from __future__ import annotations

from ninshubur.db import rows, one
from ninshubur.logs import read_jsonl
from ninshubur.schedule import status_summary
from tests.conftest import NOW, SAMPLE_ROUTES, Env, grep_home, hn_router, load_sample, write_config


def routes_with_hn() -> dict:
    r = dict(SAMPLE_ROUTES)
    r[("hn.algolia.com", "/api/v1/search_by_date")] = hn_router(load_sample("hn/freelancer_stories.json"), load_sample("hn/comments.json"))
    return r


def test_run_once_end_to_end(home):
    write_config(home)
    env = Env(home, routes_with_hn())
    reports = env.run_once()
    by = {r.source: r for r in reports}
    assert set(by) == {"confs", "acme-jobs", "hn", "claude-status", "event-sites"}
    # only adapter hosts (and the event hosts from the admitted dataset) were contacted
    assert env.seen.hosts() <= {"raw.githubusercontent.com", "boards-api.greenhouse.io", "hn.algolia.com", "status.claude.com",
                                "pycon.pt", "www.saastock.com", "growthsummit.example"}
    assert by["confs"].stored == 3 and by["acme-jobs"].stored == 3
    assert by["claude-status"].stored == 4
    # HN: stories fetched, then the fresh freelancer thread's comments as a follow-up (identifier-templated)
    hn_paths = [c for c in env.seen.calls if c[0] == "hn.algolia.com"]
    assert len(hn_paths) == 4  # robots + latest threads + freelancer search + ONE comments page (deduped across plans)
    assert by["hn"].fetched == 3
    hn = by["hn"]
    assert hn.dropped == 1  # the injection comment never stored
    assert hn.held >= 1 and env.source("hn")["health"] == "suspect"
    assert env.source("confs")["health"] == "ok" and env.source("acme-jobs")["health"] == "ok"
    items = rows(env.conn, "SELECT source, kind, title, tainted, held, taint_codes FROM items ORDER BY source, title")
    hn_items = [i for i in items if i["source"] == "hn"]
    assert not any("Ignore all previous" in i["title"] for i in hn_items)
    assert grep_home(home, "Ignore all previous instructions") == []
    ops = next(i for i in hn_items if "outbound SDR contractor" in i["title"])
    assert ops["tainted"] == 1 and ops["held"] == 1 and "shell_fragment" in ops["taint_codes"]
    q = rows(env.conn, "SELECT * FROM quarantine")
    assert len(q) == 1 and q[0]["action"] == "dropped_item" and "prompt_injection" in q[0]["detector_codes"]
    assert q[0]["excerpt_hash"] and len(q[0]["excerpt_hash"]) == 32
    # snapshots for clean sources only
    snaps = {s["source"] for s in rows(env.conn, "SELECT source FROM snapshots")}
    assert snaps >= {"confs", "acme-jobs", "claude-status"}
    # candidates recorded, never fetched
    cands = rows(env.conn, "SELECT host FROM candidates")
    assert {c["host"] for c in cands} >= {"pycon.pt", "www.saastock.com", "example.org"}
    assert "example.org" not in env.seen.hosts()  # a candidate from HN content, never fetched
    # entities: company from greenhouse
    ents = rows(env.conn, "SELECT type, key, name FROM entities")
    assert ("company", "company:acme.example", "Acme Inc") in {(e["type"], e["key"], e["name"]) for e in ents}
    pol = read_jsonl(home / "logs" / "policy.jsonl")
    assert {p["event"] for p in pol} >= {"quarantine", "health", "tainted_items"}
    summary = status_summary(env.conn, NOW)
    assert summary["by_health"]["suspect"] >= 1 and summary["items_held"] >= 1 and summary["candidates_new"] >= 3


def test_second_run_is_conditional_and_grows_interval(home):
    write_config(home, {"confs": dict(__import__("tests.conftest", fromlist=["DEFAULT_SOURCES"]).DEFAULT_SOURCES["confs"])})
    env = Env(home, SAMPLE_ROUTES)
    r1 = env.run_once()[0]
    assert r1.changed == 1
    interval1 = env.source("confs")["interval_s"]
    assert env.source("confs")["next_due_at"] > NOW
    assert env.run_once() == []  # not due yet
    env.clock.advance(2 * 86400)
    r2 = env.run_once()[0]
    assert r2.unchanged == 1 and r2.changed == 0
    assert env.source("confs")["interval_s"] > interval1
    log = read_jsonl(home / "logs" / "fetch.jsonl")
    assert [l["status"] for l in log if l["url"].endswith("general.json")] == [200, 304]
    assert log[-1]["conditional"] is True


def test_errors_back_off_and_break(home):
    from tests.conftest import DEFAULT_SOURCES
    write_config(home, {"confs": DEFAULT_SOURCES["confs"]})
    routes = {("raw.githubusercontent.com", "/robots.txt"): {"status": 404, "body": ""},
              ("raw.githubusercontent.com", "*"): {"status": 404, "body": "gone"}}
    env = Env(home, routes)
    intervals = []
    for _ in range(5):
        env.run_once(force=True)
        intervals.append(env.source("confs")["interval_s"])
    assert intervals == sorted(intervals) and intervals[-1] > intervals[0]
    assert env.source("confs")["health"] == "broken"
    assert env.run_once(force=True) == []  # broken sources are not run


def test_removed_source_is_disabled_and_review_overdue_stops_fetching(home):
    from tests.conftest import DEFAULT_SOURCES
    write_config(home, {"confs": DEFAULT_SOURCES["confs"]})
    env = Env(home, SAMPLE_ROUTES)
    env.run_once()
    assert env.source("confs")["health"] == "ok"
    write_config(home, {})
    env2 = Env(home, SAMPLE_ROUTES)
    env2.run_once()
    assert env2.source("confs")["health"] == "disabled"
    stale = dict(DEFAULT_SOURCES["confs"])
    stale["trust"] = {**stale["trust"], "reviewed_at": "2026-01-01"}  # 120 days passed
    write_config(home, {"confs": stale})
    env3 = Env(home, SAMPLE_ROUTES)
    assert env3.run_once(force=True) == []
    assert env3.source("confs")["health"] == "review_overdue"
    assert env3.seen.calls == []


def test_clear_restores_a_quarantined_source(home):
    from tests.conftest import DEFAULT_SOURCES
    write_config(home, {"confs": DEFAULT_SOURCES["confs"]})
    routes = {("raw.githubusercontent.com", "/robots.txt"): {"status": 404, "body": ""},
              ("raw.githubusercontent.com", "*"): {"status": 302, "headers": {"Location": "https://evil.example/"}, "body": ""}}
    env = Env(home, routes)
    r = env.run_once()[0]
    assert env.source("confs")["health"] == "quarantined" and r.ingests[0].codes == ["off_host_redirect"]
    assert env.run_once(force=True) == []
    row = env.ingestor.clear("confs")
    assert row["health"] == "ok" and abs(row["reputation"] - 0.75 * 0.8) < 1e-9
    assert one(env.conn, "SELECT COUNT(*) AS n FROM taint_events")["n"] == 0
