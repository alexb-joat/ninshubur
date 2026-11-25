"""Run report: a dated page of aggregates about what the crawler has done, rebuilt by one command.

``ninshubur export run-report`` opens the database read-only (``db.connect_readonly``) and counts. Closed
vocabularies only: every label on the page is a status class, an adapter name, a detector or refusal code, an item
or suggestion kind or status, a source health value or a model name, and anything not in the closed lists below is
counted under "other". No item title, link, host, company, person, source or profile name is read into the report,
and no free-text column is printed: ``fetches.error`` and ``sources.health_reason`` are classified or left alone,
never shown. ``docs/run-report.md`` says what each number means and what the page does not show.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters.base import all_adapters
from .db import connect_readonly
from .detectors import SEVERITIES
from .explorer.store import DB_NAME as EXPLORER_DB, STATUSES as LADDER_STATES
from .items import ITEM_KINDS
from .logs import write_private

# Detector codes, with the severity ``detectors.py`` gives each (and ``ingest.py``'s schema_drift). tests/test_run_report.py
# fails when a Finding(...) in detectors.py is missing here.
DETECTOR_SEVERITY: dict[str, str] = {
    "hidden_unicode": "high", "prompt_injection": "high", "active_html": "high", "private_key": "high",
    "api_key_like": "high", "mixed_script": "high", "lookalike_host": "high", "binary_magic": "high",
    "depth_exceeded": "high", "url_ip_literal": "high", "url_punycode": "high", "url_userinfo": "high",
    "schema_drift": "high",
    "shell_fragment": "medium", "base64_run": "medium", "repetition": "medium", "item_explosion": "medium",
    "cert_error": "integrity", "off_host_redirect": "integrity", "content_type_mismatch": "integrity",
    "body_too_large": "integrity", "decompression_bomb": "integrity", "invalid_utf8": "integrity",
}
# What the fetch gate refuses with (``fetches.error`` is ``refused:<code>``): policy.check_url's codes, then the
# Refusal(...) codes of policy.py and fetch.py.
REFUSAL_CODES = frozenset({
    "url_bad_host", "url_bad_port", "url_control_chars", "url_empty", "url_ip_literal", "url_no_host", "url_not_https",
    "url_numeric_host", "url_port", "url_punycode", "url_scheme", "url_single_label", "url_too_long", "url_unparseable",
    "url_userinfo", "url_wildcard",
    "body_on_get", "byte_budget", "circuit_open", "credential_header", "derived_fragment", "derived_links_not_allowed",
    "derived_off_host", "derived_path_term", "derived_query_too_long", "host_denied", "host_not_in_spec",
    "method_not_allowed", "person_data_remote_llm", "private_address", "review_overdue", "robots_disallow",
    "robots_unreachable", "source_health",
})
SOURCE_HEALTH = ("ok", "suspect", "broken", "quarantined", "disabled")
# The kinds suggest.py writes now, then conference_trip, which builds before the event-pages work wrote and databases still hold.
SUGGESTION_KINDS = ("cfp_submit", "conference_prospecting", "service_request_lead", "hiring_signal_outreach", "tool_outage",
                    "conference_trip")
SUGGESTION_STATUSES = ("new", "held", "accepted", "dismissed", "done", "expired", "redacted")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,63}$")
OTHER = "other"
EXPLORER_ADAPTER = "explorer"  # the explorer's inbox source (explorer/gate.py, handoff.py): a source row, not in the adapter registry
DAY = 86400.0


def _utc(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def adapter_names() -> set[str]:
    return set(all_adapters()) | {EXPLORER_ADAPTER}


def status_class(status: int | None) -> str:
    """'2xx', '4xx' and so on; 'no response' where the fetch got none (refused, or the network failed)."""
    if status is None:
        return "no response"
    return f"{status // 100}xx" if 100 <= status <= 599 else OTHER


def _bump(d: dict, key: str, n: int = 1) -> None:
    d[key] = d.get(key, 0) + n


def _closed(value, allowed) -> str:
    return value if value in allowed else OTHER


class _Reader:
    """Runs the report's queries and records which tables answered, so the footer says what was read."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.read: list[str] = []

    def table(self, name: str) -> bool:
        if name not in self.have:
            return False
        if name not in self.read:
            self.read.append(name)
        return True

    def all(self, sql: str, params: tuple = ()) -> list[tuple]:
        return [tuple(r) for r in self.conn.execute(sql, params)]


def _where(col: str, since: float | None) -> tuple[str, tuple]:
    return (f" WHERE {col}>=?", (since,)) if since is not None else ("", ())


def _safe(fn, *args) -> dict:
    """A section whose query this build cannot run on this database (a column an older schema lacks) is left out, not fatal."""
    try:
        return fn(*args)
    except sqlite3.OperationalError:
        return {"omitted": "its tables could not be read with this build's queries (an older or newer schema)"}


def _missing(table: str, why: str = "this database has no such table (an older schema)") -> dict:
    return {"omitted": f"table `{table}`: {why}"}


def _window(r: _Reader, since: float | None) -> dict:
    if not r.table("fetches"):
        return _missing("fetches")
    w, p = _where("ts", since)
    first, last, n = r.all(f"SELECT MIN(ts), MAX(ts), COUNT(*) FROM fetches{w}", p)[0]
    days = {x[0] for x in r.all(f"SELECT DISTINCT date(ts, 'unixepoch') FROM fetches{w}", p)}
    out = {"first_fetch": _utc(first), "last_fetch": _utc(last), "fetches": n, "days_with_a_fetch": len(days)}
    if r.table("feedback"):
        with_feedback = {x[0] for x in r.all("SELECT DISTINCT date(at, 'unixepoch') FROM feedback")}
        out["days_fetched_without_human_feedback"] = len(days - with_feedback)
    else:
        out["days_fetched_without_human_feedback"] = None
        out["note"] = "table `feedback` is missing, so days without human feedback are not counted"
    return out


def _fetches(r: _Reader, since: float | None) -> dict:
    if not r.table("fetches"):
        return _missing("fetches")
    w, p = _where("f.ts", since)
    join = "LEFT JOIN sources s ON s.name = f.source" if r.table("sources") else ""
    adapter_col = "s.adapter" if join else "NULL"
    registered = adapter_names()
    by_class: dict[str, int] = {}
    by_adapter: dict[str, dict[str, int]] = {}
    errors = changed = nbytes = total = 0
    sql = (f"SELECT {adapter_col}, f.status, f.error IS NOT NULL, COUNT(*), COALESCE(SUM(f.changed), 0), "
           f"COALESCE(SUM(f.bytes), 0) FROM fetches f {join}{w} GROUP BY 1, 2, 3")
    for adapter, status, has_error, n, ch, nb in r.all(sql, p):
        cls = status_class(status)
        name = _closed(adapter, registered) if adapter is not None else \
            ("source no longer in the database" if join else "not known (no sources table)")
        total, changed, nbytes = total + n, changed + ch, nbytes + nb
        errors += n if has_error else 0
        _bump(by_class, cls, n)
        row = by_adapter.setdefault(name, {"fetches": 0, "2xx": 0, "non-2xx": 0, "no response": 0})
        row["fetches"] += n
        row["2xx" if cls == "2xx" else "no response" if cls == "no response" else "non-2xx"] += n
    return {"fetches": total, "bytes": nbytes, "changed": changed, "with_an_error_recorded": errors,
            "by_status_class": dict(sorted(by_class.items())), "by_adapter": dict(sorted(by_adapter.items())),
            "adapters_known": bool(join)}


def _split_codes(text: str | None) -> list[str]:
    return [c for c in (text or "").split(",") if c]


def _refusals(r: _Reader, since: float | None) -> dict:
    out: dict = {}
    if r.table("fetches"):
        w, p = _where("ts", since)
        gate: dict[str, int] = {}
        findings: dict[str, int] = {}
        for err, n in r.all(f"SELECT error, COUNT(*) FROM fetches{w} GROUP BY error", p):
            if not err:
                continue
            if err.startswith("refused:"):
                _bump(gate, _closed(err[len("refused:"):], REFUSAL_CODES), n)
                continue
            parts = _split_codes(err)
            if parts and all(c in DETECTOR_SEVERITY for c in parts):
                for c in parts:
                    _bump(findings, c, n)
        out["gate_refusals"] = dict(sorted(gate.items()))
        out["fetch_findings"] = dict(sorted(findings.items()))
    else:
        out["gate_refusals"] = out["fetch_findings"] = _missing("fetches")
    if r.table("quarantine"):
        w, p = _where("ts", since)
        by_sev: dict[str, int] = {}
        by_code: dict[str, int] = {}
        total = 0
        for sev, codes, n in r.all(f"SELECT severity, detector_codes, COUNT(*) FROM quarantine{w} GROUP BY 1, 2", p):
            total += n
            _bump(by_sev, _closed(sev, SEVERITIES), n)
            for c in _split_codes(codes):
                _bump(by_code, _closed(c, DETECTOR_SEVERITY), n)
        out["quarantine"] = {"rows": total, "by_severity": dict(sorted(by_sev.items())), "by_code": dict(sorted(by_code.items()))}
    else:
        out["quarantine"] = _missing("quarantine")
    out["severity_of"] = {c: s for c, s in sorted(DETECTOR_SEVERITY.items())}
    return out


def _items(r: _Reader, since: float | None) -> dict:
    if not r.table("items"):
        return _missing("items")
    w, p = _where("first_seen_at", since)
    by_kind: dict[str, dict[str, int]] = {}
    for kind, n, held, tainted in r.all(f"SELECT kind, COUNT(*), COALESCE(SUM(held), 0), COALESCE(SUM(tainted), 0) FROM items{w} GROUP BY kind", p):
        row = by_kind.setdefault(_closed(kind, ITEM_KINDS), {"items": 0, "held": 0, "tainted": 0})
        row["items"] += n
        row["held"] += held
        row["tainted"] += tainted
    return {"by_kind": dict(sorted(by_kind.items())), "items": sum(v["items"] for v in by_kind.values())}


def _companies(r: _Reader) -> dict:
    if not r.table("company_aliases"):
        return _missing("company_aliases")
    groups, aliases = r.all("SELECT COUNT(DISTINCT company_key), COUNT(*) FROM company_aliases")[0]
    return {"canonical_groups": groups, "aliases": aliases}


def _suggestions(r: _Reader, since: float | None) -> dict:
    if not r.table("suggestions"):
        return _missing("suggestions")
    w, p = _where("created_at", since)
    grid: dict[str, dict[str, int]] = {}
    for kind, status, n in r.all(f"SELECT kind, status, COUNT(*) FROM suggestions{w} GROUP BY 1, 2", p):
        _bump(grid.setdefault(_closed(kind, SUGGESTION_KINDS), {}), _closed(status, SUGGESTION_STATUSES), n)
    return {"by_kind_and_status": {k: dict(sorted(v.items())) for k, v in sorted(grid.items())},
            "suggestions": sum(sum(v.values()) for v in grid.values())}


def _sources(r: _Reader) -> dict:
    if not r.table("sources"):
        return _missing("sources")
    registered = adapter_names()
    by_adapter: dict[str, dict] = {}
    for adapter, health, enabled, n in r.all("SELECT adapter, health, enabled, COUNT(*) FROM sources GROUP BY 1, 2, 3"):
        row = by_adapter.setdefault(_closed(adapter, registered), {"sources": 0, "enabled": 0, "health": {}})
        row["sources"] += n
        row["enabled"] += n if enabled else 0
        _bump(row["health"], _closed(health, SOURCE_HEALTH), n)
    return {"by_adapter": {a: {**v, "health": dict(sorted(v["health"].items()))} for a, v in sorted(by_adapter.items())},
            "sources": sum(v["sources"] for v in by_adapter.values())}


def _llm(r: _Reader, since: float | None) -> dict:
    if not r.table("llm_runs"):
        return _missing("llm_runs")
    w, p = _where("started_at", since)
    by_model: dict[str, dict] = {}
    sql = ("SELECT model, COUNT(*), COALESCE(SUM(answered), 0), COALESCE(SUM(failed), 0), COALESCE(SUM(input_tokens), 0), "
           f"COALESCE(SUM(output_tokens), 0), COALESCE(SUM(usd), 0) FROM llm_runs{w} GROUP BY model")
    for model, runs, answered, failed, tin, tout, usd in r.all(sql, p):
        row = by_model.setdefault(model if _MODEL_NAME.match(str(model)) else "other (not a plain model name)",
                                  {"runs": 0, "answered": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0, "usd_as_recorded": 0.0})
        row["runs"] += runs
        row["answered"] += answered
        row["failed"] += failed
        row["input_tokens"] += tin
        row["output_tokens"] += tout
        row["usd_as_recorded"] = round(row["usd_as_recorded"] + usd, 6)
    return {"by_model": dict(sorted(by_model.items())), "runs": sum(v["runs"] for v in by_model.values())}


def _explorer(path: Path | None) -> tuple[dict, bool]:
    """The explorer's ladder counts by state, from its own database, read-only; (section, whether it was read)."""
    if path is None or not path.is_file():
        return {"omitted": "the explorer's database is not in this data home (the explorer is Linux only and may never have run)"}, False
    try:
        conn = connect_readonly(path=path)
    except (sqlite3.Error, OSError) as e:
        return {"omitted": f"the explorer's database could not be opened read-only ({type(e).__name__})"}, False
    try:
        r = _Reader(conn)
        if not r.table("explorer_candidates"):
            return _missing("explorer_candidates", "the explorer's database has no such table"), False
        by_state: dict[str, int] = {}
        for state, n in r.all("SELECT status, COUNT(*) FROM explorer_candidates GROUP BY status"):
            _bump(by_state, _closed(state, LADDER_STATES), n)
        ordered = {s: by_state[s] for s in (*LADDER_STATES, OTHER) if s in by_state}
        return {"by_state": ordered, "candidates": sum(ordered.values())}, True
    except sqlite3.Error as e:
        return {"omitted": f"the explorer's database could not be read ({type(e).__name__})"}, False
    finally:
        conn.close()


def build(conn: sqlite3.Connection, *, now: float, days: int | None = None, explorer_db: Path | None = None) -> dict:
    """The report as plain data. ``conn`` should come from ``connect_readonly``; ``days`` limits the time-stamped
    sections to the last N days (sources, companies and the explorer's ladder are counts as of now)."""
    since = now - days * DAY if days else None
    r = _Reader(conn)
    sections = {
        "window": _safe(_window, r, since), "fetches": _safe(_fetches, r, since), "refusals": _safe(_refusals, r, since),
        "items": _safe(_items, r, since), "companies": _safe(_companies, r), "suggestions": _safe(_suggestions, r, since),
        "sources": _safe(_sources, r), "llm_runs": _safe(_llm, r, since),
    }
    sections["explorer"], explorer_read = _explorer(explorer_db)
    left_out = []
    for name, sec in sections.items():
        for key, val in ([(None, sec)] if "omitted" in sec else [(k, v) for k, v in sec.items() if isinstance(v, dict) and "omitted" in v]):
            left_out.append({"section": name if key is None else f"{name}/{key}", "why": val["omitted"]})
    return {"generated_at": _utc(now), "version": __version__, "days": days,
            "tables_read": sorted(r.read), "explorer_database_read": explorer_read, "left_out": left_out, "sections": sections}


# --- rendering ------------------------------------------------------------------------------
def _table(head: list[str], body: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(" --- " if i == 0 else " ---: " for i in range(len(head))) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return "\n".join(out)


def _counts(d: dict[str, int], label: str) -> str:
    if not d:
        return "None.\n"
    body = [[k, f"{v:,}"] for k, v in d.items()]
    return _table([label, "count"], body) + "\n"


def _skip(sec: dict) -> str | None:
    return f"Left out: {sec['omitted']}.\n" if "omitted" in sec else None


def render_markdown(rep: dict) -> str:
    s = rep["sections"]
    date = rep["generated_at"][:10]
    days = rep["days"]
    out = [f"# Ninshubur run report, {date}\n"]
    w = s["window"]
    if "omitted" in w:
        out.append(_skip(w))
    else:
        scope = f"the last {days} days" if days else "everything stored"
        if w["fetches"]:
            out.append(f"Window: first fetch {w['first_fetch']}, last fetch {w['last_fetch']} ({scope}), {w['fetches']:,} fetches.\n")
        else:
            out.append(f"Window: no fetches in {scope}.\n")
        out.append(f"- Days with at least one fetch: {w['days_with_a_fetch']}")
        nf = w["days_fetched_without_human_feedback"]
        out.append(f"- Days fetched without human feedback: {nf if nf is not None else 'not counted'}"
                   + ("" if nf is not None else f" ({w['note']})"))
        out.append("")
    out.append("Counts only, taken from this one database. No title, link, host, or name of a company, person, source or "
               "profile is on this page, and no free-text field is printed. `docs/run-report.md` says what each number means "
               "and what the page does not show.\n")

    f = s["fetches"]
    out.append("## Fetches\n")
    if _skip(f):
        out.append(_skip(f))
    else:
        out.append(f"{f['fetches']:,} fetches, {f['bytes']:,} bytes received, {f['changed']:,} that changed what was stored, "
                   f"{f['with_an_error_recorded']:,} with an error recorded (counted here, never printed).\n")
        out.append("By status class:\n")
        out.append(_counts(f["by_status_class"], "status class"))
        out.append("By adapter (not by source name):\n")
        if f["by_adapter"]:
            out.append(_table(["adapter", "fetches", "2xx", "non-2xx", "no response"],
                              [[a, *(f"{v[k]:,}" for k in ("fetches", "2xx", "non-2xx", "no response"))] for a, v in f["by_adapter"].items()]) + "\n")
        else:
            out.append("None.\n")

    ref = s["refusals"]
    out.append("## Refusals and quarantines\n")
    out.append("Fetches the gate refused before any request, by code:\n")
    out.append(_skip(ref["gate_refusals"]) or _counts(ref["gate_refusals"], "refusal code"))
    out.append("Findings recorded on fetched documents, by detector code:\n")
    out.append(_skip(ref["fetch_findings"]) or _counts({f"{c} ({DETECTOR_SEVERITY[c]})": n for c, n in ref["fetch_findings"].items()}, "detector code (severity)"))
    q = ref["quarantine"]
    out.append("Quarantine records:\n")
    if _skip(q):
        out.append(_skip(q))
    else:
        out.append(f"{q['rows']:,} record(s). By severity:\n")
        out.append(_counts(q["by_severity"], "severity"))
        out.append("By detector code (a record can carry several):\n")
        out.append(_counts({(f"{c} ({DETECTOR_SEVERITY[c]})" if c in DETECTOR_SEVERITY else c): n for c, n in q["by_code"].items()}, "detector code (severity)"))

    it = s["items"]
    out.append("## Items\n")
    if _skip(it):
        out.append(_skip(it))
    else:
        out.append(f"{it['items']:,} items first seen in the window, by kind:\n")
        out.append(_table(["kind", "items", "held", "tainted"], [[k, f"{v['items']:,}", f"{v['held']:,}", f"{v['tainted']:,}"] for k, v in it["by_kind"].items()]) + "\n"
                   if it["by_kind"] else "None.\n")

    c = s["companies"]
    out.append("## Companies\n")
    out.append(_skip(c) or f"{c['canonical_groups']:,} canonical company groups (from {c['aliases']:,} aliases, as of the last roll-up; not limited to the window).\n")

    sg = s["suggestions"]
    out.append("## Suggestions\n")
    if _skip(sg):
        out.append(_skip(sg))
    elif not sg["by_kind_and_status"]:
        out.append("None in the window.\n")
    else:
        states = [x for x in (*SUGGESTION_STATUSES, OTHER) if any(x in v for v in sg["by_kind_and_status"].values())]
        out.append(f"{sg['suggestions']:,} suggestions created in the window, by kind and status:\n")
        out.append(_table(["kind", *states], [[k, *(f"{v.get(x, 0):,}" for x in states)] for k, v in sg["by_kind_and_status"].items()]) + "\n")

    so = s["sources"]
    out.append("## Sources\n")
    if _skip(so):
        out.append(_skip(so))
    elif not so["by_adapter"]:
        out.append("None.\n")
    else:
        states = [x for x in (*SOURCE_HEALTH, OTHER) if any(x in v["health"] for v in so["by_adapter"].values())]
        out.append(f"{so['sources']:,} sources as of now, by adapter and health:\n")
        out.append(_table(["adapter", "sources", "enabled", *states],
                          [[a, f"{v['sources']:,}", f"{v['enabled']:,}", *(f"{v['health'].get(x, 0):,}" for x in states)] for a, v in so["by_adapter"].items()]) + "\n")

    ll = s["llm_runs"]
    out.append("## Model runs\n")
    if _skip(ll):
        out.append(_skip(ll))
    elif not ll["by_model"]:
        out.append("None in the window.\n")
    else:
        out.append(f"{ll['runs']:,} runs of `ninshubur qualify`. The cost column is the sum of what each run recorded as its USD, as recorded, "
                   "not a bill.\n")
        out.append(_table(["model", "runs", "answered", "failed", "input tokens", "output tokens", "USD (as recorded)"],
                          [[m, f"{v['runs']:,}", f"{v['answered']:,}", f"{v['failed']:,}", f"{v['input_tokens']:,}", f"{v['output_tokens']:,}",
                            f"{v['usd_as_recorded']:.4f}"] for m, v in ll["by_model"].items()]) + "\n")

    ex = s["explorer"]
    out.append("## Explorer ladder\n")
    if _skip(ex):
        out.append("Not included. " + _skip(ex))
    else:
        out.append(f"{ex['candidates']:,} candidates as of now, by state:\n")
        out.append(_counts(ex["by_state"], "state"))

    out.append("---\n")
    out.append(f"Generated {rep['generated_at']} by ninshubur {rep['version']}. Tables read: {', '.join(rep['tables_read']) or 'none'}"
               + (", and the explorer's `explorer_candidates`" if rep["explorer_database_read"] else "") + ". "
               "The database was opened read-only (`mode=ro`, `query_only`); nothing was written to it. "
               + ("Left out: " + "; ".join(f"{x['section']} ({x['why']})" for x in rep["left_out"]) + ". " if rep["left_out"] else "")
               + "Rebuild this page with `ninshubur export run-report" + (f" --days {days}" if days else "") + "`.\n")
    return "\n".join(out)


def to_json(rep: dict) -> str:
    return json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def default_path(home: Path, now: float, fmt: str) -> Path:
    return home / "feeds" / f"run-report-{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d}.{fmt}"


def run(args, home: Path, now: float) -> int:
    """``ninshubur export run-report``: build, print where it went. Never opens the main database for writing."""
    if args.format not in ("md", "json"):
        print("run-report is written as md or json (--format md|json)", file=sys.stderr)
        return 2
    if args.days is not None and args.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 2
    try:
        conn = connect_readonly(home)
    except FileNotFoundError:
        print(f"no database in {home}: nothing to report yet (the crawler has not run here)", file=sys.stderr)
        return 2
    try:
        rep = build(conn, now=now, days=args.days, explorer_db=home / "explorer" / EXPLORER_DB)
    finally:
        conn.close()
    text = to_json(rep) if args.format == "json" else render_markdown(rep)
    path = Path(args.output) if args.output else default_path(home, now, args.format)
    write_private(path, text)
    print(f"written {path}")
    return 0
