"""Reach adapter 1: hosts the main crawler already logged as link candidates. No network.

The main database is opened read-only (SQLite ``mode=ro``), so the explorer can never write to it, and a main
crawler that is busy writing only delays this read."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ...db import DB_NAME
from ...policy import check_url, host_of
from ..store import Candidate


def open_main_readonly(home: Path) -> sqlite3.Connection:
    path = home / DB_NAME
    if not path.exists():
        raise FileNotFoundError(f"no main database at {path}: run `ninshubur run` once first, or set NINSHUBUR_HOME "
                                "to the folder that holds ninshubur.db")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def logged_links(main: sqlite3.Connection) -> list[Candidate]:
    """One candidate per logged host. The URL is the logged example when it is a fetchable URL on that host (its
    page is the evidence), else the host's root."""
    out: list[Candidate] = []
    for r in main.execute("SELECT host, example_url, found_in_source, reason FROM candidates "
                          "WHERE status='new' ORDER BY id"):
        host = (r["host"] or "").lower().strip(".")
        if not host:
            continue
        ex = r["example_url"] or ""
        url = ex if ex and host_of(ex) == host and not check_url(ex) else f"https://{host}/"
        out.append(Candidate(url=url, kind="logged_link", via=r["found_in_source"], reason=r["reason"] or ""))
    return out
