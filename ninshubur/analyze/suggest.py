"""Suggestion builders (Phase 1: rules only) and the suggestions table.

A suggestion is never executed by the machine. It carries steps, reasons, evidence (exact
extracted fields with fetched_at), confidence, value, cost and timing; a human accepts,
dismisses or marks it done. Suggestions built on tainted or held evidence, or on a single
Tier >= 2 source above the corroboration value, are ``held`` and never leave the machine.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import Config
from ..db import j, one, rows, uj
from ..policy import Policy
from ..profiles import Profile
from . import relevance as rel
from . import timing, value

DAY = 86400.0
LIVE = ("new", "held")


@dataclass
class Suggestion:
    kind: str
    profile: str | None
    title: str
    steps: list[str]
    reasons: list[str]
    evidence: list[dict]
    confidence: float
    est_value: float
    est_cost: float
    cost_parts: dict
    deadline: float | None
    best_window: tuple[float, float] | None
    expires_at: float
    entity_keys: list[str]
    item_ids: list[int]
    status: str = "new"
    held_reason: str | None = None
    fingerprint: str = ""
    id: int | None = None

    @property
    def roi(self) -> float:
        return (self.est_value - self.est_cost) / max(self.est_cost, 1.0)


def fingerprint(kind: str, entity_keys: list[str], window_start: float | None) -> str:
    bucket = int(window_start // (7 * DAY)) if window_start else 0
    raw = f"{kind}|{'|'.join(sorted(entity_keys))}|{bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _evidence(item: dict, src: dict | None, values: dict) -> dict:
    return {"url": item.get("url"), "fetched_at": item.get("fetched_at"), "source": item["source"],
            "tier": int(uj(item.get("attrs")).get("tier", src["tier"] if src else 3)),
            "values": {k: v for k, v in values.items() if v not in (None, "")}}


class Suggester:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, cfg: Config, profiles: dict[str, Profile],
                 now: Callable[[], float] = time.time):
        self.conn = conn
        self.policy = policy
        self.cfg = cfg
        self.profiles = profiles
        self.now = now
        self.corroboration_usd = float(min(cfg.get("corroboration_value_usd", default=300),
                                           policy.get("corroboration", "value_usd", default=300)))
        self._sources: dict[str, dict] = {}

    # -- helpers --------------------------------------------------------------------------------
    def source(self, name: str) -> dict | None:
        if name not in self._sources:
            self._sources[name] = one(self.conn, "SELECT * FROM sources WHERE name=?", (name,))
        return self._sources[name]

    def _tier(self, item: dict) -> int:
        t = uj(item.get("attrs")).get("tier")
        if t is not None:
            return int(t)
        s = self.source(item["source"])
        return int(s["tier"]) if s else 3

    def _reputation(self, item: dict) -> float:
        s = self.source(item["source"])
        return float(s["reputation"]) if s else 0.3

    def _calibration(self, kind: str) -> float:
        r = one(self.conn, "SELECT multiplier FROM calibration WHERE kind=?", (kind,))
        return float(r["multiplier"]) if r else 1.0

    def _items(self, kind: str, since: float | None = None) -> list[dict]:
        sql = "SELECT i.*, GROUP_CONCAT(d.kind || ':' || d.at || ':' || COALESCE(d.end_at, '')) AS dates FROM items i LEFT JOIN item_dates d ON d.item_id=i.id WHERE i.kind=?"
        params: list = [kind]
        if since is not None:
            sql += " AND (i.published_at IS NULL OR i.published_at>=?)"
            params.append(since)
        sql += " GROUP BY i.id"
        return rows(self.conn, sql, params)

    @staticmethod
    def _dates(item: dict) -> dict[str, tuple[float, float | None]]:
        out: dict[str, tuple[float, float | None]] = {}
        for part in (item.get("dates") or "").split(","):
            if not part:
                continue
            k, at, end = part.split(":")
            out[k] = (float(at), float(end) if end else None)
        return out

    def _hold_reason(self, items: list[dict], tier: int, est_value: float) -> str | None:
        if any(i["tainted"] for i in items):
            return "evidence_tainted"
        if any(i["held"] for i in items):
            return "source_suspect"
        sources = {i["source"] for i in items}
        if tier >= 2 and len(sources) <= 1 and est_value > self.corroboration_usd:
            return "needs_corroboration"
        return None

    def _finish(self, s: Suggestion, items: list[dict], tier: int) -> Suggestion:
        s.fingerprint = fingerprint(s.kind, s.entity_keys, s.best_window[0] if s.best_window else None)
        reason = self._hold_reason(items, tier, s.est_value)
        if reason:
            s.status, s.held_reason = "held", reason
        return s

    def _profile_items(self, kind: str, profile: Profile, since: float | None, threshold_key: str = "relevance") -> list[tuple[dict, rel.Relevance]]:
        out = []
        now = self.now()
        for it in self._items(kind, since):
            text = f"{it['title']} {it.get('text') or ''} {' '.join(str(v) for v in uj(it.get('attrs')).values() if isinstance(v, str))}"
            r = rel.score(text, it["kind"], it.get("published_at"), self._reputation(it), profile, now)
            if r.score >= profile.thresholds.get(threshold_key, 0.55):
                out.append((it, r))
        return out

    # -- builders -------------------------------------------------------------------------------
    def build_cfp_submit(self, profile: Profile) -> list[Suggestion]:
        now = self.now()
        out = []
        for it in self._items("event"):
            d = self._dates(it)
            if "cfp_deadline" not in d:
                continue
            deadline = d["cfp_deadline"][0]
            if not (now + 3 * DAY <= deadline <= now + 90 * DAY):
                continue
            attrs = uj(it["attrs"])
            topic_hit = attrs.get("topic") in {t.lower() for t in profile.topics}
            geo_hit = profile.matches_city(attrs.get("city")) or profile.matches_country(attrs.get("country"))
            lex, matched, _ = rel.lexical(it["title"], profile)
            if not (topic_hit or geo_hit or lex > 0):
                continue
            w = timing.cfp(deadline, now)
            tier = self._tier(it)
            conf = value.confidence(self._reputation(it), 1.0, 1, tier, self.policy)
            relevance = max(0.6 if topic_hit else 0.0, 0.5 if geo_hit else 0.0, lex)
            est = value.estimate("cfp_submit", cfg=self.cfg, policy=self.policy, tier=tier, profile_weight=profile.weight("cfp_submit"),
                                 relevance=relevance, conf=conf, timing_factor=w.factor(now), minutes=value.minutes_for("cfp_submit", self.cfg, 120),
                                 calibration=self._calibration("cfp_submit"))
            reasons = [f"CFP closes {_d(deadline)} ({(deadline - now) / DAY:.0f} days)",
                       f"match: {'topic ' + str(attrs.get('topic')) if topic_hit else ''}{' city/country' if geo_hit else ''}{' keywords ' + ', '.join(matched) if matched else ''}".strip(),
                       est.formula]
            s = Suggestion(kind="cfp_submit", profile=profile.name, title=f"Submit a talk to {it['title']}",
                           steps=[f"Draft an abstract from the top pains: {', '.join(profile.pains[:3]) or 'see profile'}",
                                  f"Submit via {attrs.get('cfpUrl') or it['url']} before {_d(deadline - DAY)}",
                                  "Note the decision date in the calendar feed"],
                           reasons=reasons, evidence=[_evidence(it, self.source(it["source"]), {"name": it["title"], "cfpEndDate": _d(deadline),
                                                                                              "startDate": _d(d.get("event_start", (0, None))[0]) if "event_start" in d else None,
                                                                                              "city": attrs.get("city"), "cfpUrl": attrs.get("cfpUrl")})],
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=deadline,
                           best_window=(w.start, w.end), expires_at=deadline, entity_keys=[f"event:{it['uid']}"], item_ids=[it["id"]])
            out.append(self._finish(s, [it], tier))
        return out

    def build_conference_trip(self, profile: Profile) -> list[Suggestion]:
        now = self.now()
        out = []
        for it in self._items("event"):
            d = self._dates(it)
            if "event_start" not in d:
                continue
            start, end = d["event_start"]
            attrs = uj(it["attrs"])
            if attrs.get("online") or not (now < start <= now + 60 * DAY):
                continue
            city_hit = profile.matches_city(attrs.get("city"))
            country_hit = profile.matches_country(attrs.get("country"))
            topic_hit = str(attrs.get("topic", "")).lower() in {t.lower() for t in profile.topics}
            if not (city_hit or country_hit or topic_hit):
                continue
            w = timing.event_trip(start, now)
            tier = self._tier(it)
            conf = value.confidence(self._reputation(it), 1.0 if attrs.get("city") else 0.7, 1, tier, self.policy)
            travel = value.travel_band(profile.home_base, attrs.get("city"), profile.home_country, attrs.get("country"), self.cfg)
            relevance = 0.8 if city_hit else (0.55 if country_hit else 0.4)
            est = value.estimate("conference_trip", cfg=self.cfg, policy=self.policy, tier=tier, profile_weight=profile.weight("conference_trip"),
                                 relevance=relevance, conf=conf, timing_factor=w.factor(now),
                                 minutes=value.minutes_for("conference_trip", self.cfg, 240), travel_usd=travel,
                                 calibration=self._calibration("conference_trip"))
            s = Suggestion(kind="conference_trip", profile=profile.name,
                           title=f"Attend {it['title']} in {attrs.get('city') or attrs.get('country')} ({_d(start)})",
                           steps=[f"Shortlist prospects in {attrs.get('city') or attrs.get('country')} with titles {', '.join(profile.titles[:3]) or 'from profile'}",
                                  f"Decide and book before {_d(w.decide_by or w.deadline or start)}",
                                  f"Propose three meeting slots between {_d(w.start)} and {_d(w.end)} in local business hours"],
                           reasons=[f"{it['title']} starts {_d(start)} in {attrs.get('city')}, a profile city" if city_hit
                                    else (f"{it['title']} is in {attrs.get('country')}, a profile country" if country_hit
                                          else f"{it['title']} is on profile topic {attrs.get('topic')} (outside profile geos)"), est.formula],
                           evidence=[_evidence(it, self.source(it["source"]), {"name": it["title"], "startDate": _d(start), "endDate": _d(end) if end else None,
                                                                                "city": attrs.get("city"), "country": attrs.get("country")})],
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=w.deadline,
                           best_window=(w.start, w.end), expires_at=start, entity_keys=[f"event:{it['uid']}"], item_ids=[it["id"]])
            out.append(self._finish(s, [it], tier))
        return out

    def build_service_request_lead(self, profile: Profile) -> list[Suggestion]:
        now = self.now()
        out = []
        for it, r in self._profile_items("service_request", profile, now - 7 * DAY):
            posted = it.get("published_at") or now
            w = timing.social_request(posted, now)
            tier = self._tier(it)
            conf = value.confidence(self._reputation(it), 0.8, 1, tier, self.policy)
            est = value.estimate("service_request_lead", cfg=self.cfg, policy=self.policy, tier=tier,
                                 profile_weight=profile.weight("service_request_lead"), relevance=r.score, conf=conf,
                                 timing_factor=w.factor(now), minutes=value.minutes_for("service_request_lead", self.cfg, 10),
                                 calibration=self._calibration("service_request_lead"))
            attrs = uj(it["attrs"])
            s = Suggestion(kind="service_request_lead", profile=profile.name, title=f"Reply to a request: {it['title'][:70]}",
                           steps=["Read the post and reply only through the contact method given in the post itself",
                                  "Lead with the matching pain and one concrete result; no attachments, no tracking links",
                                  f"Send before {_d(w.end)}; mark done with the outcome"],
                           reasons=[f"matched {', '.join(r.matched)} (relevance {r.score:.2f})",
                                    f"posted {_d(posted)} by {attrs.get('author', '?')}; classification {attrs.get('classification', '?')}",
                                    est.formula],
                           evidence=[_evidence(it, self.source(it["source"]), {"author": attrs.get("author"), "created_at": _d(posted),
                                                                                "classification": attrs.get("classification")})],
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=w.deadline,
                           best_window=(w.start, w.end), expires_at=w.deadline or now + 7 * DAY, entity_keys=[f"item:{it['uid']}"], item_ids=[it["id"]])
            out.append(self._finish(s, [it], tier))
        return out

    def build_hiring_signal_outreach(self, profile: Profile) -> list[Suggestion]:
        now = self.now()
        out = []
        for it in self._items("hiring", now - 30 * DAY):
            attrs = uj(it["attrs"])
            company = (profile.company_by_board(str(attrs.get("board", ""))) or profile.company_by_key(f"company:{attrs.get('company', '')}")
                       or profile.company_by_name(attrs.get("company")))
            lex, matched, negatives = rel.lexical(it["title"], profile)
            title_hit = any(t.lower() in it["title"].lower() for t in profile.titles)
            if not (company or lex > 0 or title_hit) or negatives:
                continue
            posted = it.get("published_at") or now
            w = timing.hiring_post(posted)
            tier = self._tier(it)
            conf = value.confidence(self._reputation(it), 1.0, 1, tier, self.policy)
            relevance = max(0.75 if company else 0.0, lex, 0.6 if title_hit else 0.0)
            est = value.estimate("hiring_signal_outreach", cfg=self.cfg, policy=self.policy, tier=tier,
                                 profile_weight=profile.weight("hiring_signal_outreach"), relevance=relevance, conf=conf,
                                 timing_factor=w.factor(now), minutes=value.minutes_for("hiring_signal_outreach", self.cfg, 15),
                                 calibration=self._calibration("hiring_signal_outreach"))
            name = (company or {}).get("name") or attrs.get("company") or attrs.get("board")
            s = Suggestion(kind="hiring_signal_outreach", profile=profile.name, title=f"{name} is hiring: {it['title']}",
                           steps=[f"Reference the posting ({it['url']}) and what the hire implies about their pipeline",
                                  "Offer the service the hire implies (e.g. outsourced outbound while they staff up)",
                                  f"Send between {_d(w.start)} and {_d(w.end)}, Tue-Thu morning local time"],
                           reasons=[("target company " + name) if company else f"title matches profile ({', '.join(matched) or 'title'})",
                                    f"posted {_d(posted)} in {attrs.get('location') or 'unknown location'}", est.formula],
                           evidence=[_evidence(it, self.source(it["source"]), {"title": it["title"], "location": attrs.get("location"),
                                                                                "posted": _d(posted), "company": name})],
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=w.deadline,
                           best_window=(w.start, w.end), expires_at=w.deadline or now + 30 * DAY,
                           entity_keys=[f"company:{(company or {}).get('domain') or attrs.get('board')}", f"job:{attrs.get('job_id')}"],
                           item_ids=[it["id"]])
            out.append(self._finish(s, [it], tier))
        return out

    def build_tool_outage(self, profile: Profile) -> list[Suggestion]:
        now = self.now()
        out = []
        tools = profile.tool_ids()
        for it in self._items("incident"):
            attrs = uj(it["attrs"])
            provider = str(attrs.get("provider", "")).lower()
            if attrs.get("resolved") or attrs.get("impact") not in ("major", "critical"):
                continue
            if not any(t and (t in provider or provider in t) for t in tools):
                continue
            w = timing.outage(now)
            tier = self._tier(it)
            conf = value.confidence(self._reputation(it), 1.0, 1, tier, self.policy)
            est = value.estimate("tool_outage", cfg=self.cfg, policy=self.policy, tier=tier, profile_weight=profile.weight("tool_outage"),
                                 relevance=1.0, conf=conf, timing_factor=1.0, minutes=value.minutes_for("tool_outage", self.cfg, 0),
                                 calibration=self._calibration("tool_outage"))
            started = self._dates(it).get("incident_started", (it.get("published_at") or now, None))[0]
            s = Suggestion(kind="tool_outage", profile=profile.name, title=f"{provider} incident: {it['title']}",
                           steps=["Pause work that depends on this provider or switch to the configured fallback",
                                  f"Watch {it['url']} for resolution; this suggestion expires itself when the incident resolves"],
                           reasons=[f"{attrs.get('impact')} impact, status {attrs.get('status')}, started {_d(started)}", est.formula],
                           evidence=[_evidence(it, self.source(it["source"]), {"name": it["title"], "status": attrs.get("status"),
                                                                                "impact": attrs.get("impact"), "created_at": _d(started)})],
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=None,
                           best_window=(w.start, w.end), expires_at=now + 3 * DAY, entity_keys=[f"incident:{it['uid']}"], item_ids=[it["id"]])
            out.append(self._finish(s, [it], tier))
        return out

    BUILDERS = ("build_cfp_submit", "build_conference_trip", "build_service_request_lead", "build_hiring_signal_outreach", "build_tool_outage")

    # -- orchestration ----------------------------------------------------------------------------
    def build_all(self) -> list[Suggestion]:
        built: list[Suggestion] = []
        for profile in self.profiles.values():
            for name in self.BUILDERS:
                built.extend(getattr(self, name)(profile))
        self._upsert(built)
        self.expire()
        return built

    def _upsert(self, built: list[Suggestion]) -> None:
        now = self.now()
        for s in built:
            existing = one(self.conn, "SELECT id, status FROM suggestions WHERE fingerprint=?", (s.fingerprint,))
            if existing is None:
                cur = self.conn.execute(
                    """INSERT INTO suggestions(fingerprint, kind, profile, title, steps, reasons, evidence, confidence, est_value, est_cost,
                       cost_parts, deadline, best_window_start, best_window_end, expires_at, status, held_reason, created_at, updated_at,
                       entity_keys, item_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (s.fingerprint, s.kind, s.profile, s.title, j(s.steps), j(s.reasons), j(s.evidence), s.confidence, s.est_value,
                     s.est_cost, j(s.cost_parts), s.deadline, s.best_window[0] if s.best_window else None,
                     s.best_window[1] if s.best_window else None, s.expires_at, s.status, s.held_reason, now, now, j(s.entity_keys), j(s.item_ids)))
                s.id = int(cur.lastrowid)
            else:
                s.id = int(existing["id"])
                status = existing["status"]
                if status in LIVE:
                    status = s.status  # held <-> new follows the evidence; human decisions stick
                    held_reason = s.held_reason
                else:
                    held_reason = None
                self.conn.execute(
                    """UPDATE suggestions SET title=?, steps=?, reasons=?, evidence=?, confidence=?, est_value=?, est_cost=?, cost_parts=?,
                       deadline=?, best_window_start=?, best_window_end=?, expires_at=?, status=?, held_reason=?, updated_at=?, item_ids=? WHERE id=?""",
                    (s.title, j(s.steps), j(s.reasons), j(s.evidence), s.confidence, s.est_value, s.est_cost, j(s.cost_parts), s.deadline,
                     s.best_window[0] if s.best_window else None, s.best_window[1] if s.best_window else None, s.expires_at, status,
                     held_reason, now, j(s.item_ids), s.id))
                s.status = status

    def expire(self) -> int:
        now = self.now()
        n = self.conn.execute("UPDATE suggestions SET status='expired', updated_at=? WHERE status IN ('new','held') AND expires_at<?", (now, now)).rowcount
        # outages resolve themselves
        for s in rows(self.conn, "SELECT id, item_ids FROM suggestions WHERE kind='tool_outage' AND status IN ('new','held','accepted')"):
            ids = uj(s["item_ids"], [])
            if ids and all(uj(i["attrs"]).get("resolved") for i in rows(self.conn, f"SELECT attrs FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)):
                self.conn.execute("UPDATE suggestions SET status='expired', updated_at=? WHERE id=?", (now, s["id"]))
                n += 1
        return int(n)

    # -- reading ----------------------------------------------------------------------------------
    def list(self, *, status: tuple[str, ...] = ("new",), profile: str | None = None, kind: str | None = None, top: int = 20) -> list[dict]:
        sql = f"SELECT * FROM suggestions WHERE status IN ({','.join('?' * len(status))})"
        params: list = list(status)
        if profile:
            sql += " AND profile=?"
            params.append(profile)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        out = [_hydrate(r) for r in rows(self.conn, sql, params)]
        out.sort(key=lambda r: r["roi"], reverse=True)
        return out[:top]

    def get(self, sid: int) -> dict | None:
        r = one(self.conn, "SELECT * FROM suggestions WHERE id=?", (sid,))
        return _hydrate(r) if r else None

    def plan(self, minutes: int, **kw) -> list[dict]:
        """Greedy knapsack over human minutes, by ROI density."""
        picked, used = [], 0.0
        for s in self.list(top=1000, **kw):
            m = float(s["cost_parts"].get("human_minutes", 0))
            if used + m <= minutes:
                picked.append(s)
                used += m
        return picked


def _hydrate(r: dict) -> dict:
    out = dict(r)
    for k in ("steps", "reasons", "evidence", "cost_parts", "entity_keys", "item_ids"):
        out[k] = uj(r.get(k), [] if k != "cost_parts" else {})
    out["roi"] = (r["est_value"] - r["est_cost"]) / max(r["est_cost"], 1.0)
    return out


def _d(ts: float | None) -> str:
    if ts is None:
        return "?"
    return time.strftime("%Y-%m-%d", time.gmtime(ts))
