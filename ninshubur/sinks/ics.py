"""ICS calendar feed of accepted suggestions: one VEVENT per deadline and per best window.
Regenerated in full on every push so the file always mirrors the table (UID = fingerprint)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..db import rows, uj
from ..logs import write_private

PRODID = "-//Ninshubur//timings//EN"


def _ics_dt(ts: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))


def _ics_date(ts: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ts))


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 73:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def render(suggestions: list[dict], now: float) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:Ninshubur timings"]
    for s in suggestions:
        steps = uj(s.get("steps"), []) if isinstance(s.get("steps"), str) else s.get("steps", [])
        desc = _esc("\n".join(f"- {x}" for x in steps) + f"\n\n[{s['kind']}] value {s['est_value']:.0f} USD, cost {s['est_cost']:.0f} USD")
        if s.get("deadline"):
            lines += ["BEGIN:VEVENT", f"UID:{s['fingerprint']}-deadline@ninshubur", f"DTSTAMP:{_ics_dt(now)}",
                      f"DTSTART;VALUE=DATE:{_ics_date(s['deadline'])}", f"SUMMARY:{_esc('Deadline: ' + s['title'])}",
                      f"DESCRIPTION:{desc}", f"CATEGORIES:{s['kind']}", "END:VEVENT"]
        if s.get("best_window_start") and s.get("best_window_end") and s["best_window_end"] > s["best_window_start"]:
            lines += ["BEGIN:VEVENT", f"UID:{s['fingerprint']}-window@ninshubur", f"DTSTAMP:{_ics_dt(now)}",
                      f"DTSTART;VALUE=DATE:{_ics_date(s['best_window_start'])}", f"DTEND;VALUE=DATE:{_ics_date(s['best_window_end'] + 86400)}",
                      f"SUMMARY:{_esc('Window: ' + s['title'])}", f"DESCRIPTION:{desc}", f"CATEGORIES:{s['kind']}", "TRANSP:TRANSPARENT",
                      "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


class IcsSink:
    name = "ics"

    def __init__(self, conn: sqlite3.Connection, home: Path, rel_path: str = "feeds/timings.ics", now=time.time):
        self.conn = conn
        self.path = home / rel_path
        self.now = now

    def write_all(self) -> Path:
        accepted = rows(self.conn, "SELECT * FROM suggestions WHERE status='accepted' ORDER BY deadline")
        write_private(self.path, render(accepted, self.now()))
        return self.path

    def push(self, suggestion: dict, event: str) -> str | None:
        self.write_all()
        return f"{suggestion['fingerprint']}@ninshubur" if event == "accept" else None
