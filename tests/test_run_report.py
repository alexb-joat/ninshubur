"""`ninshubur export run-report`: aggregates only. The synthetic database below carries distinctive strings in every
column that names something (titles, links, sources, profiles, people, companies, free-text errors); none may reach the
page. The counts are worked out by hand, for the fixed clock NOW (2026-10-12 12:00 UTC)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ninshubur import runreport as RR
from ninshubur.db import connect, connect_readonly, db_path
from ninshubur.detectors import SEVERITIES
from tests.conftest import NOW

ROOT = Path(__file__).resolve().parent.parent

# Every one of these is in the synthetic database; none may appear in the output.
DISTINCTIVE = [
    "Zq Source Alpha", "Zq Source Beta", "Zq Source Gamma", "Zq Gone Source", "Zq Title One", "Zq Title Two", "Zq Title Three",
    "Zq Title Four", "Zq Person Name", "zq-host-one.example", "zq-explorer.example", "Zq Profile Name", "Zq Entity Person",
    "Zq Subject Company", "ConnectError-ZQ", "zq_made_up_code", "ZQHEALTH", "ZQHASH", "ZQ-STEPS", "zq_kind", "zq-a", "company:zq1",
    "local qwen!!", "zq_sev",
]


def at(day: int, hh: int = 0, mm: int = 0) -> float:
    return datetime(2026, 10, day, hh, mm, tzinfo=timezone.utc).timestamp()


def add_source(conn, name, adapter, *, enabled=1, health="ok", reason=None):
    conn.execute("""INSERT INTO sources(name, adapter, kind, tier, reputation, interval_s, base_interval_s, max_interval_s, enabled,
                    health, health_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (name, adapter, "x", 1, 0.5, 60, 60, 600, enabled, health, reason, NOW, NOW))


def add_fetch(conn, ts, source, status, error=None, changed=0, nbytes=0):
    conn.execute("INSERT INTO fetches(ts, source, url, status, bytes, changed, error) VALUES (?,?,?,?,?,?,?)",
                 (ts, source, "https://zq-host-one.example/a", status, nbytes, changed, error))


def add_item(conn, n, kind, title, ts, held=0, tainted=0):
    conn.execute("""INSERT INTO items(uid, source, kind, title, url, legal_basis, first_seen_at, last_seen_at, held, tainted)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (f"u{n}", "Zq Source Alpha", kind, title, "https://zq-host-one.example/job", "x", ts, ts, held, tainted))


def add_suggestion(conn, n, kind, status, ts):
    conn.execute("""INSERT INTO suggestions(fingerprint, kind, profile, title, steps, reasons, evidence, confidence, est_value, est_cost,
                    expires_at, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (f"f{n}", kind, "Zq Profile Name", "Zq Title One", "ZQ-STEPS", "[]", "[]", 0.5, 1.0, 1.0, NOW + 1, status, ts, ts))


def add_run(conn, model, answered, failed, tin, tout, usd, ts):
    conn.execute("""INSERT INTO llm_runs(task, profile, slot, model, prompt_hash, started_at, answered, failed, input_tokens, output_tokens, usd)
                    VALUES ('qualify','Zq Profile Name','local',?,?,?,?,?,?,?,?)""", (model, "ph", ts, answered, failed, tin, tout, usd))


def build_db(home, *, explorer: bool = False) -> Path:
    conn = connect(home)
    add_source(conn, "Zq Source Alpha", "greenhouse")
    add_source(conn, "Zq Source Beta", "lever", health="quarantined", reason="reason ZQHEALTH")
    add_source(conn, "Zq Source Gamma", "greenhouse", enabled=0, health="broken")
    for ts, src, status, err, ch, nb in [
        (at(10, 10), "Zq Source Alpha", 200, None, 1, 1000), (at(10, 11), "Zq Source Alpha", 200, None, 0, 500),
        (at(10, 12), "Zq Source Alpha", 304, None, 0, 0),
        (at(11, 9), "Zq Source Beta", 404, None, 0, 100), (at(11, 10), "Zq Source Beta", 503, None, 0, 0),
        (at(11, 11), "Zq Source Beta", None, "ConnectError-ZQ", 0, 0),
        (at(12, 8, 0), "Zq Source Alpha", None, "refused:robots_disallow", 0, 0),
        (at(12, 8, 5), "Zq Source Alpha", None, "refused:robots_disallow", 0, 0),
        (at(12, 8, 10), "Zq Source Alpha", None, "refused:zq_made_up_code", 0, 0),
        (at(12, 8, 15), "Zq Source Alpha", 200, "prompt_injection,hidden_unicode", 1, 2000),
        (at(12, 8, 20), "Zq Gone Source", 200, None, 0, 300),
    ]:
        add_fetch(conn, ts, src, status, err, ch, nb)
    conn.execute("INSERT INTO feedback(suggestion_id, at, status, reason) VALUES (1, ?, 'accepted', 'Zq Title One')", (at(11, 15),))
    for ts, sev, codes, act in [(at(12, 8, 15), "high", "prompt_injection,hidden_unicode", "document_flagged"),
                                (at(11, 9), "integrity", "cert_error", "quarantine_source"),
                                (at(12, 8, 30), "zq_sev", "zq_unlisted_code", "dropped_item")]:
        conn.execute("INSERT INTO quarantine(ts, source, url_hash, detector_codes, severity, excerpt_hash, action) VALUES (?,?,?,?,?,?,?)",
                     (ts, "Zq Source Beta", "ZQHASH", codes, sev, "ZQHASH", act))
    for n, kind, title, held, tainted in [(1, "hiring", "Zq Title One", 0, 0), (2, "hiring", "Zq Title Two", 1, 0),
                                          (3, "event", "Zq Title Three", 0, 1), (4, "zq_kind", "Zq Title Four", 0, 0),
                                          (5, "person", "Zq Person Name", 0, 0)]:
        add_item(conn, n, kind, title, at(10, 12), held, tainted)
    conn.execute("""INSERT INTO entities(type, key, name, source, legal_basis, fetched_at) VALUES ('person','p1','Zq Entity Person','Zq Source Alpha','x',?)""", (NOW,))
    for alias, key in [("name:zq-a", "company:zq1"), ("board:greenhouse:zq", "company:zq1"), ("name:zq-b", "company:zq2")]:
        conn.execute("INSERT INTO company_aliases(alias, company_key, origin, created_at) VALUES (?,?,?,?)", (alias, key, "name", NOW))
    for n, kind, status in [(1, "cfp_submit", "new"), (2, "cfp_submit", "new"), (3, "cfp_submit", "accepted"),
                            (4, "hiring_signal_outreach", "held"), (5, "tool_outage", "expired"), (6, "zq_kind", "zq_weird")]:
        add_suggestion(conn, n, kind, status, at(11, 11))
    add_run(conn, "claude-haiku-5-5", 10, 1, 1000, 200, 0.5, at(11, 10))
    add_run(conn, "claude-haiku-5-5", 5, 0, 500, 100, 0.25, at(11, 11))
    add_run(conn, "local qwen!!", 3, 0, 300, 30, 0.0, at(12, 9))
    conn.execute("""INSERT INTO qualifications(run_id, profile, subject_key, subject_name, model, prompt_hash, evidence_hash, status, created_at)
                    VALUES (1,'Zq Profile Name','company:zq1','Zq Subject Company','m','h','e','ok',?)""", (NOW,))
    conn.close()
    if explorer:
        from ninshubur.explorer import store
        ec = store.connect(home)
        for i, status in enumerate(["candidate", "candidate", "trial", "admitted", "rejected"]):
            ec.execute("""INSERT INTO explorer_candidates(url, host, kind, via, status, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)""",
                       (f"https://zq-explorer.example/{i}", "zq-explorer.example", "hn", "Zq Source Alpha", status, NOW, NOW))
        ec.close()
    return db_path(home)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def run(home, capsys, monkeypatch):
    from ninshubur.__main__ import main
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)

    def call(*args: str) -> tuple[int, str]:
        code = main(list(args))
        out = capsys.readouterr()
        return code, out.out + out.err
    return call


def report(home, **kw) -> dict:
    conn = connect_readonly(home)
    try:
        return RR.build(conn, now=NOW, explorer_db=home / "explorer" / "explorer.db", **kw)
    finally:
        conn.close()


def leaked(text: str) -> list[str]:
    return [w for w in DISTINCTIVE if w.lower() in text.lower()]


def test_the_counts_are_the_hand_worked_ones(home):
    build_db(home, explorer=True)
    s = report(home)["sections"]
    assert s["window"] == {"first_fetch": "2026-10-10 10:00:00 UTC", "last_fetch": "2026-10-12 08:20:00 UTC", "fetches": 11,
                           "days_with_a_fetch": 3, "days_fetched_without_human_feedback": 2}  # feedback only on the 11th
    f = s["fetches"]
    assert (f["fetches"], f["bytes"], f["changed"], f["with_an_error_recorded"]) == (11, 3900, 2, 5)
    assert f["by_status_class"] == {"2xx": 4, "3xx": 1, "4xx": 1, "5xx": 1, "no response": 4}
    assert f["by_adapter"] == {
        "greenhouse": {"fetches": 7, "2xx": 3, "non-2xx": 1, "no response": 3},
        "lever": {"fetches": 3, "2xx": 0, "non-2xx": 2, "no response": 1},
        "source no longer in the database": {"fetches": 1, "2xx": 1, "non-2xx": 0, "no response": 0}}
    r = s["refusals"]
    assert r["gate_refusals"] == {"other": 1, "robots_disallow": 2}
    assert r["fetch_findings"] == {"hidden_unicode": 1, "prompt_injection": 1}
    assert r["quarantine"] == {"rows": 3, "by_severity": {"high": 1, "integrity": 1, "other": 1},
                               "by_code": {"cert_error": 1, "hidden_unicode": 1, "other": 1, "prompt_injection": 1}}
    assert s["items"] == {"items": 5, "by_kind": {"event": {"items": 1, "held": 0, "tainted": 1}, "hiring": {"items": 2, "held": 1, "tainted": 0},
                                                  "other": {"items": 1, "held": 0, "tainted": 0}, "person": {"items": 1, "held": 0, "tainted": 0}}}
    assert s["companies"] == {"canonical_groups": 2, "aliases": 3}
    assert s["suggestions"] == {"suggestions": 6, "by_kind_and_status": {
        "cfp_submit": {"accepted": 1, "new": 2}, "hiring_signal_outreach": {"held": 1}, "other": {"other": 1}, "tool_outage": {"expired": 1}}}
    assert s["sources"] == {"sources": 3, "by_adapter": {"greenhouse": {"sources": 2, "enabled": 1, "health": {"broken": 1, "ok": 1}},
                                                         "lever": {"sources": 1, "enabled": 1, "health": {"quarantined": 1}}}}
    assert s["llm_runs"]["by_model"]["claude-haiku-5-5"] == {"runs": 2, "answered": 15, "failed": 1, "input_tokens": 1500,
                                                             "output_tokens": 300, "usd_as_recorded": 0.75}
    assert s["llm_runs"]["by_model"]["other (not a plain model name)"]["runs"] == 1 and s["llm_runs"]["runs"] == 3
    assert s["explorer"] == {"by_state": {"candidate": 2, "trial": 1, "admitted": 1, "rejected": 1}, "candidates": 5}


def test_days_limits_the_time_stamped_sections(home):
    build_db(home)
    s = report(home, days=1)["sections"]  # since 2026-10-11 12:00 UTC: only the 12th
    assert s["window"]["fetches"] == 5 and s["window"]["days_with_a_fetch"] == 1 and s["window"]["first_fetch"] == "2026-10-12 08:00:00 UTC"
    assert s["fetches"]["by_status_class"] == {"2xx": 2, "no response": 3} and s["fetches"]["bytes"] == 2300
    assert s["refusals"]["quarantine"]["rows"] == 2 and s["refusals"]["gate_refusals"] == {"other": 1, "robots_disallow": 2}
    assert s["llm_runs"]["runs"] == 1 and s["suggestions"] == {"suggestions": 0, "by_kind_and_status": {}}
    assert s["sources"]["sources"] == 3  # a count as of now, not a window


def test_no_name_title_link_or_free_text_reaches_the_page(home):
    build_db(home, explorer=True)
    rep = report(home)
    for text in (RR.render_markdown(rep), RR.to_json(rep)):
        assert leaked(text) == []
        assert "unattended" not in text.lower()


def test_the_cli_writes_a_private_page_and_never_touches_the_database(run, home, monkeypatch):
    path = build_db(home, explorer=True)
    before = sha(path)

    def boom(*a, **k):
        raise AssertionError("run-report must not open the main database through connect()")
    monkeypatch.setattr("ninshubur.__main__.connect", boom)
    out_file = home / "feeds" / "page.md"
    code, out = run("export", "run-report", "-o", str(out_file))
    assert code == 0 and f"written {out_file}" in out
    text = out_file.read_text(encoding="utf-8")
    assert text.startswith("# Ninshubur run report, 2026-10-12\n")
    assert "Window: first fetch 2026-10-10 10:00:00 UTC, last fetch 2026-10-12 08:20:00 UTC (everything stored), 11 fetches." in text
    assert "- Days with at least one fetch: 3" in text and "- Days fetched without human feedback: 2" in text
    assert "| greenhouse | 7 | 3 | 1 | 3 |" in text and "| claude-haiku-5-5 | 2 | 15 | 1 | 1,500 | 300 | 0.7500 |" in text
    assert "| robots_disallow | 2 |" in text and "| prompt_injection (high) | 1 |" in text and "2 canonical company groups" in text
    assert "| candidate | 2 |" in text and "ninshubur export run-report`." in text
    assert leaked(text) == []
    assert sha(path) == before and not [p for p in home.glob("ninshubur.db-*")]
    if os.name == "posix":
        assert oct(os.stat(out_file).st_mode & 0o777) == "0o600"
    code, out = run("export", "run-report", "--format", "json", "--days", "2", "-o", str(home / "feeds" / "page.json"))
    data = json.loads((home / "feeds" / "page.json").read_text(encoding="utf-8"))
    assert code == 0 and data["days"] == 2 and data["sections"]["fetches"]["fetches"] == 9  # since 2026-10-10 12:00: the first two are out
    assert sha(path) == before


def test_the_default_name_is_dated_and_the_options_are_checked(run, home):
    build_db(home)
    code, out = run("export", "run-report")
    assert code == 0 and (home / "feeds" / "run-report-2026-10-12.md").is_file()
    assert run("export", "run-report", "--format", "html")[0] == 2
    assert run("export", "run-report", "--days", "0")[0] == 2
    assert run("export", "digest", "--format", "json")[0] == 2  # json is for run-report only


def test_no_database_says_so_and_creates_none(run, home):
    code, out = run("export", "run-report")
    assert code == 2 and "nothing to report" in out
    assert not list(home.glob("ninshubur.db*"))


def test_a_missing_table_leaves_its_section_out_with_one_line_why(home):
    build_db(home)
    conn = connect(home)
    conn.execute("DROP TABLE qualifications")
    conn.execute("DROP TABLE llm_runs")
    conn.execute("DROP TABLE company_aliases")
    conn.close()
    rep = report(home)
    text = RR.render_markdown(rep)
    assert "## Model runs\n\nLeft out: table `llm_runs`: this database has no such table (an older schema).\n" in text
    assert "## Companies\n\nLeft out: table `company_aliases`" in text
    assert "## Fetches" in text and "| greenhouse | 7 | 3 | 1 | 3 |" in text  # the rest is still there
    assert {x["section"] for x in rep["left_out"]} >= {"llm_runs", "companies", "explorer"}
    assert "llm_runs" not in rep["tables_read"] and "fetches" in rep["tables_read"]
    assert "Left out: " in text.split("---")[-1]


def test_a_column_an_older_schema_lacks_leaves_the_section_out_not_the_page(home):
    path = build_db(home)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE items RENAME COLUMN tainted TO tainted_old")
    conn.commit()
    conn.close()
    text = RR.render_markdown(report(home))
    assert "## Items\n\nLeft out: its tables could not be read" in text and "## Fetches" in text


def test_the_explorer_is_not_included_when_its_database_is_absent(home):
    build_db(home)
    rep = report(home)
    text = RR.render_markdown(rep)
    assert "## Explorer ladder\n\nNot included. Left out: the explorer's database is not in this data home" in text
    assert rep["explorer_database_read"] is False


def test_an_empty_window_still_renders(home):
    connect(home).close()
    text = RR.render_markdown(report(home))
    assert "Window: no fetches in everything stored." in text and "- Days fetched without human feedback: 0" in text
    assert "None." in text and leaked(text) == []


# --- the closed lists still match the code that produces the values ------------------------------
def test_every_detector_code_the_code_can_write_is_in_the_closed_list():
    src = (ROOT / "ninshubur" / "detectors.py").read_text(encoding="utf-8")
    found = dict((c, s) for s, c in re.findall(r'Finding\("([a-z]+)", "([a-z0-9_]+)"', src))
    assert len(found) >= 17
    for code, sev in found.items():
        assert RR.DETECTOR_SEVERITY.get(code) == sev, code
    for code in ("url_ip_literal", "url_punycode", "url_userinfo", "schema_drift"):
        assert RR.DETECTOR_SEVERITY[code] == "high"
    assert set(RR.DETECTOR_SEVERITY.values()) <= set(SEVERITIES)


def test_every_refusal_code_the_gate_can_write_is_in_the_closed_list():
    policy = (ROOT / "ninshubur" / "policy.py").read_text(encoding="utf-8")
    fetch = (ROOT / "ninshubur" / "fetch.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'return "(url_[a-z_]+)"', policy)) | set(re.findall(r'Refusal\("([a-z_]+)"', policy + fetch))
    assert len(codes) >= 30
    assert codes <= RR.REFUSAL_CODES, sorted(codes - RR.REFUSAL_CODES)


def test_suggestion_kinds_statuses_and_source_health_are_the_ones_the_code_writes():
    suggest = (ROOT / "ninshubur" / "analyze" / "suggest.py").read_text(encoding="utf-8")
    assert set(re.findall(r'Suggestion\(kind="([a-z_]+)"', suggest)) <= set(RR.SUGGESTION_KINDS)
    from ninshubur.analyze.suggest import DECIDED, LIVE
    assert set(LIVE) | set(DECIDED) | {"expired", "redacted"} == set(RR.SUGGESTION_STATUSES)
    ingest = (ROOT / "ninshubur" / "ingest.py").read_text(encoding="utf-8")
    sched = (ROOT / "ninshubur" / "schedule.py").read_text(encoding="utf-8")
    assert set(re.findall(r'set_health\([a-z_.]+, "([a-z]+)"', ingest + sched)) <= set(RR.SOURCE_HEALTH)


def test_the_explorers_inbox_source_is_a_known_adapter():
    from ninshubur.explorer import gate, handoff
    assert gate.SOURCE.adapter in RR.adapter_names() and handoff.SOURCE.adapter in RR.adapter_names()
    assert "greenhouse" in RR.adapter_names() and "explorer" not in __import__("ninshubur.adapters.base", fromlist=["x"]).all_adapters()


def test_status_classes():
    assert [RR.status_class(s) for s in (None, 200, 304, 404, 503, 599, 99, 700)] == ["no response", "2xx", "3xx", "4xx", "5xx", "5xx", "other", "other"]
