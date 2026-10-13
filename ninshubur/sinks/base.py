"""Sinks receive accepted suggestions. They are idempotent via ``sink_refs`` and never delete
or move anything remote. Held suggestions never reach a sink."""
from __future__ import annotations

import sqlite3
import time
from typing import Protocol

from ..db import one


class Sink(Protocol):
    name: str

    def push(self, suggestion: dict, event: str) -> str | None:
        """Return an external id (or None if nothing was created)."""


def already_pushed(conn: sqlite3.Connection, suggestion_id: int, sink: str) -> str | None:
    r = one(conn, "SELECT external_id FROM sink_refs WHERE suggestion_id=? AND sink=?", (suggestion_id, sink))
    return r["external_id"] if r else None


def record_push(conn: sqlite3.Connection, suggestion_id: int, sink: str, external_id: str, now: float | None = None) -> None:
    conn.execute("INSERT OR REPLACE INTO sink_refs(suggestion_id, sink, external_id, pushed_at) VALUES (?,?,?,?)",
                 (suggestion_id, sink, external_id, now if now is not None else time.time()))


def dispatch(conn: sqlite3.Connection, sinks: list, suggestion: dict, event: str, now: float | None = None) -> list[tuple[str, str]]:
    if suggestion.get("status") == "held" or suggestion.get("held_reason"):
        return []
    done = []
    for sink in sinks:
        if already_pushed(conn, suggestion["id"], sink.name) and event == "accept":
            continue
        ext = sink.push(suggestion, event)
        if ext:
            record_push(conn, suggestion["id"], sink.name, ext, now)
            done.append((sink.name, ext))
    return done
