"""Ingest: turn a ParseResult into stored rows while applying the fallback ladder.

F1 item: medium taint -> stored ``tainted``; high taint -> dropped + quarantine row (hashes only).
F2 source: high taint or schema drift -> ``suspect`` (new items ``held``); integrity, 3 taint
events in 24 h or reputation < quarantine_below -> ``quarantined`` (no fetching).
F6 recovery: clean documents become the source's last-good snapshot.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import detectors
from .adapters.base import AdapterSpec, Fetched, ParseResult
from .config import Config, SourceConfig
from .db import j, one, uj
from .items import Item
from .logs import JsonlLog
from .policy import Policy

INTEGRITY_CODES = frozenset({"cert_error", "off_host_redirect", "content_type_mismatch", "body_too_large",
                             "decompression_bomb", "invalid_utf8"})


@dataclass
class IngestReport:
    source: str
    stored: int = 0
    updated: int = 0
    dropped: int = 0
    tainted: int = 0
    held: int = 0
    candidates: int = 0
    health_before: str = "ok"
    health_after: str = "ok"
    reputation: float = 0.0
    codes: list[str] = field(default_factory=list)
    snapshot: bool = False


def _h(s: str | None) -> str | None:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32] if s else None


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, cfg: Config, home: Path,
                 now: Callable[[], float] = time.time):
        self.conn = conn
        self.policy = policy
        self.cfg = cfg
        self.now = now
        self.policy_log = JsonlLog(home / "logs" / "policy.jsonl")
        self.rep = {k: float(v) for k, v in policy.get("reputation", default={}).items()}

    # -- source rows ----------------------------------------------------------------------------
    def ensure_source(self, src: SourceConfig, spec: AdapterSpec) -> dict:
        t = src.trust
        tier = src.tier
        tier_cfg = self.policy.tier(tier)
        base = int(src.base_interval_s or spec.base_interval_s)
        mx = int(src.max_interval_s or spec.max_interval_s)
        now = self.now()
        row = one(self.conn, "SELECT * FROM sources WHERE name=?", (src.name,))
        if row is None:
            self.conn.execute(
                """INSERT INTO sources(name, adapter, kind, params, enabled, tier, owner, owner_url, terms_url, licence, legal_basis,
                   rate_limit, expected_content_type, schema_id, reviewed_by, reviewed_at, review_every_days, reputation,
                   interval_s, base_interval_s, max_interval_s, next_due_at, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (src.name, src.adapter, spec.kind, j(src.params), int(src.enabled), tier, t.get("owner"), t.get("owner_url"),
                 t.get("terms_url"), t.get("licence"), src.legal_basis, t.get("rate_limit"), src.expected_content_type,
                 src.schema_id, t.get("reviewed_by"), src.reviewed_at_ts(), int(t.get("review_every_days", 0)),
                 float(tier_cfg["reputation"]), base, base, mx, 0, now, now))
            self.conn.execute("INSERT OR IGNORE INTO source_stats(source, alpha, beta, prior_alpha, prior_beta) VALUES (?,?,?,?,?)",
                              (src.name, 1.0, 1.0, 1.0, 1.0))
        else:
            self.conn.execute(
                """UPDATE sources SET adapter=?, kind=?, params=?, enabled=?, tier=?, owner=?, owner_url=?, terms_url=?, licence=?,
                   legal_basis=?, rate_limit=?, expected_content_type=?, schema_id=?, reviewed_by=?, reviewed_at=?,
                   review_every_days=?, base_interval_s=?, max_interval_s=?, updated_at=? WHERE name=?""",
                (src.adapter, spec.kind, j(src.params), int(src.enabled), tier, t.get("owner"), t.get("owner_url"), t.get("terms_url"),
                 t.get("licence"), src.legal_basis, t.get("rate_limit"), src.expected_content_type, src.schema_id,
                 t.get("reviewed_by"), src.reviewed_at_ts(), int(t.get("review_every_days", 0)), base, mx, now, src.name))
            if row["health"] == "disabled" and src.enabled:
                self.set_health(src.name, "ok", "re-enabled from config")
        return one(self.conn, "SELECT * FROM sources WHERE name=?", (src.name,))  # type: ignore[return-value]

    def source(self, name: str) -> dict | None:
        return one(self.conn, "SELECT * FROM sources WHERE name=?", (name,))

    def set_health(self, name: str, health: str, reason: str) -> None:
        row = self.source(name)
        if row is None:
            return
        if row["health"] != health:
            self.policy_log.write({"event": "health", "source": name, "from": row["health"], "to": health, "reason": reason})
        self.conn.execute("UPDATE sources SET health=?, health_reason=?, updated_at=? WHERE name=?",
                          (health, reason, self.now(), name))

    def scale(self, tier: int) -> float:
        """Reputation deltas and thresholds in policy.yaml are written for Tier 0 (0.90). Lower tiers
        start lower, so everything scales by their default / Tier 0 default to stay proportional."""
        return float(self.policy.tier(tier)["reputation"]) / float(self.policy.tier(0)["reputation"])

    def adjust_reputation(self, name: str, delta: float) -> float:
        row = self.source(name)
        if row is None:
            return 0.0
        tier = int(row["tier"])
        default = float(self.policy.tier(tier)["reputation"])
        cap = default + self.rep.get("cap_above_default", 0.05) * self.scale(tier)
        new = max(0.0, min(cap, float(row["reputation"]) + delta * self.scale(tier)))
        self.conn.execute("UPDATE sources SET reputation=? WHERE name=?", (new, name))
        return new

    def taint_events_24h(self, name: str) -> int:
        r = one(self.conn, "SELECT COUNT(*) AS n FROM taint_events WHERE source=? AND ts>=?", (name, self.now() - 86400))
        return int(r["n"]) if r else 0

    def _taint_event(self, name: str, severity: str, codes: tuple[str, ...]) -> None:
        self.conn.execute("INSERT INTO taint_events(ts, source, severity, codes) VALUES (?,?,?,?)",
                          (self.now(), name, severity, ",".join(codes)))
        self.conn.execute("UPDATE source_stats SET taint_events=taint_events+1 WHERE source=?", (name,))

    def _quarantine(self, name: str, url: str | None, codes: tuple[str, ...], severity: str, excerpt: str | None, action: str) -> None:
        self.conn.execute("INSERT INTO quarantine(ts, source, url_hash, detector_codes, severity, excerpt_hash, action) VALUES (?,?,?,?,?,?,?)",
                          (self.now(), name, _h(url), ",".join(codes), severity, _h(excerpt), action))
        self.policy_log.write({"event": "quarantine", "source": name, "url_hash": _h(url), "codes": list(codes),
                               "severity": severity, "action": action})

    def _reevaluate_health(self, name: str, *, integrity: bool = False, high: bool = False, drift: bool = False) -> str:
        row = self.source(name)
        if row is None:
            return "ok"
        health = row["health"]
        if health in ("disabled", "broken"):
            return health
        rep = float(row["reputation"])
        k = self.scale(int(row["tier"]))
        quarantine_below = self.rep.get("quarantine_below", 0.25) * k
        suspect_below = self.rep.get("suspect_below", 0.40) * k
        events = self.taint_events_24h(name)
        if integrity or rep < quarantine_below or events >= int(self.rep.get("taint_events_24h", 3)):
            reason = "transport integrity failure" if integrity else (f"reputation {rep:.2f}" if rep < quarantine_below
                                                                      else f"{events} taint events in 24h")
            self.set_health(name, "quarantined", reason)
            return "quarantined"
        if health == "quarantined":
            return health  # only `sources review --clear` leaves quarantine
        if high or drift or rep < suspect_below:
            self.set_health(name, "suspect", "high-taint item" if high else ("schema drift" if drift else f"reputation {rep:.2f}"))
            return "suspect"
        return health

    # -- the ingest ladder ------------------------------------------------------------------------
    def integrity_event(self, src: SourceConfig, fetched: Fetched) -> IngestReport:
        codes = detectors.codes(fetched.findings)
        rep = IngestReport(source=src.name, codes=list(codes))
        row = self.source(src.name)
        rep.health_before = row["health"] if row else "ok"
        self._quarantine(src.name, fetched.plan.log_url or fetched.plan.url, codes, "integrity", None, "quarantine_source")
        rep.reputation = self.adjust_reputation(src.name, self.rep.get("integrity", -0.30))
        self.conn.execute("UPDATE source_stats SET parse_fail=parse_fail+1 WHERE source=?", (src.name,))
        rep.health_after = self._reevaluate_health(src.name, integrity=True)
        return rep

    def ingest(self, src: SourceConfig, spec: AdapterSpec, parsed: ParseResult, fetched: Fetched) -> IngestReport:
        name = src.name
        rep = IngestReport(source=name)
        row = self.source(name)
        rep.health_before = row["health"] if row else "ok"
        url = fetched.plan.log_url or fetched.plan.url
        now = self.now()

        doc_findings = parsed.findings
        drift = any(f.code == "schema_drift" for f in doc_findings)
        doc_high = [f for f in doc_findings if f.severity == "high" and f.code != "schema_drift"]
        if doc_high:
            codes = detectors.codes(doc_high)
            rep.codes.extend(codes)
            self._quarantine(name, url, codes, "high", None, "document_flagged")
            self._taint_event(name, "high", codes)
            self.adjust_reputation(name, self.rep.get("taint_high", -0.25))
        if drift:
            rep.codes.append("schema_drift")
            self._quarantine(name, url, ("schema_drift",), "high", None, "schema_drift")
            self.adjust_reputation(name, self.rep.get("schema_drift", -0.10))

        high_items = [i for i in parsed.items if i.taint_severity == "high"]
        medium_items = [i for i in parsed.items if i.taint_severity == "medium"]
        for it in high_items:
            self._quarantine(name, it.url, it.taint_codes, "high", it.text or it.title, "dropped_item")
            self._taint_event(name, "high", it.taint_codes)
            rep.dropped += 1
        if high_items:
            self.adjust_reputation(name, self.rep.get("taint_high", -0.25))
        if medium_items:
            self._taint_event(name, "medium", detectors.codes(detectors.Finding("medium", c) for i in medium_items for c in i.taint_codes))
            self.adjust_reputation(name, self.rep.get("taint_medium", -0.10))
            self.policy_log.write({"event": "tainted_items", "source": name, "count": len(medium_items), "action": "stored_tainted_held"})
        clean_doc = not doc_high and not drift and not high_items
        if clean_doc:
            self.adjust_reputation(name, self.rep.get("parse_ok", 0.01))
            self.conn.execute("UPDATE source_stats SET parse_ok=parse_ok+1 WHERE source=?", (name,))
        else:
            self.conn.execute("UPDATE source_stats SET parse_fail=parse_fail+1 WHERE source=?", (name,))

        health = self._reevaluate_health(name, high=bool(high_items or doc_high), drift=drift)
        hold_all = health in ("suspect", "quarantined")

        for it in parsed.items:
            if it.taint_severity == "high":
                continue
            held = hold_all or it.taint_severity == "medium"
            created = self._store_item(it, src, fetched.fetched_at or now, held=held)
            rep.stored += int(created)
            rep.updated += int(not created)
            rep.tainted += int(bool(it.tainted))
            rep.held += int(held)
        rep.candidates = self._store_candidates(name, parsed.candidates)
        self.conn.execute("UPDATE source_stats SET items_total=items_total+? WHERE source=?", (rep.stored, name))
        if clean_doc and parsed.items:
            self._snapshot(name, [i for i in parsed.items if i.taint_severity is None])
            rep.snapshot = True
        rep.health_after = health
        r = self.source(name)
        rep.reputation = float(r["reputation"]) if r else 0.0
        return rep

    # -- storage ----------------------------------------------------------------------------------
    def _store_item(self, it: Item, src: SourceConfig, fetched_at: float, *, held: bool) -> bool:
        attrs = dict(it.attrs)
        if it.tier is not None:
            attrs["tier"] = it.tier
        attrs_json = j(attrs)
        attrs_hash = hashlib.sha256(attrs_json.encode()).hexdigest()[:16]
        now = self.now()
        existing = one(self.conn, "SELECT id, title, attrs, attrs_hash, held, tainted FROM items WHERE uid=?", (it.uid,))
        if existing is None:
            cur = self.conn.execute(
                """INSERT INTO items(uid, source, kind, title, url, text, published_at, first_seen_at, last_seen_at, attrs, attrs_hash,
                   legal_basis, person_data, tainted, taint_codes, held, fetched_at, source_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it.uid, src.name, it.kind, it.title, it.url, it.text, it.published_at, now, now, attrs_json, attrs_hash,
                 it.legal_basis, int(it.person_data), int(it.tainted), ",".join(it.taint_codes), int(held), fetched_at, it.url))
            item_id = int(cur.lastrowid)
            created = True
        else:
            item_id = int(existing["id"])
            if existing["attrs_hash"] != attrs_hash:
                old = uj(existing["attrs"])
                for k in sorted(set(old) | set(attrs)):
                    if old.get(k) != attrs.get(k):
                        self.conn.execute("INSERT INTO item_changes(item_id, at, field, old, new) VALUES (?,?,?,?,?)",
                                          (item_id, now, k, j(old.get(k)), j(attrs.get(k))))
            if existing["title"] != it.title:
                self.conn.execute("INSERT INTO item_changes(item_id, at, field, old, new) VALUES (?,?,?,?,?)",
                                  (item_id, now, "title", existing["title"], it.title))
            # a previously clean item never becomes un-held by a later suspect fetch; a held one is released on clean ingest
            self.conn.execute(
                """UPDATE items SET title=?, url=?, text=?, published_at=?, last_seen_at=?, attrs=?, attrs_hash=?, tainted=?, taint_codes=?,
                   held=?, fetched_at=? WHERE id=?""",
                (it.title, it.url, it.text, it.published_at, now, attrs_json, attrs_hash, int(it.tainted), ",".join(it.taint_codes),
                 int(held), fetched_at, item_id))
            created = False
        self.conn.execute("DELETE FROM item_dates WHERE item_id=?", (item_id,))
        for d in it.dates:
            self.conn.execute("INSERT OR IGNORE INTO item_dates(item_id, kind, at, end_at) VALUES (?,?,?,?)", (item_id, d.kind, d.at, d.end))
        for e in it.entities:
            eid = self._upsert_entity(e, src, fetched_at)
            if eid:
                self.conn.execute("INSERT OR IGNORE INTO item_entities(item_id, entity_id, role) VALUES (?,?,?)", (item_id, eid, "subject"))
        return created

    def _upsert_entity(self, e, src: SourceConfig, fetched_at: float) -> int | None:
        if e.type == "person":
            fields, _ = self.policy.allow_store({"name": e.name, "title": e.title, "office_city": e.city, "country": e.country,
                                                 "company_domain": e.domain}, source_adapter=src.adapter)
            if not fields:
                return None
            name, title, city, country, domain = (fields.get("name", ""), fields.get("title"), fields.get("office_city"),
                                                  fields.get("country"), fields.get("company_domain"))
        else:
            name, title, city, country, domain = e.name, e.title, e.city, e.country, e.domain
        retention = self.policy.retention_until(fetched_at) if e.type == "person" else None
        self.conn.execute(
            """INSERT INTO entities(type, key, name, title, domain, office_city, country, source, legal_basis, fetched_at, retention_until)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET name=excluded.name, title=COALESCE(excluded.title, entities.title),
                 domain=COALESCE(excluded.domain, entities.domain), office_city=COALESCE(excluded.office_city, entities.office_city),
                 country=COALESCE(excluded.country, entities.country), fetched_at=excluded.fetched_at,
                 retention_until=excluded.retention_until, forgotten_at=NULL""",
            (e.type, e.key, name, title, domain, city, country, src.name, src.legal_basis, fetched_at, retention))
        r = one(self.conn, "SELECT id FROM entities WHERE key=?", (e.key,))
        return int(r["id"]) if r else None

    def _store_candidates(self, source: str, cands: list[tuple[str, str, str]]) -> int:
        n = 0
        now = self.now()
        for host, example, reason in cands:
            host = host.lower()
            if not host or self.policy.host_denied(host):
                continue
            self.conn.execute(
                """INSERT INTO candidates(host, example_url, found_in_source, reason, count, first_seen, last_seen)
                   VALUES (?,?,?,?,1,?,?) ON CONFLICT(host, found_in_source) DO UPDATE SET count=count+1, last_seen=excluded.last_seen""",
                (host, example, source, reason, now, now))
            n += 1
        return n

    def _snapshot(self, source: str, items: list[Item]) -> None:
        payload = [{"uid": i.uid, "kind": i.kind, "title": i.title, "url": i.url, "published_at": i.published_at,
                    "dates": [{"kind": d.kind, "at": d.at, "end": d.end} for d in i.dates], "attrs": i.attrs} for i in items]
        now = self.now()
        self.conn.execute("INSERT INTO snapshots(source, taken_at, items_json, good) VALUES (?,?,?,1)", (source, now, j(payload)))
        keep_days = int(self.policy.get("retention", "snapshots_days", default=30))
        self.conn.execute("DELETE FROM snapshots WHERE source=? AND taken_at<? AND id NOT IN (SELECT id FROM snapshots WHERE source=? ORDER BY taken_at DESC LIMIT 1)",
                          (source, now - keep_days * 86400, source))

    def last_good_snapshot(self, source: str) -> dict | None:
        return one(self.conn, "SELECT * FROM snapshots WHERE source=? AND good=1 ORDER BY taken_at DESC LIMIT 1", (source,))

    # -- review -------------------------------------------------------------------------------------
    def clear(self, name: str) -> dict | None:
        row = self.source(name)
        if row is None:
            return None
        default = float(self.policy.tier(int(row["tier"]))["reputation"])
        new = default * float(self.rep.get("clear_factor", 0.8))
        self.conn.execute("UPDATE sources SET reputation=?, fail_count=0 WHERE name=?", (new, name))
        self.conn.execute("DELETE FROM taint_events WHERE source=?", (name,))
        self.set_health(name, "ok", "cleared by review")
        self.conn.execute("UPDATE items SET held=0 WHERE source=? AND tainted=0", (name,))
        self.policy_log.write({"event": "review_clear", "source": name, "reputation": new})
        return self.source(name)

    def decay(self) -> int:
        """Weekly: reputation moves 10 % toward the tier default. Returns sources touched."""
        n = 0
        now = self.now()
        last = one(self.conn, "SELECT value FROM meta WHERE key='last_decay_at'")
        if last and now - float(last["value"]) < 7 * 86400:
            return 0
        for row in self.conn.execute("SELECT name, tier, reputation FROM sources").fetchall():
            default = float(self.policy.tier(int(row["tier"]))["reputation"])
            new = row["reputation"] + (default - row["reputation"]) * self.rep.get("decay_weekly", 0.10)
            self.conn.execute("UPDATE sources SET reputation=? WHERE name=?", (new, row["name"]))
            n += 1
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_decay_at', ?)", (str(now),))
        return n
