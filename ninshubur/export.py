"""Exports for the LeadGet team: a per-profile digest (Markdown, printable HTML) and flat CSV
files of suggestions or items. Everything is written with 0600 under the data directory; nothing
is sent anywhere. Audience is internal, so formulas, held reasons and tiers stay visible."""
from __future__ import annotations

import csv
import html
import io
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .analyze.suggest import Suggester
from .db import rows, uj
from .logs import write_private
from .schedule import status_summary

KIND_LABELS = {
    "conference_trip": "Conference trips", "cfp_submit": "Calls for papers", "service_request_lead": "Service requests",
    "hiring_signal_outreach": "Hiring signals", "tool_outage": "Provider incidents", "outreach_window": "Outreach windows",
    "pain_content_idea": "Content ideas", "tooling_adopt": "Tooling", "tool_price_change": "Price changes",
    "tool_deprecation": "Deprecations", "local_meeting_suggestion": "Local meetings",
}
STATUS_ORDER = ("new", "accepted", "held", "done", "dismissed", "expired")


def parse_since(s: str | None, now: float) -> float | None:
    if not s:
        return None
    s = s.strip().lower()
    n, unit = (s[:-1], s[-1]) if s[-1] in "dwh" else (s, "d")
    mult = {"h": 3600, "d": 86400, "w": 7 * 86400}[unit]
    return now - float(n) * mult


def gather(conn: sqlite3.Connection, suggester: Suggester, *, profile: str | None, statuses: tuple[str, ...],
           since: float | None, now: float, tz: ZoneInfo) -> dict:
    """One data-gathering pass shared by every renderer."""
    sugg = suggester.list(status=statuses, profile=profile, top=10_000)
    if since is not None:
        sugg = [s for s in sugg if s["updated_at"] >= since]
    by_kind: dict[str, list[dict]] = {}
    for s in sugg:
        by_kind.setdefault(s["kind"], []).append(s)
    summary = status_summary(conn, now)
    cands = rows(conn, "SELECT host, count, found_in_source, reason FROM candidates WHERE status='new' ORDER BY count DESC LIMIT 10")
    live = [s for s in sugg if s["status"] in ("new", "accepted")]
    return {
        "profile": profile or "all profiles", "generated_at": now, "tz": tz.key, "since": since,
        "statuses": statuses, "suggestions": sugg, "by_kind": by_kind, "sources": summary["sources"],
        "candidates": cands, "counts": {k: len(v) for k, v in by_kind.items()},
        "total_value": sum(s["est_value"] for s in live), "total_minutes": sum(float(s["cost_parts"].get("human_minutes", 0)) for s in live),
        "held": [s for s in sugg if s["status"] == "held"],
    }


def _day(ts: float | None, tz: ZoneInfo) -> str:
    return "-" if not ts else datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def _dt(ts: float | None, tz: ZoneInfo) -> str:
    return "-" if not ts else datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def badges(s: dict) -> list[str]:
    tier = min((e.get("tier", 3) for e in s["evidence"]), default=3)
    b = [f"T{tier}"]
    if tier >= 2:
        b.append("unverified")
    if s["status"] == "held":
        b.append(f"held: {s.get('held_reason')}")
    return b


# --- Markdown ---------------------------------------------------------------------------------
def render_md(d: dict) -> str:
    tz = ZoneInfo(d["tz"])
    out = [f"# Ninshubur digest: {d['profile']}", "",
           f"Generated {_dt(d['generated_at'], tz)} ({d['tz']}). Statuses: {', '.join(d['statuses'])}."
           + (f" Updated since {_day(d['since'], tz)}." if d["since"] else ""), "",
           f"**{len(d['suggestions'])} suggestions**, " + ", ".join(f"{n} {KIND_LABELS.get(k, k)}" for k, n in sorted(d["counts"].items()))
           + f". Live expected value {d['total_value']:.0f} USD for {d['total_minutes']:.0f} minutes of human time.", ""]
    for kind, items in sorted(d["by_kind"].items(), key=lambda kv: -sum(s["est_value"] for s in kv[1])):
        out.append(f"## {KIND_LABELS.get(kind, kind)}")
        out.append("")
        for s in items:
            out.append(f"### #{s['id']} {s['title']}")
            out.append(f"*{s['status']}* · {' · '.join(badges(s))} · value **{s['est_value']:.0f} USD** · cost {s['est_cost']:.0f} USD"
                       f" · ROI {s['roi']:.1f} · confidence {s['confidence']:.2f}")
            win = f" (best {_day(s['best_window_start'], tz)} to {_day(s['best_window_end'], tz)})" if s.get("best_window_start") else ""
            out.append(f"Due {_day(s.get('deadline'), tz)}{win}; expires {_day(s['expires_at'], tz)}.")
            out.append("")
            for r in s["reasons"]:
                out.append(f"- {r}")
            out.append("")
            out.append("Steps:")
            for i, st in enumerate(s["steps"], 1):
                out.append(f"{i}. {st}")
            out.append("")
            out.append("Evidence:")
            for e in s["evidence"]:
                vals = ", ".join(f"{k}={v}" for k, v in e.get("values", {}).items())
                out.append(f"- [{e.get('url')}]({e.get('url')}) ({e.get('source')}, T{e.get('tier')}, fetched {_dt(e.get('fetched_at'), tz)}) {vals}")
            out.append("")
    if d["held"]:
        out.append("## Held (not actionable until the source is reviewed or corroborated)")
        out.append("")
        for s in d["held"]:
            out.append(f"- #{s['id']} [{s['kind']}] {s['title']}: {s.get('held_reason')}")
        out.append("")
    out.append("## Sources")
    out.append("")
    out.append("| Source | Adapter | Tier | Reputation | Health | Next run | Review due |")
    out.append("|---|---|---|---|---|---|---|")
    for src in d["sources"]:
        due = (src["reviewed_at"] or 0) + (src["review_every_days"] or 0) * 86400
        out.append(f"| {src['name']} | {src['adapter']} | {src['tier']} | {src['reputation']:.2f} | {src['health']} | "
                   f"{_dt(src['next_due_at'], tz)} | {_day(due, tz)} |")
    out.append("")
    if d["candidates"]:
        out.append("## Candidate hosts seen in content (never fetched; admit with `sources add`)")
        out.append("")
        for c in d["candidates"]:
            out.append(f"- {c['host']} (x{c['count']}, via {c['found_in_source']}: {c['reason']})")
        out.append("")
    return "\n".join(out)


# --- HTML (printable) ---------------------------------------------------------------------------
_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#111;background:#fff;max-width:900px;margin:24px auto;padding:0 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid #999;padding-bottom:4px}
h3{font-size:15px;margin:18px 0 2px}.meta{color:#444;font-size:12px}.badge{display:inline-block;border:1px solid #777;border-radius:3px;padding:0 5px;font-size:11px;margin-right:4px}
.held{color:#7a3b00}ul,ol{margin:4px 0 8px 20px}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #bbb;padding:3px 6px;text-align:left}
a{color:#0645ad;word-break:break-all}.entry{break-inside:avoid;page-break-inside:avoid}
@media print{body{margin:0;max-width:none}h2{break-before:page;page-break-before:always}h2:first-of-type{break-before:auto;page-break-before:auto}a{color:#000;text-decoration:none}}
"""


def render_html(d: dict) -> str:
    tz = ZoneInfo(d["tz"])
    e = html.escape
    out = ["<!doctype html>", "<html lang=\"en\"><head><meta charset=\"utf-8\">",
           f"<title>Ninshubur digest: {e(d['profile'])}</title><style>{_CSS}</style></head><body>",
           f"<h1>Ninshubur digest: {e(d['profile'])}</h1>",
           f"<p class=\"meta\">Generated {_dt(d['generated_at'], tz)} ({e(d['tz'])}). Statuses: {e(', '.join(d['statuses']))}."
           + (f" Updated since {_day(d['since'], tz)}." if d["since"] else "") + "</p>",
           f"<p><b>{len(d['suggestions'])} suggestions</b>, " + e(", ".join(f"{n} {KIND_LABELS.get(k, k)}" for k, n in sorted(d["counts"].items())))
           + f". Live expected value {d['total_value']:.0f} USD for {d['total_minutes']:.0f} minutes of human time.</p>"]
    for kind, items in sorted(d["by_kind"].items(), key=lambda kv: -sum(s["est_value"] for s in kv[1])):
        out.append(f"<h2>{e(KIND_LABELS.get(kind, kind))}</h2>")
        for s in items:
            out.append("<div class=\"entry\">")
            out.append(f"<h3>#{s['id']} {e(s['title'])}</h3>")
            bd = "".join(f"<span class=\"badge{' held' if b.startswith('held') else ''}\">{e(b)}</span>" for b in badges(s))
            out.append(f"<p class=\"meta\"><i>{e(s['status'])}</i> {bd} value <b>{s['est_value']:.0f} USD</b> · cost {s['est_cost']:.0f} USD"
                       f" · ROI {s['roi']:.1f} · confidence {s['confidence']:.2f}</p>")
            win = f" (best {_day(s['best_window_start'], tz)} to {_day(s['best_window_end'], tz)})" if s.get("best_window_start") else ""
            out.append(f"<p>Due {_day(s.get('deadline'), tz)}{win}; expires {_day(s['expires_at'], tz)}.</p>")
            out.append("<ul>" + "".join(f"<li>{e(r)}</li>" for r in s["reasons"]) + "</ul>")
            out.append("<p>Steps:</p><ol>" + "".join(f"<li>{e(st)}</li>" for st in s["steps"]) + "</ol>")
            out.append("<p>Evidence:</p><ul>")
            for ev in s["evidence"]:
                vals = ", ".join(f"{k}={v}" for k, v in ev.get("values", {}).items())
                url = str(ev.get("url") or "")
                href = e(url) if url.startswith("https://") else ""
                link = f"<a href=\"{href}\">{e(url)}</a>" if href else e(url)
                out.append(f"<li>{link} ({e(str(ev.get('source')))}, T{ev.get('tier')}, fetched {_dt(ev.get('fetched_at'), tz)}) {e(vals)}</li>")
            out.append("</ul></div>")
    if d["held"]:
        out.append("<h2>Held</h2><p class=\"meta\">Not actionable until the source is reviewed or the claim is corroborated.</p><ul>")
        out += [f"<li>#{s['id']} [{e(s['kind'])}] {e(s['title'])}: {e(str(s.get('held_reason')))}</li>" for s in d["held"]]
        out.append("</ul>")
    out.append("<h2>Sources</h2><table><tr><th>Source</th><th>Adapter</th><th>Tier</th><th>Reputation</th><th>Health</th><th>Next run</th><th>Review due</th></tr>")
    for src in d["sources"]:
        due = (src["reviewed_at"] or 0) + (src["review_every_days"] or 0) * 86400
        out.append(f"<tr><td>{e(src['name'])}</td><td>{e(src['adapter'])}</td><td>{src['tier']}</td><td>{src['reputation']:.2f}</td>"
                   f"<td>{e(src['health'])}</td><td>{_dt(src['next_due_at'], tz)}</td><td>{_day(due, tz)}</td></tr>")
    out.append("</table>")
    if d["candidates"]:
        out.append("<h2>Candidate hosts (never fetched)</h2><ul>")
        out += [f"<li>{e(c['host'])} (x{c['count']}, via {e(c['found_in_source'])}: {e(str(c['reason']))})</li>" for c in d["candidates"]]
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


# --- CSV ---------------------------------------------------------------------------------------
SUGGESTION_COLUMNS = ["id", "kind", "profile", "status", "held_reason", "title", "value_usd", "cost_usd", "roi", "confidence",
                      "deadline", "window_start", "window_end", "expires", "source", "tier", "evidence_url", "fetched_at", "steps", "reasons"]
ITEM_COLUMNS = ["source", "kind", "title", "url", "published_at", "city", "country", "company", "dates", "tainted", "held"]


def render_csv_suggestions(d: dict) -> str:
    tz = ZoneInfo(d["tz"])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(SUGGESTION_COLUMNS)
    for s in d["suggestions"]:
        ev = s["evidence"][0] if s["evidence"] else {}
        w.writerow([s["id"], s["kind"], s.get("profile") or "", s["status"], s.get("held_reason") or "", s["title"],
                    f"{s['est_value']:.2f}", f"{s['est_cost']:.2f}", f"{s['roi']:.2f}", f"{s['confidence']:.2f}",
                    _day(s.get("deadline"), tz), _day(s.get("best_window_start"), tz), _day(s.get("best_window_end"), tz),
                    _day(s["expires_at"], tz), ev.get("source", ""), ev.get("tier", ""), ev.get("url", ""), _dt(ev.get("fetched_at"), tz),
                    " | ".join(s["steps"]), " | ".join(s["reasons"])])
    return "﻿" + buf.getvalue()


def render_csv_items(conn: sqlite3.Connection, *, since: float | None, tz: ZoneInfo) -> str:
    sql = ("SELECT i.source, i.kind, i.title, i.url, i.published_at, i.attrs, i.tainted, i.held, "
           "GROUP_CONCAT(d.kind || '=' || d.at) AS dates FROM items i LEFT JOIN item_dates d ON d.item_id=i.id")
    params: list = []
    if since is not None:
        sql += " WHERE i.last_seen_at >= ?"
        params.append(since)
    sql += " GROUP BY i.id ORDER BY i.source, i.kind, i.published_at DESC"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(ITEM_COLUMNS)
    for r in rows(conn, sql, params):
        a = uj(r["attrs"])
        dates = "; ".join(f"{p.split('=')[0]} {_day(float(p.split('=')[1]), tz)}" for p in (r["dates"] or "").split(",") if "=" in p)
        w.writerow([r["source"], r["kind"], r["title"], r["url"] or "", _day(r["published_at"], tz), a.get("city", ""), a.get("country", ""),
                    a.get("company", ""), dates, r["tainted"], r["held"]])
    return "﻿" + buf.getvalue()


# --- entry point -------------------------------------------------------------------------------
def default_name(what: str, fmt: str, profile: str | None, now: float) -> str:
    week = time.strftime("%G-W%V", time.gmtime(now))
    tag = f"-{profile}" if profile else ""
    return f"{what}-{week}{tag}.{fmt}"


def export(conn: sqlite3.Connection, suggester: Suggester, home: Path, *, what: str, fmt: str, profile: str | None,
           statuses: tuple[str, ...], since: float | None, now: float, tz: ZoneInfo, out: Path | None = None) -> Path:
    if what == "items":
        if fmt != "csv":
            raise ValueError("items export is csv only")
        text = render_csv_items(conn, since=since, tz=tz)
    else:
        d = gather(conn, suggester, profile=profile, statuses=statuses, since=since, now=now, tz=tz)
        if fmt == "md":
            text = render_md(d)
        elif fmt == "html":
            text = render_html(d)
        elif fmt == "csv":
            text = render_csv_suggestions(d)
        else:
            raise ValueError(f"unknown format {fmt}")
    path = out or (home / "feeds" / default_name(what, fmt, profile, now))
    write_private(path, text)
    return path


def weekly_digests(conn: sqlite3.Connection, suggester: Suggester, home: Path, profiles: list[str], now: float, tz: ZoneInfo) -> list[Path]:
    """Called by the daemon: one Markdown digest per profile when the ISO week changes."""
    week = time.strftime("%G-W%V", time.gmtime(now))
    last = conn.execute("SELECT value FROM meta WHERE key='last_digest_week'").fetchone()
    if last and last[0] == week:
        return []
    written = []
    for p in profiles or [None]:
        written.append(export(conn, suggester, home, what="digest", fmt="md", profile=p, statuses=("new", "accepted", "held"),
                              since=None, now=now, tz=tz))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_digest_week', ?)", (week,))
    return written
