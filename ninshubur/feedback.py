"""Human decisions on suggestions. The only way a suggestion changes status besides expiry."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

from .analyze.suggest import _hydrate
from .db import one
from .sinks.base import dispatch

OUTCOMES = ("won", "lost", "meeting", "reply", "none")


class FeedbackError(Exception):
    pass


class Feedback:
    def __init__(self, conn: sqlite3.Connection, sinks: list, now: Callable[[], float] = time.time):
        self.conn = conn
        self.sinks = sinks
        self.now = now

    def _get(self, sid: int) -> dict:
        r = one(self.conn, "SELECT * FROM suggestions WHERE id=?", (sid,))
        if r is None:
            raise FeedbackError(f"no suggestion {sid}")
        return _hydrate(r)

    def _set(self, sid: int, status: str, reason: str | None = None, outcome: str | None = None, minutes: int | None = None) -> dict:
        now = self.now()
        self.conn.execute("UPDATE suggestions SET status=?, updated_at=? WHERE id=?", (status, now, sid))
        self.conn.execute("INSERT INTO feedback(suggestion_id, at, status, reason, outcome, minutes_spent) VALUES (?,?,?,?,?,?)",
                          (sid, now, status, reason, outcome, minutes))
        if minutes:
            self.conn.execute("INSERT INTO budget_ledger(ts, kind, amount, ref) VALUES (?,?,?,?)", (now, "human_minutes", minutes, f"suggestion:{sid}"))
        return self._get(sid)

    def _calibrate(self, kind: str, col: str) -> None:
        self.conn.execute(f"INSERT INTO calibration(kind, multiplier, {col}, updated_at) VALUES (?, 1.0, 1, ?) "
                          f"ON CONFLICT(kind) DO UPDATE SET {col}={col}+1, updated_at=excluded.updated_at", (kind, self.now()))

    def accept(self, sid: int) -> tuple[dict, list[tuple[str, str]]]:
        s = self._get(sid)
        if s["status"] == "held":
            raise FeedbackError(f"suggestion {sid} is held ({s['held_reason']}); review the source first")
        if s["status"] in ("expired", "redacted"):
            raise FeedbackError(f"suggestion {sid} is {s['status']}")
        if s["status"] == "accepted":
            return s, dispatch(self.conn, self.sinks, s, "accept", self.now())  # idempotent
        s = self._set(sid, "accepted")
        self._calibrate(s["kind"], "accepted")
        return s, dispatch(self.conn, self.sinks, s, "accept", self.now())

    def dismiss(self, sid: int, reason: str | None = None) -> dict:
        s = self._get(sid)
        if s["status"] in ("done", "redacted"):
            raise FeedbackError(f"suggestion {sid} is {s['status']}")
        s = self._set(sid, "dismissed", reason=reason)
        self._calibrate(s["kind"], "dismissed")
        return s

    def done(self, sid: int, outcome: str, minutes: int | None = None) -> tuple[dict, list[tuple[str, str]]]:
        if outcome not in OUTCOMES:
            raise FeedbackError(f"outcome must be one of {OUTCOMES}")
        s = self._get(sid)
        if s["status"] not in ("accepted", "new"):
            raise FeedbackError(f"suggestion {sid} is {s['status']}; accept it first")
        s = self._set(sid, "done", outcome=outcome, minutes=minutes)
        if outcome in ("won", "meeting", "reply"):
            self._calibrate(s["kind"], "won")
        return s, dispatch(self.conn, self.sinks, s, "done", self.now())
