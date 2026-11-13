"""Suggestion builders (Phase 1: rules only) and the suggestions table.

A suggestion is never executed by the machine. It carries steps, reasons, evidence (exact
extracted fields with fetched_at), confidence, value, cost and timing; a human accepts,
dismisses or marks it done. Suggestions built on tainted or held evidence, or on a single
Tier >= 2 source above the corroboration value, are ``held`` and never leave the machine.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..companies import AliasesError, Resolver, aliases_path, display_name, load_aliases, rollup
from ..config import Config
from ..db import j, one, rows, uj
from ..home import data_home
from ..logs import JsonlLog
from ..policy import Policy
from ..profiles import Profile
from . import relevance as rel
from . import timing, value

DAY = 86400.0
LIVE = ("new", "held")
DECIDED = ("accepted", "dismissed", "done")
EVIDENCE_CAP = 5  # postings shown per company suggestion (the newest); the reasons give the total


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


def company_fingerprint(kind: str, profile: str | None, company_key: str, generation: int = 0) -> str:
    """Per-company kinds: profile and company, no time bucket (see ``Suggester._company_fingerprint``)."""
    raw = f"{kind}|profile:{profile or ''}|{company_key}|g{generation}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _evidence(item: dict, src: dict | None, values: dict) -> dict:
    return {"url": item.get("url"), "fetched_at": item.get("fetched_at"), "source": item["source"],
            "tier": int(uj(item.get("attrs")).get("tier", src["tier"] if src else 3)),
            "values": {k: v for k, v in values.items() if v not in (None, "")}}


class Suggester:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, cfg: Config, profiles: dict[str, Profile],
                 now: Callable[[], float] = time.time, home: Path | None = None):
        self.conn = conn
        self.policy = policy
        self.cfg = cfg
        self.profiles = profiles
        self.now = now
        self.corroboration_usd = float(min(cfg.get("corroboration_value_usd", default=300),
                                           policy.get("corroboration", "value_usd", default=300)))
        self._sources: dict[str, dict] = {}
        self.home = home
        self._aliases_mtime: float | None = None
        self._aliases_good: dict = {}
        self.aliases_error: str | None = None
        self.resolver: Resolver = self._load_resolver()
        self.last_regroup: dict = {}
        self._new_company_rows: dict[str, int] = {}

    def _load_resolver(self) -> Resolver:
        """Config, profiles and the aliases file; the file is read again when it changes (a running daemon).
        A malformed file never stops crawling or suggestions: the last good aliases (or none) stay in use,
        the error is printed once on stderr, logged once to logs/policy.jsonl, and kept in ``aliases_error``
        for `check` and `companies`."""
        path = aliases_path(self.home)
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        self._aliases_mtime = mtime
        try:
            aliases = load_aliases(path)
        except AliasesError as e:
            msg = str(e)
            if msg != self.aliases_error:
                print(f"warning: {msg} (using the last good aliases until it is fixed)", file=sys.stderr)
                JsonlLog((self.home or data_home()) / "logs" / "policy.jsonl").write(
                    {"event": "aliases_error", "path": str(path), "detail": msg[:300]})
            self.aliases_error = msg
            aliases = self._aliases_good
        else:
            self.aliases_error = None
            self._aliases_good = aliases
        return Resolver(self.cfg, self.profiles, aliases, aliases_file=path)

    def refresh_resolver(self) -> Resolver:
        try:
            mtime: float | None = aliases_path(self.home).stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._aliases_mtime:
            self.resolver = self._load_resolver()
        return self.resolver

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

    def _hold_reason(self, items: list[dict], tier: int, est_value: float, n_sources: int | None = None) -> str | None:
        if any(i["tainted"] for i in items):
            return "evidence_tainted"
        if any(i["held"] for i in items):
            return "source_suspect"
        n = len({i["source"] for i in items}) if n_sources is None else n_sources
        if tier >= 2 and n <= 1 and est_value > self.corroboration_usd:
            return "needs_corroboration"
        return None

    def _finish(self, s: Suggestion, items: list[dict], tier: int, n_sources: int | None = None) -> Suggestion:
        s.fingerprint = fingerprint(s.kind, s.entity_keys, s.best_window[0] if s.best_window else None)
        reason = self._hold_reason(items, tier, s.est_value, n_sources)
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

    def _prospects(self, event_uid: str) -> tuple[list[dict], list[dict], list[dict]]:
        """(pages, persons, orgs) crawled for an event: entities linked to its prospect_page items."""
        pages = rows(self.conn, "SELECT * FROM items WHERE kind='prospect_page' AND json_extract(attrs, '$.event_uid')=? ORDER BY id", (event_uid,))
        if not pages:
            return [], [], []
        ids = [p["id"] for p in pages]
        q = f"""SELECT e.*, ie.role AS link_role, i.held AS page_held, i.tainted AS page_tainted FROM entities e
                JOIN item_entities ie ON ie.entity_id=e.id JOIN items i ON i.id=ie.item_id
                WHERE ie.item_id IN ({','.join('?' * len(ids))}) AND e.forgotten_at IS NULL"""
        ents = rows(self.conn, q, ids)
        persons = [e for e in ents if e["type"] == "person"]
        orgs = [e for e in ents if e["type"] == "company"]
        return pages, persons, orgs

    def build_conference_prospecting(self, profile: Profile) -> list[Suggestion]:
        now = self.now()
        out = []
        for it in self._items("event"):
            d = self._dates(it)
            if "event_start" not in d:
                continue
            start, end = d["event_start"]
            end = end or start
            attrs = uj(it["attrs"])
            if now <= start <= now + 90 * DAY:
                phase = "upcoming"
                w = timing.Window(max(now, start - 28 * DAY), max(now, start - 7 * DAY), max(now + DAY, start - 7 * DAY))
                expires = end + DAY
            elif end < now and now - end <= 180 * DAY:
                phase = "held"
                w = timing.Window(now, now + 21 * DAY, None)
                expires = now + 45 * DAY
            else:
                continue
            pages, persons, orgs = self._prospects(it["uid"])
            if not pages or (len(orgs) < 1 and len(persons) < 3):
                continue
            city_hit = profile.matches_city(attrs.get("city"))
            country_hit = profile.matches_country(attrs.get("country"))
            topic_hit = str(attrs.get("topic", "")).lower() in {t.lower() for t in profile.topics}
            geo_rel = 1.0 if city_hit else (0.85 if country_hit else (0.7 if topic_hit else 0.5))
            matched_orgs = [o for o in orgs if profile.company_by_name(o["name"]) or (o.get("domain") and profile.company_by_key(f"company:{o['domain']}"))]
            matched_people = [p for p in persons if p.get("title") and any(t.lower() in p["title"].lower() for t in profile.titles)]
            paid = [o for o in orgs if o.get("link_role") in ("sponsor", "exhibitor", "partner")]
            n_matched = len(matched_orgs) + len(matched_people)
            match_ratio = min(1.0, 0.5 + 0.5 * n_matched / max(1, len(paid) + len(persons)) + 0.1 * min(5, n_matched))
            scale = min(2.0, math.log2(1 + len(paid) + len(orgs) + len(persons) / 5))
            tier = 2
            agree = max([int(e.get("seen_events") or 1) for e in orgs + persons] + [1])
            conf = value.confidence(self._reputation(pages[0]), 0.9, agree, tier, self.policy)
            minutes = min(120.0, value.minutes_for("conference_prospecting", self.cfg, 20) + 3 * n_matched)
            est = value.estimate("conference_prospecting", cfg=self.cfg, policy=self.policy, tier=tier,
                                 profile_weight=profile.weight("conference_prospecting"), relevance=round(geo_rel * match_ratio * scale / 2, 3),
                                 conf=conf, timing_factor=w.factor(now), minutes=minutes, calibration=self._calibration("conference_prospecting"))
            where = attrs.get("city") or attrs.get("country") or "online"
            if phase == "upcoming":
                title = f"Prospect {it['title']} ({where}, {_d(start)}): {len(paid)} paying companies, {len(persons)} speakers"
                steps = [f"Shortlist the sponsors/exhibitors{' (' + ', '.join(o['name'] for o in matched_orgs[:5]) + ' match the profile)' if matched_orgs else ''}",
                         f"Pick speakers with profile titles{' (' + ', '.join(p['name'] for p in matched_people[:5]) + ')' if matched_people else ''}",
                         f"Send a short pre-event note between {_d(w.start)} and {_d(w.end)}; verify each contact before writing",
                         "Offer a meeting during the event week; mark done with the outcome"]
                reason0 = f"{it['title']} starts {_d(start)} in {where}; {len(paid)} companies paid for presence, {len(persons)} speakers listed"
            else:
                title = f"Follow up {it['title']} ({where}, held {_d(end)}): {len(paid)} paying companies, {len(persons)} speakers"
                steps = [f"Shortlist the companies that paid for a stand or sponsorship{' (' + ', '.join(o['name'] for o in matched_orgs[:5]) + ' match the profile)' if matched_orgs else ''}",
                         "Angle: measurable pipeline versus what the event cost them; ask what the event delivered",
                         f"Send between {_d(w.start)} and {_d(w.end)}; follow up two weeks later",
                         "Speakers with profile titles are secondary targets; verify each contact before writing"]
                reason0 = f"{it['title']} was held {_d(end)} in {where}; {len(paid)} companies paid for presence, {len(persons)} speakers listed"
            reasons = [reason0,
                       f"profile match: {len(matched_orgs)} target companies, {len(matched_people)} speakers with target titles; geo/topic factor {geo_rel:.2f}",
                       "every prospect is unverified (Tier 2 page content): confirm role and company before contact",
                       est.formula]
            evidence = [_evidence(pg, self.source(pg["source"]), {"page": pg["url"], "persons": uj(pg["attrs"]).get("persons"),
                                                                 "orgs": uj(pg["attrs"]).get("orgs"), "layer": uj(pg["attrs"]).get("layer")})
                        for pg in pages]
            evidence.insert(0, _evidence(it, self.source(it["source"]), {"name": it["title"], "startDate": _d(start), "city": attrs.get("city"),
                                                                        "country": attrs.get("country")}))
            s = Suggestion(kind="conference_prospecting", profile=profile.name, title=title, steps=steps, reasons=reasons, evidence=evidence,
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=w.deadline,
                           best_window=(w.start, w.end), expires_at=expires, entity_keys=[f"event:{it['uid']}", phase],
                           item_ids=[it["id"]] + [pg["id"] for pg in pages])
            out.append(self._finish(s, [it] + pages, tier))
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
        """One suggestion per company and profile, over every qualifying posting of the last 30 days."""
        now = self.now()
        resolver = self.resolver
        targets: dict[str, dict] = {}
        for t in profile.target_companies:
            k = resolver.key_for_target(t)
            if k:
                targets.setdefault(k, t)
        groups: dict[str, list[dict]] = {}
        for it in self._items("hiring", now - 30 * DAY):
            res = resolver.resolve(it)
            if res is None:
                continue
            attrs = uj(it["attrs"])
            company = targets.get(res.key)
            lex, matched, negatives = rel.lexical(it["title"], profile)
            title_hit = any(t.lower() in it["title"].lower() for t in profile.titles)
            roles = [r for r in (attrs.get("roles") or []) if isinstance(r, str)]
            if not (company or lex > 0 or title_hit or roles) or negatives:
                continue
            groups.setdefault(res.key, []).append({
                "it": it, "res": res, "attrs": attrs, "company": company, "matched": matched, "lex": lex, "title_hit": title_hit,
                "roles": roles, "posted": it.get("published_at") or now,
                "relevance": max(0.75 if company else 0.0, lex, 0.6 if title_hit else 0.0, 0.5 if roles else 0.0)})
        known = self._company_rows(profile.name)
        out = []
        for key in sorted(groups):
            g = sorted(groups[key], key=lambda x: (-x["posted"], x["it"]["id"]))  # newest first
            newest, n = g[0], len(g)
            best = max(g, key=lambda x: x["relevance"])  # ties: the newest
            items = [x["it"] for x in g]
            tiers = {x["it"]["id"]: self._tier(x["it"]) for x in g}
            # Untrusted items (a domain or name read from a post) join for grouping and evidence but never move
            # trust: with trusted items, tier, reputation and corroboration come from those only; without, from
            # the least trusted posting, and claims never corroborate each other (one source).
            trusted = [x for x in g if x["res"].trusted]
            basis = [x["it"] for x in trusted] or items
            tier = max(tiers[i["id"]] for i in basis)
            sources = sorted({i["source"] for i in items})
            n_sources = len({i["source"] for i in basis}) if trusted else 1
            conf = value.confidence(min(self._reputation(i) for i in basis), 1.0, n_sources, tier, self.policy)
            relevance = best["relevance"]
            posted = newest["posted"]
            w = timing.hiring_post(posted)
            est = value.estimate("hiring_signal_outreach", cfg=self.cfg, policy=self.policy, tier=tier,
                                 profile_weight=profile.weight("hiring_signal_outreach"), relevance=relevance, conf=conf,
                                 timing_factor=w.factor(now), minutes=value.minutes_for("hiring_signal_outreach", self.cfg, 15),
                                 calibration=self._calibration("hiring_signal_outreach"))
            company = next((x["company"] for x in g if x["company"]), None)
            name = (company or {}).get("name") or display_name([(x["res"], float(x["it"]["first_seen_at"]), int(x["it"]["id"])) for x in g])
            matched = sorted({m for x in g for m in x["matched"]})
            roles = sorted({r for x in g for r in x["roles"]})
            where = newest["attrs"].get("location") or "unknown location"
            shown = g[:EVIDENCE_CAP]
            weakest = max(g, key=lambda x: (tiers[x["it"]["id"]], x["posted"]))
            if tiers[weakest["it"]["id"]] > max(tiers[x["it"]["id"]] for x in shown):
                shown = shown + [weakest]  # the badge must show the least trusted evidence

            def values(x: dict) -> dict:
                v = {"title": x["it"]["title"], "location": x["attrs"].get("location"), "posted": _d(x["posted"]), "company": name}
                if trusted and not x["res"].trusted:
                    v["match"] = "name only, unverified"
                return v
            s = Suggestion(kind="hiring_signal_outreach", profile=profile.name,
                           title=f"{name} is hiring: {newest['it']['title']}" if n == 1 else f"{name} is hiring: {n} roles ({best['it']['title']})",
                           steps=[f"Reference the posting ({newest['it']['url']}) and what the hire implies about their pipeline",
                                  "Offer the service the hire implies (e.g. outsourced outbound while they staff up)",
                                  f"Send between {_d(w.start)} and {_d(w.end)}, Tue-Thu morning local time"],
                           reasons=[("target company " + name) if company
                                    else (f"title matches profile ({', '.join(matched) or 'title'})" if any(x["lex"] > 0 or x["title_hit"] for x in g)
                                          else f"hiring for {', '.join(roles)}: roles that imply an outbound need"),
                                    f"posted {_d(posted)} in {where}" if n == 1 else
                                    (f"{n} open postings in 30 days from {', '.join(sources)}; newest posted {_d(posted)} in {where}"
                                     + (f"; evidence lists the {len(shown)} most recent" if n > len(shown) else "")),
                                    est.formula],
                           evidence=[_evidence(x["it"], self.source(x["it"]["source"]), values(x)) for x in shown],
                           confidence=conf, est_value=est.value, est_cost=est.cost, cost_parts=est.cost_parts, deadline=w.deadline,
                           best_window=(w.start, w.end), expires_at=w.deadline or now + 30 * DAY,
                           entity_keys=[key], item_ids=sorted(i["id"] for i in items))
            self._finish(s, items, tier, n_sources)  # any tainted or held posting in the group still holds it
            s.fingerprint = self._company_fingerprint(s, known.get(j(s.entity_keys)), basis)
            out.append(s)
        return out

    def _company_rows(self, profile: str) -> dict[str, list[dict]]:
        """Per-company hiring rows of one profile, newest first, with the time of the last human decision."""
        out: dict[str, list[dict]] = {}
        for r in rows(self.conn, """SELECT s.id, s.fingerprint, s.status, s.entity_keys, s.updated_at, (SELECT MAX(f.at) FROM feedback f
                                    WHERE f.suggestion_id=s.id) AS decided_at FROM suggestions s
                                    WHERE s.kind='hiring_signal_outreach' AND s.profile=? ORDER BY s.id DESC""", (profile,)):
            if not any(k.startswith("job:") for k in uj(r["entity_keys"], [])):
                out.setdefault(r["entity_keys"], []).append(r)
        return out

    @staticmethod
    def _company_fingerprint(s: Suggestion, known: list[dict] | None, items: list[dict]) -> str:
        """One live suggestion per company and profile. The fingerprint has no week bucket, so a new posting
        updates the live row instead of adding one. After a person decided (accepted, dismissed, done) the
        decision sticks until a posting first seen after it arrives (``items``: the trusted postings when the
        group has any, so a name-only post cannot resurface a dismissed company); after an expiry, any
        qualifying posting is new. Then a new generation starts, as a new posting started a new suggestion
        before. A decision without a feedback row counts from the row's last update."""
        gen = len(known or [])
        if known:
            latest = known[0]
            if latest["status"] in LIVE:
                return latest["fingerprint"]
            if latest["status"] != "expired":
                decided = float(latest["decided_at"] or latest["updated_at"] or 0.0)
                if not any(float(i["first_seen_at"]) > decided for i in items):
                    return latest["fingerprint"]
        return company_fingerprint(s.kind, s.profile, s.entity_keys[0], gen)

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

    BUILDERS = ("build_cfp_submit", "build_conference_prospecting", "build_service_request_lead", "build_hiring_signal_outreach", "build_tool_outage")

    # -- orchestration ----------------------------------------------------------------------------
    def build_all(self) -> list[Suggestion]:
        rollup(self.conn, self.refresh_resolver(), self.policy, self.now())  # company signals first, cheap and idempotent
        built: list[Suggestion] = []
        for profile in self.profiles.values():
            for name in self.BUILDERS:
                built.extend(getattr(self, name)(profile))
        self._new_company_rows = {}
        self._upsert(built)
        self.last_regroup = self.regroup(built)
        self.expire()
        return built

    def regroup(self, built: list[Suggestion] | None = None, *, dry_run: bool = False) -> dict:
        """Retire hiring rows the per-company suggestions replace.

        Per-posting rows of earlier versions (``entity_keys`` with a ``job:`` key) still ``new``/``held`` whose
        posting a company suggestion of the same profile now covers are expired. So are live company rows
        whose postings all moved to a different key (an alias added later). Never deleted, never a row a
        person decided. ``dry_run`` builds in memory and only counts, including the company rows the
        rebuild will create (per profile) and the old rows already past their own expiry date."""
        if built is None:
            self.refresh_resolver()
            built = [s for p in self.profiles.values() for s in self.build_hiring_signal_outreach(p)]
        built = [s for s in built if s.kind == "hiring_signal_outreach"]
        now = self.now()
        cover: dict[tuple[str | None, int], str] = {}
        for s in built:
            for iid in s.item_ids:
                cover[(s.profile, iid)] = s.entity_keys[0]
        built_keys = {(s.profile, s.entity_keys[0]) for s in built}
        allrows = rows(self.conn, "SELECT id, fingerprint, profile, status, entity_keys, item_ids, expires_at FROM suggestions "
                                  "WHERE kind='hiring_signal_outreach' ORDER BY id")
        old, moved = [], []
        for r in allrows:
            keys = [str(k) for k in uj(r["entity_keys"], [])]
            if any(k.startswith("job:") for k in keys):
                old.append(r)
            elif r["status"] in LIVE and keys and (r["profile"], keys[0]) not in built_keys:
                ids = [int(i) for i in uj(r["item_ids"], [])]
                if ids and all(cover.get((r["profile"], i)) not in (None, keys[0]) for i in ids):
                    moved.append(r)
        live = [r for r in old if r["status"] in LIVE]
        expire: list[int] = []
        companies: set[str] = set()
        for r in live:
            keys = {cover.get((r["profile"], int(i))) for i in uj(r["item_ids"], [])} - {None}
            if keys:
                expire.append(int(r["id"]))
                companies |= keys  # type: ignore[arg-type]
        rest = [r for r in live if int(r["id"]) not in expire]
        by_date = sum(1 for r in rest if r["expires_at"] < now)
        if dry_run:
            existing = {r["fingerprint"] for r in allrows}
            new_rows: dict[str, int] = {}
            for s in built:
                if s.fingerprint not in existing:
                    new_rows[str(s.profile)] = new_rows.get(str(s.profile), 0) + 1
        else:
            new_rows = dict(self._new_company_rows)
        ids = expire + [int(r["id"]) for r in moved]
        if ids and not dry_run:
            self.conn.execute(f"UPDATE suggestions SET status='expired', updated_at=? WHERE status IN ('new','held') "
                              f"AND id IN ({','.join('?' * len(ids))})", [now, *ids])
        return {"live_per_posting": len(live), "expire": len(expire), "companies": len(companies),
                "new_rows": dict(sorted(new_rows.items())), "moved": len(moved),
                "stay_live": len(rest) - by_date, "past_expiry": by_date,
                "decided_untouched": sum(1 for r in old if r["status"] in DECIDED)}

    def _upsert(self, built: list[Suggestion]) -> None:
        now = self.now()
        for s in built:
            existing = one(self.conn, "SELECT id, status FROM suggestions WHERE fingerprint=?", (s.fingerprint,))
            if existing is None:
                if s.kind == "hiring_signal_outreach":
                    self._new_company_rows[str(s.profile)] = self._new_company_rows.get(str(s.profile), 0) + 1
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
