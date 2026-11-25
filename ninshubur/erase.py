"""Erasure: ``ninshubur forget person|company`` and the retention sweep.

``forget`` deletes the stored record and everything that points at it, takes the name out of the texts
Ninshubur wrote (suggestions, feedback notes, qualify reasons), and puts a salted hash of the name on the
``forgotten`` list, so the next crawl does not collect it again. The list holds hashes only; the log line
holds a hash and counts, never the name.

What it does not do, and says so in its preview: it does not rewrite the operator's own files (exported
digests, CSVs and label sheets, config and profiles; they are listed), it does not edit crawled text that
mentions a person's name (the count is shown), and forgetting a company leaves the records of people who
name it as their employer (counted; each is a person to forget by name). A person is matched by name, so
a namesake is forgotten and blocked too.

The sweep enforces policy ``retention``: person rows past ``retention_until``, snapshots past
``snapshots_days`` (the newest of each source stays), quarantine rows past ``quarantine_days``.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .companies import Resolver, company_domain, domain_key, name_key, normalise_name
from .db import j, one, rows, uj
from .logs import JsonlLog

REMOVED = "[removed]"
TYPES = ("person", "company")
DAY = 86400.0
# Texts Ninshubur wrote that can carry a name: (table, column, holds JSON).
_TEXTS = (("suggestions", "title", False), ("suggestions", "steps", True), ("suggestions", "reasons", True),
          ("suggestions", "evidence", True), ("feedback", "reason", False), ("feedback", "outcome", False),
          ("qualifications", "reason", False))
# Crawled text, searched for a person's name and counted, never changed.
_CRAWLED = (("items", "title"), ("items", "text"), ("items", "attrs"), ("item_changes", "old"), ("item_changes", "new"),
            ("snapshots", "items_json"))
_EXPORT_SCAN_MAX = 20_000_000


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def person_norm(name: object) -> str:
    """Casefold + NFKC, punctuation and symbols to spaces, whitespace collapsed: "  Jane  DOE-Smith " and
    "jane doe smith" are one name."""
    s = unicodedata.normalize("NFKC", str(name or "")).casefold()
    return " ".join("".join(" " if unicodedata.category(ch)[0] in "PSZC" else ch for ch in s).split())


class Forgotten:
    """The do-not-collect list: salted hashes of forgotten names. The salt is made once per database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._salt: str | None = None

    def _mark(self, etype: str, value: str) -> str:
        if self._salt is None:
            row = one(self.conn, "SELECT value FROM meta WHERE key='forget_salt'")
            if row is None:
                self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('forget_salt', ?)", (secrets.token_hex(16),))
                row = one(self.conn, "SELECT value FROM meta WHERE key='forget_salt'")
            self._salt = str(row["value"])
        return hashlib.sha256(f"{self._salt}|{etype}|{value}".encode()).hexdigest()[:32]

    def marks(self, etype: str, names: Any = (), domains: Any = (), keys: Any = ()) -> list[str]:
        if etype == "person":
            values = {f"name:{n}" for n in (person_norm(x) for x in names) if n}
        else:
            values = {f"name:{n}" for n in (normalise_name(x) for x in names) if n}
            values |= {f"domain:{d}" for d in (company_domain(x) for x in domains) if d}
            values |= {f"key:{k}" for k in keys if k}
        return sorted(self._mark(etype, v) for v in values)

    def any(self) -> bool:
        return one(self.conn, "SELECT 1 AS x FROM forgotten LIMIT 1") is not None

    def has(self, etype: str, *, name: object = None, domain: object = None, key: object = None) -> bool:
        if not self.any():  # the usual case: one cheap read, no hashing
            return False
        marks = self.marks(etype, [name] if name else (), [domain] if domain else (), [key] if key else ())
        if not marks:
            return False
        return one(self.conn, f"SELECT 1 AS x FROM forgotten WHERE mark IN ({','.join('?' * len(marks))}) LIMIT 1", marks) is not None  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters

    def blocks(self, item: Any) -> bool:
        """True for a hiring item whose company is forgotten: the item is the company's record, so it is not stored."""
        if getattr(item, "kind", None) != "hiring":
            return False
        attrs = getattr(item, "attrs", None) or {}
        if attrs.get("company") and self.has("company", name=attrs.get("company"), domain=attrs.get("domain")):
            return True
        return any(e.type == "company" and self.has("company", name=e.name, domain=e.domain, key=e.key)
                   for e in getattr(item, "entities", ()))

    def add(self, etype: str, marks: list[str], now: float) -> int:
        n = 0
        for m in marks:
            n += self.conn.execute("INSERT OR IGNORE INTO forgotten(mark, type, at) VALUES (?,?,?)", (m, etype, now)).rowcount
        return n

    def lift(self, marks: list[str]) -> int:
        if not marks:
            return 0
        return self.conn.execute(f"DELETE FROM forgotten WHERE mark IN ({','.join('?' * len(marks))})", marks).rowcount  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters

    def counts(self) -> dict[str, int]:
        return {r["type"]: int(r["n"]) for r in rows(self.conn, "SELECT type, COUNT(*) AS n FROM forgotten GROUP BY type")}


@dataclass
class Target:
    """What one ``forget`` would remove. Built by :func:`find`, shown as the preview, applied by :func:`forget`."""
    etype: str
    query: str
    names: list[str] = field(default_factory=list)          # every stored spelling, plus the query
    entities: list[dict] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)           # company keys (canonical and per entity)
    domains: list[str] = field(default_factory=list)
    item_ids: list[int] = field(default_factory=list)       # a company's hiring items
    sources: list[str] = field(default_factory=list)        # where those items came from
    links: int = 0
    suggestions: list[int] = field(default_factory=list)    # about the company itself: become 'redacted'
    signals: int = 0
    aliases: int = 0
    qualifications: int = 0
    texts: int = 0                                          # written texts that name it
    crawled_mentions: int = 0                               # person only: crawled text naming it, left as it is
    staff_rows: int = 0                                     # company only: person records naming it as employer, left
    already_forgotten: bool = False

    @property
    def stored(self) -> bool:
        return bool(self.entities or self.item_ids or self.suggestions or self.signals or self.aliases
                    or self.qualifications or self.texts)


def _name_re(names: list[str]) -> re.Pattern[str] | None:
    usable = sorted({n.strip() for n in names if len(n.strip()) >= 3}, key=len, reverse=True)
    if not usable:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in usable) + r")(?!\w)", re.I)


def _like(conn: sqlite3.Connection, table: str, col: str, names: list[str]) -> list[dict]:
    usable = sorted({n.strip() for n in names if len(n.strip()) >= 3})
    if not usable:
        return []
    where = " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for _ in usable)
    args = ["%" + n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for n in usable]
    return rows(conn, f"SELECT id, {col} AS v FROM {table} WHERE {col} IS NOT NULL AND ({where})", args)  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters


def _scrub(value: Any, rx: re.Pattern[str]) -> Any:
    if isinstance(value, str):
        return rx.sub(REMOVED, value)
    if isinstance(value, list):
        return [_scrub(v, rx) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, rx) for k, v in value.items()}
    return value


def _named_texts(conn: sqlite3.Connection, names: list[str], skip_suggestions: set[int]) -> list[tuple[str, str, bool, int, str]]:
    """(table, column, is_json, row id, new value) for each written text that names one of ``names``."""
    rx = _name_re(names)
    if rx is None:
        return []
    out = []
    for table, col, is_json in _TEXTS:
        for r in _like(conn, table, col, names):
            if table == "suggestions" and r["id"] in skip_suggestions:
                continue
            new = j(_scrub(uj(r["v"], []), rx)) if is_json else rx.sub(REMOVED, r["v"])
            if new != r["v"]:
                out.append((table, col, is_json, int(r["id"]), new))
    return out


def find(conn: sqlite3.Connection, etype: str, query: str, resolver: Resolver | None = None) -> Target:
    """Everything stored about one person or company. Reads only."""
    if etype not in TYPES:
        raise ValueError(f"forget takes 'person' or 'company', not {etype!r}")
    query = " ".join(str(query or "").split())
    t = Target(etype=etype, query=query)
    fg = Forgotten(conn)
    if etype == "person":
        want = person_norm(query)
        t.entities = [e for e in rows(conn, "SELECT * FROM entities WHERE type='person' ORDER BY id")
                      if e["key"] == query or (want and person_norm(e["name"]) == want)]
        t.names = sorted({e["name"] for e in t.entities} | {query})
        t.already_forgotten = fg.has("person", name=query)
    else:
        qnorm, qdomain = normalise_name(query), company_domain(query)
        keys = {k for k in (name_key(qnorm) if qnorm else None, domain_key(qdomain) if qdomain else None, query) if k}
        names, domains = {query}, {qdomain} if qdomain else set()
        resolved = []
        if resolver is not None:
            for it in rows(conn, "SELECT id, source, attrs FROM items WHERE kind='hiring' ORDER BY id"):
                r = resolver.resolve(it)
                if r is not None:
                    resolved.append((it, r))
        ents = rows(conn, "SELECT * FROM entities WHERE type='company' ORDER BY id")
        hit_items: dict[int, dict] = {}
        hit_ents: dict[int, dict] = {}
        for _ in range(3):  # a spelling can lead to a domain and the domain to more spellings: settle in a few rounds
            before = (len(hit_items), len(hit_ents), len(keys))
            for it, r in resolved:
                if r.key in keys or (qnorm and r.norm == qnorm) or (r.domain and r.domain in domains):
                    hit_items[int(it["id"])] = it
                    keys.add(r.key)
                    names.add(r.name)
                    if r.domain:
                        domains.add(r.domain)
                    raw = str(uj(it["attrs"]).get("company") or "").strip()
                    if raw:
                        names.add(raw)
            for e in ents:
                if e["key"] in keys or (qnorm and normalise_name(e["name"]) == qnorm) or (e["domain"] and e["domain"] in domains):
                    hit_ents[int(e["id"])] = e
                    keys.add(e["key"])
                    names.add(e["name"])
                    if e["domain"] and company_domain(e["domain"]):
                        domains.add(company_domain(e["domain"]))
            if (len(hit_items), len(hit_ents), len(keys)) == before:
                break
        t.entities = list(hit_ents.values())
        t.item_ids = sorted(hit_items)
        t.sources = sorted({str(it["source"]) for it in hit_items.values()})
        t.keys, t.domains, t.names = sorted(keys), sorted(domains), sorted(n for n in names if n)
        ph = ",".join("?" * len(t.keys))
        t.signals = int(one(conn, f"SELECT COUNT(*) AS n FROM signals WHERE entity_key IN ({ph})", t.keys)["n"])  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
        t.aliases = int(one(conn, f"SELECT COUNT(*) AS n FROM company_aliases WHERE company_key IN ({ph})", t.keys)["n"])  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
        t.qualifications = int(one(conn, f"SELECT COUNT(*) AS n FROM qualifications WHERE subject_key IN ({ph})", t.keys)["n"])  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
        keyset = set(t.keys)
        t.suggestions = [int(s["id"]) for s in rows(conn, "SELECT id, entity_keys FROM suggestions WHERE status != 'redacted'")
                         if keyset & {str(k) for k in uj(s["entity_keys"], [])}]
        t.already_forgotten = fg.has("company", name=query, domain=qdomain)
        norms = {normalise_name(n) for n in t.names} - {""}
        t.staff_rows = sum(1 for p in rows(conn, "SELECT company FROM entities WHERE type='person' AND company IS NOT NULL")
                          if normalise_name(p["company"]) in norms)
    ids = [int(e["id"]) for e in t.entities]
    if ids:
        t.links = int(one(conn, f"SELECT COUNT(*) AS n FROM item_entities WHERE entity_id IN ({','.join('?' * len(ids))})", ids)["n"])  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
    t.texts = len(_named_texts(conn, t.names, set(t.suggestions)))
    if etype == "person":
        rx = _name_re(t.names)
        if rx is not None:
            t.crawled_mentions = sum(1 for table, col in _CRAWLED for r in _like(conn, table, col, t.names) if rx.search(r["v"]))
    return t


def _flush(conn: sqlite3.Connection) -> None:
    """Fold the write-ahead log into the database and empty it, so the old rows are not left in the -wal file.
    Best effort: a reader in another process can hold part of the log; its next checkpoint finishes the job."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def _delete_in(conn: sqlite3.Connection, table: str, col: str, values: list) -> int:
    n = 0
    for i in range(0, len(values), 500):
        chunk = values[i:i + 500]
        n += conn.execute(f"DELETE FROM {table} WHERE {col} IN ({','.join('?' * len(chunk))})", chunk).rowcount  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
    return n


def _apply(conn: sqlite3.Connection, t: Target, now: float) -> dict:
    """Delete what ``t`` names. The caller holds the transaction."""
    done = {"entities": 0, "links": 0, "items": 0, "signals": 0, "aliases": 0, "qualifications": 0, "suggestions": 0,
            "texts": 0, "snapshots": 0}
    ids = [int(e["id"]) for e in t.entities]
    done["links"] = _delete_in(conn, "item_entities", "entity_id", ids)
    done["entities"] = _delete_in(conn, "entities", "id", ids)
    if t.etype == "company":
        if t.item_ids:
            uids = set()
            for i in range(0, len(t.item_ids), 500):
                chunk = t.item_ids[i:i + 500]
                uids |= {r["uid"] for r in rows(conn, f"SELECT uid FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk)}  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
            for table in ("item_dates", "item_changes", "item_entities", "embeddings", "signals"):
                _delete_in(conn, table, "item_id", t.item_ids)
            done["items"] = _delete_in(conn, "items", "id", t.item_ids)
            for src in t.sources:  # last-good snapshots list a source's items: drop this company's entries
                for snap in rows(conn, "SELECT id, items_json FROM snapshots WHERE source=?", (src,)):
                    payload = uj(snap["items_json"], [])
                    kept = [p for p in payload if not (isinstance(p, dict) and p.get("uid") in uids)]
                    if len(kept) != len(payload):
                        conn.execute("UPDATE snapshots SET items_json=? WHERE id=?", (j(kept), snap["id"]))
                        done["snapshots"] += 1
        done["signals"] = _delete_in(conn, "signals", "entity_key", t.keys)
        done["aliases"] = _delete_in(conn, "company_aliases", "company_key", t.keys)
        done["qualifications"] = _delete_in(conn, "qualifications", "subject_key", t.keys)
        for sid in t.suggestions:
            done["suggestions"] += conn.execute(
                """UPDATE suggestions SET status='redacted', title=?, steps='[]', reasons='[]', evidence='[]', entity_keys='[]',
                   item_ids='[]', held_reason=NULL, updated_at=? WHERE id=?""", (REMOVED, now, sid)).rowcount
    for table, col, _is_json, rid, new in _named_texts(conn, t.names, set()):
        conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (new, rid))  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
        done["texts"] += 1
    return done


def forget(conn: sqlite3.Connection, t: Target, now: float, log: JsonlLog | None = None) -> dict:
    """Apply ``t`` and put its names on the forgotten list, in one transaction. Logs a hash and counts."""
    fg = Forgotten(conn)
    with _tx(conn):
        marks = fg.marks(t.etype, t.names, t.domains, t.keys)
        done = _apply(conn, t, now)
        done["listed"] = fg.add(t.etype, marks, now)
    _flush(conn)
    if log is not None:
        log.write({"event": "forget", "type": t.etype, "mark": (fg.marks(t.etype, [t.query]) or [None])[0], **done})
    return done


def lift(conn: sqlite3.Connection, etype: str, query: str, log: JsonlLog | None = None) -> int:
    """Take a name off the forgotten list (a typing mistake). Restores nothing; the next crawl may collect it again."""
    fg = Forgotten(conn)
    query = " ".join(str(query or "").split())
    marks = fg.marks(etype, [query], [query] if etype == "company" else (),
                     [k for k in (name_key(normalise_name(query)) if normalise_name(query) else None,
                                  domain_key(company_domain(query)) if company_domain(query) else None, query) if k]
                     if etype == "company" else ())
    with _tx(conn):
        n = fg.lift(marks)
    if n and log is not None:
        log.write({"event": "forget_lifted", "type": etype, "mark": (fg.marks(etype, [query]) or [None])[0], "marks": n})
    return n


def exported_mentions(home: Path, names: list[str]) -> list[Path]:
    """Files that still show one of the names: exports under ``feeds/`` and the YAML files in the data home
    (config, profiles, aliases). They are the operator's own: listed, not changed."""
    rx = _name_re(names)
    if rx is None:
        return []
    home = Path(home)
    out = []
    for p in sorted(home.glob("*.y*ml")) + (sorted((home / "feeds").rglob("*")) if (home / "feeds").is_dir() else []):
        try:
            if p.is_file() and p.stat().st_size <= _EXPORT_SCAN_MAX and rx.search(p.read_text(encoding="utf-8", errors="ignore")):
                out.append(p)
        except OSError:
            continue
    return out


# --- retention sweep ---------------------------------------------------------------------------------
def sweep(conn: sqlite3.Connection, policy: Any, now: float, log: JsonlLog | None = None) -> dict:
    """Delete what policy ``retention`` says is too old. Idempotent. Records when it ran in ``meta``."""
    snap_days = int(policy.get("retention", "snapshots_days", default=30))
    quar_days = int(policy.get("retention", "quarantine_days", default=90))
    with _tx(conn):
        expired = rows(conn, "SELECT * FROM entities WHERE type='person' AND retention_until IS NOT NULL AND retention_until < ?", (now,))
        t = Target(etype="person", query="", entities=expired, names=sorted({e["name"] for e in expired}))
        done = _apply(conn, t, now) if expired else {"entities": 0, "links": 0, "texts": 0}
        snapshots = conn.execute(
            """DELETE FROM snapshots WHERE taken_at < ? AND id NOT IN
               (SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY source ORDER BY taken_at DESC, id DESC) AS n
                                FROM snapshots) WHERE n = 1)""", (now - snap_days * DAY,)).rowcount
        quarantine = conn.execute("DELETE FROM quarantine WHERE ts < ?", (now - quar_days * DAY,)).rowcount
        report = {"persons": done["entities"], "links": done["links"], "texts": done["texts"], "snapshots": snapshots,
                  "quarantine": quarantine}
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sweep_at', ?)", (repr(float(now)),))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sweep', ?)", (j(report),))
    if any(report.values()):
        _flush(conn)
        if log is not None:
            log.write({"event": "retention_sweep", **report})
    return report


def last_sweep(conn: sqlite3.Connection) -> tuple[float | None, dict]:
    at = one(conn, "SELECT value FROM meta WHERE key='last_sweep_at'")
    rep = one(conn, "SELECT value FROM meta WHERE key='last_sweep'")
    try:
        return (float(at["value"]) if at else None), (uj(rep["value"]) if rep else {})
    except ValueError:
        return None, {}


def sweep_if_due(conn: sqlite3.Connection, policy: Any, now: float, log: JsonlLog | None = None) -> dict | None:
    """Run :func:`sweep` when ``retention.sweep_every_s`` (default one hour) has passed since the last one."""
    every = float(policy.get("retention", "sweep_every_s", default=3600))
    at, _ = last_sweep(conn)
    if at is not None and 0 <= now - at < every:
        return None
    return sweep(conn, policy, now, log)
