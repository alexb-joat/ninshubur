"""The explorer's own SQLite store: the main schema (so the shared ``Fetcher`` can keep its ledger, robots cache and
fetch log here) plus the explorer's tables. A separate file, so the explorer never holds a lock the main crawler
waits on, and deleting the folder resets the explorer without touching ``ninshubur.db``."""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import db
from ..policy import check_url, host_of

DB_NAME = "explorer.db"

KINDS = ("logged_link", "hn", "github", "mastodon", "board")
# candidate -> probed -> trial -> admitted; rejected and quarantined are terminal until Alex (or a re-trial) moves them
STATUSES = ("candidate", "probed", "trial", "admitted", "rejected", "quarantined")

SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS explorer_candidates (
        id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, host TEXT NOT NULL, kind TEXT NOT NULL, via TEXT NOT NULL,
        profile TEXT, query TEXT, reason TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL DEFAULT 1,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
        status_reason TEXT, status_at REAL)""",
    "CREATE INDEX IF NOT EXISTS explorer_candidates_host ON explorer_candidates(host)",
    "CREATE INDEX IF NOT EXISTS explorer_candidates_status ON explorer_candidates(status)",
]


def explorer_home(home: Path) -> Path:
    d = home / "explorer"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def disk_use(home: Path, cap_files: int = 100_000) -> int:
    """Bytes in the explorer's folder (symlinks not followed; at most ``cap_files`` entries are counted)."""
    total, seen = 0, 0
    for root, _dirs, files in os.walk(explorer_home(home), followlinks=False):
        for name in files:
            seen += 1
            if seen > cap_files:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def connect(home: Path) -> sqlite3.Connection:
    conn = db.connect(path=explorer_home(home) / DB_NAME)
    with conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
    return conn


@dataclass(frozen=True)
class Candidate:
    """A URL worth asking about. ``via`` names what produced it (a main-crawler source, an API, a board probe);
    ``query`` is the templated search that found it, never text taken from a page."""
    url: str
    kind: str
    via: str
    profile: str | None = None
    query: str | None = None
    reason: str = ""


@dataclass
class Added:
    new: int = 0
    seen_again: int = 0
    refused: int = 0


def add_candidates(conn: sqlite3.Connection, cands: Iterable[Candidate], now: float) -> Added:
    """Insert candidates; a URL seen again only bumps its count and last_seen (its status never changes here).
    URLs that fail the shared URL check (scheme, userinfo, IP literal, port, ...) are counted and dropped."""
    out = Added()
    with conn:
        for c in cands:
            if c.kind not in KINDS:
                raise ValueError(f"unknown candidate kind {c.kind!r}; one of {', '.join(KINDS)}")
            if check_url(c.url):
                out.refused += 1
                continue
            cur = conn.execute(
                """INSERT INTO explorer_candidates(url, host, kind, via, profile, query, reason, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET count=count+1, last_seen=excluded.last_seen
                   RETURNING count""",
                (c.url, host_of(c.url), c.kind, c.via, c.profile, c.query, c.reason[:200], now, now))
            if cur.fetchone()["count"] == 1:
                out.new += 1
            else:
                out.seen_again += 1
    return out


def set_status(conn: sqlite3.Connection, cid: int, status: str, reason: str, now: float) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; one of {', '.join(STATUSES)}")
    with conn:
        conn.execute("UPDATE explorer_candidates SET status=?, status_reason=?, status_at=? WHERE id=?",
                     (status, reason[:200], now, cid))
