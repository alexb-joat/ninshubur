"""SQLite store: schema, ordered migrations, 0600 file, WAL, parameterised access only."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .home import data_home

DB_NAME = "ninshubur.db"

SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sources (
        name TEXT PRIMARY KEY, adapter TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        tier INTEGER NOT NULL, owner TEXT, owner_url TEXT, terms_url TEXT, licence TEXT, legal_basis TEXT,
        rate_limit TEXT, expected_content_type TEXT, schema_id TEXT, reviewed_by TEXT, reviewed_at REAL,
        review_every_days INTEGER, reputation REAL NOT NULL,
        interval_s INTEGER NOT NULL, base_interval_s INTEGER NOT NULL, max_interval_s INTEGER NOT NULL,
        next_due_at REAL NOT NULL DEFAULT 0, last_run_at REAL, last_change_at REAL, fail_count INTEGER NOT NULL DEFAULT 0,
        health TEXT NOT NULL DEFAULT 'ok', health_reason TEXT, state TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fetch_state (
        key TEXT PRIMARY KEY, url TEXT NOT NULL, etag TEXT, last_modified TEXT, content_hash TEXT,
        last_fetched_at REAL, last_changed_at REAL, size INTEGER)""",
    """CREATE TABLE IF NOT EXISTS fetches (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL, status INTEGER,
        bytes INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER,
        conditional INTEGER NOT NULL DEFAULT 0, boosted INTEGER NOT NULL DEFAULT 0, error TEXT)""",
    """CREATE INDEX IF NOT EXISTS fetches_source_ts ON fetches(source, ts)""",
    """CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, taken_at REAL NOT NULL, items_json TEXT NOT NULL,
        good INTEGER NOT NULL DEFAULT 1)""",
    """CREATE INDEX IF NOT EXISTS snapshots_source ON snapshots(source, taken_at)""",
    """CREATE TABLE IF NOT EXISTS quarantine (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL, url_hash TEXT, detector_codes TEXT NOT NULL,
        severity TEXT NOT NULL, excerpt_hash TEXT, action TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS candidates (
        id INTEGER PRIMARY KEY, host TEXT NOT NULL, example_url TEXT, found_in_source TEXT NOT NULL, reason TEXT,
        count INTEGER NOT NULL DEFAULT 1, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'new', UNIQUE(host, found_in_source))""",
    """CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL, url TEXT, text TEXT NOT NULL DEFAULT '', published_at REAL,
        first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL, attrs TEXT NOT NULL DEFAULT '{}', attrs_hash TEXT,
        legal_basis TEXT NOT NULL, person_data INTEGER NOT NULL DEFAULT 0,
        tainted INTEGER NOT NULL DEFAULT 0, taint_codes TEXT NOT NULL DEFAULT '', held INTEGER NOT NULL DEFAULT 0,
        relevance REAL, relevance_parts TEXT, profile TEXT, tier_b TEXT, fetched_at REAL, source_url TEXT)""",
    """CREATE INDEX IF NOT EXISTS items_source_kind ON items(source, kind)""",
    """CREATE INDEX IF NOT EXISTS items_published ON items(published_at)""",
    """CREATE TABLE IF NOT EXISTS item_dates (
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, kind TEXT NOT NULL, at REAL NOT NULL, end_at REAL,
        UNIQUE(item_id, kind, at))""",
    """CREATE TABLE IF NOT EXISTS item_changes (
        id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, at REAL NOT NULL, field TEXT NOT NULL, old TEXT, new TEXT)""",
    """CREATE TABLE IF NOT EXISTS entities (
        id INTEGER PRIMARY KEY, type TEXT NOT NULL, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, title TEXT,
        company_id INTEGER, domain TEXT, office_city TEXT, country TEXT, tz TEXT, source TEXT NOT NULL, source_ref TEXT,
        source_url TEXT, legal_basis TEXT NOT NULL, fetched_at REAL NOT NULL, retention_until REAL, forgotten_at REAL,
        attrs TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS item_entities (
        item_id INTEGER NOT NULL, entity_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'subject',
        UNIQUE(item_id, entity_id, role))""",
    """CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, entity_key TEXT NOT NULL, at REAL NOT NULL,
        window_start REAL, window_end REAL, item_id INTEGER, source TEXT NOT NULL, confidence REAL NOT NULL,
        UNIQUE(kind, entity_key, at, source))""",
    """CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, profile TEXT, title TEXT NOT NULL,
        steps TEXT NOT NULL, reasons TEXT NOT NULL, evidence TEXT NOT NULL, confidence REAL NOT NULL,
        est_value REAL NOT NULL, est_cost REAL NOT NULL, cost_parts TEXT NOT NULL DEFAULT '{}',
        deadline REAL, best_window_start REAL, best_window_end REAL, expires_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'new', held_reason TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        entity_keys TEXT NOT NULL DEFAULT '[]', item_ids TEXT NOT NULL DEFAULT '[]')""",
    """CREATE INDEX IF NOT EXISTS suggestions_status ON suggestions(status, expires_at)""",
    """CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY, suggestion_id INTEGER NOT NULL, at REAL NOT NULL, status TEXT NOT NULL, reason TEXT,
        outcome TEXT, minutes_spent INTEGER)""",
    """CREATE TABLE IF NOT EXISTS sink_refs (
        suggestion_id INTEGER NOT NULL, sink TEXT NOT NULL, external_id TEXT NOT NULL, pushed_at REAL NOT NULL,
        PRIMARY KEY(suggestion_id, sink))""",
    """CREATE TABLE IF NOT EXISTS budget_ledger (
        id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, amount REAL NOT NULL, ref TEXT)""",
    """CREATE INDEX IF NOT EXISTS budget_kind_ts ON budget_ledger(kind, ts)""",
    """CREATE TABLE IF NOT EXISTS source_stats (
        source TEXT PRIMARY KEY, alpha REAL NOT NULL, beta REAL NOT NULL, prior_alpha REAL NOT NULL, prior_beta REAL NOT NULL,
        items_total INTEGER NOT NULL DEFAULT 0, items_relevant INTEGER NOT NULL DEFAULT 0,
        accepted INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0,
        parse_ok INTEGER NOT NULL DEFAULT 0, parse_fail INTEGER NOT NULL DEFAULT 0, taint_events INTEGER NOT NULL DEFAULT 0,
        last_reward_at REAL, last_decay_at REAL)""",
    """CREATE TABLE IF NOT EXISTS boosts (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, reason TEXT NOT NULL, requested_at REAL NOT NULL,
        run_after REAL NOT NULL, done_at REAL, trigger_item INTEGER)""",
    """CREATE TABLE IF NOT EXISTS robots (
        host TEXT PRIMARY KEY, body TEXT NOT NULL DEFAULT '', fetched_at REAL NOT NULL, status INTEGER, crawl_delay REAL)""",
    """CREATE TABLE IF NOT EXISTS geocache (q TEXT PRIMARY KEY, lat REAL, lon REAL, display TEXT, fetched_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS embeddings (item_id INTEGER NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL,
        PRIMARY KEY(item_id, model))""",
    """CREATE TABLE IF NOT EXISTS calibration (kind TEXT PRIMARY KEY, multiplier REAL NOT NULL DEFAULT 1.0,
        accepted INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0, won INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS taint_events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,
        severity TEXT NOT NULL, codes TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS taint_events_source_ts ON taint_events(source, ts)""",
]

# (table, statement) pairs applied once each, in order, after SCHEMA. Add to the end only.
MIGRATIONS: list[tuple[str, str]] = [
    ("entities", "ALTER TABLE entities ADD COLUMN handle TEXT"),
    ("entities", "ALTER TABLE entities ADD COLUMN seen_events INTEGER NOT NULL DEFAULT 0"),
    ("entities", "ALTER TABLE entities ADD COLUMN company TEXT"),
    ("entities", "ALTER TABLE entities ADD COLUMN role TEXT"),
]

SCHEMA_VERSION = 2


def db_path(home: Path | None = None) -> Path:
    return (home or data_home()) / DB_NAME


def _private(path: Path) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def connect(home: Path | None = None, path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    if not existed:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            _private(p)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    with conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        done = {r["value"] for r in conn.execute("SELECT value FROM meta WHERE key LIKE 'migration:%'")}
        for i, (table, stmt) in enumerate(MIGRATIONS):
            tag = f"{i}:{table}"
            if tag in done:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"migration:{i}", tag))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


# --- small helpers used across modules ------------------------------------------------------
def j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_default)


def _default(o: Any) -> Any:
    if isinstance(o, (set, frozenset, tuple)):
        return sorted(o) if isinstance(o, (set, frozenset)) else list(o)
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


def uj(s: str | None, default: Any = None) -> Any:
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except ValueError:
        return default if default is not None else {}


def ledger_add(conn: sqlite3.Connection, kind: str, amount: float, ref: str | None = None, ts: float | None = None) -> None:
    conn.execute("INSERT INTO budget_ledger(ts, kind, amount, ref) VALUES (?,?,?,?)",
                 (ts if ts is not None else time.time(), kind, float(amount), ref))


def ledger_sum(conn: sqlite3.Connection, kind: str, since: float) -> float:
    row = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM budget_ledger WHERE kind=? AND ts>=?", (kind, since)).fetchone()
    return float(row["s"]) if row else 0.0


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None
