"""Ashby end to end: the robots.txt 401 on api.ashbyhq.com, the reviewed exception, items, companies, suggestions."""
from __future__ import annotations

from ninshubur.analyze.suggest import Suggester
from ninshubur.db import rows
from ninshubur.profiles import load_profiles
from tests.conftest import Env, grep_home, load_sample, trust, write_config, write_profiles

ROUTES = {
    ("api.ashbyhq.com", "/robots.txt"): {"status": 401, "body": "Unauthorized"},
    ("api.ashbyhq.com", "/posting-api/job-board/acme"): {"body": load_sample("ashby/jobs.json"), "etag": 'W/"a1"'},
}


def _config(home, **trust_over):
    write_config(home, {"ab-jobs": {
        "adapter": "ashby",
        "params": {"boards": ["acme"], "companies": {"acme": {"name": "Acme Inc", "domain": "acme.example"}}},
        "trust": trust(0, "ashby_jobs.v1", **trust_over)}})


def test_ashby_without_the_reviewed_status_is_refused_by_robots(home):
    _config(home)
    env = Env(home, ROUTES)
    (report,) = env.run_once()
    assert report.refused == 1 and report.changed == 0  # `fetched` counts attempts, refused ones included
    assert [c[1] for c in env.seen.calls] == ["/robots.txt"]  # the job board itself was never asked
    assert rows(env.conn, "SELECT id FROM items") == []


def test_ashby_with_the_reviewed_status_stores_jobs_and_groups_them_by_company(home):
    _config(home, robots_reviewed_status=401)
    write_profiles(home, {"p": {"titles": ["Head of Growth"], "keywords": ["sdr"],
                                "target_companies": [{"name": "Acme Inc", "ashby": "acme"}]}})
    env = Env(home, ROUTES)
    (report,) = env.run_once()
    assert report.fetched == 1 and report.refused == 0
    items = rows(env.conn, "SELECT source, kind, title, url FROM items ORDER BY title")
    assert [i["title"] for i in items] == ["Head of Growth", "Sales Development Representative", "Senior Backend Engineer"]
    assert all(i["url"].startswith("https://jobs.ashbyhq.com/acme/") for i in items)
    assert grep_home(home, "DESCRIPTION-MARKER") == []  # descriptions never reach the database or logs
    assert env.source("ab-jobs")["health"] == "ok"
    # a profile target named by its Ashby board joins the company from config: one suggestion, all postings
    sug = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    hiring = sug.build_hiring_signal_outreach(sug.profiles["p"])
    assert len(hiring) == 1 and hiring[0].entity_keys[0] == "company:acme.example" and len(hiring[0].item_ids) == 3


def test_sources_add_admits_ashby_only_with_the_reviewed_status(home, capsys, monkeypatch):
    """The refusal names the fix; with the flag the source is admitted and the status is written to config."""
    import yaml

    import ninshubur.__main__ as cli_mod
    from tests.conftest import NOW, make_transport, public_resolver
    from tests.conftest import write_profiles as wp

    write_config(home, {})
    wp(home)
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    monkeypatch.setattr("ninshubur.fetch.resolve_host", public_resolver)
    real = cli_mod.App
    monkeypatch.setattr(cli_mod, "App", lambda **kw: real(transport=make_transport(ROUTES), **kw))
    base = ["sources", "add", "ab-jobs", "--adapter", "ashby", "--param", "boards=[acme]", "--tier", "0",
            "--owner", "Ashby public job posting API", "--owner-url", "https://developers.ashbyhq.com/docs/public-job-posting-api",
            "--terms-url", "https://www.ashbyhq.com/resources/terms", "--licence", "public API",
            "--rate-limit", "ETag; <= 4/day", "--reviewed-by", "alex"]
    assert cli_mod.main(base) == 1
    out = capsys.readouterr()
    assert "H2 FAIL" in out.out and "REFUSED" in out.err and "ab-jobs" not in (home / "config.yaml").read_text()
    refusal = [e for e in read_policy(home) if e["event"] == "refused_fetch"][-1]
    assert "answers 401" in refusal["reason"] and "robots_reviewed_status: 401" in refusal["reason"]
    assert cli_mod.main(base + ["--robots-reviewed-status", "401"]) == 0
    out = capsys.readouterr()
    assert "H2 PASS" in out.out and "robots.txt answers 401, reviewed exception" in out.out
    saved = yaml.safe_load((home / "config.yaml").read_text())["sources"]["ab-jobs"]
    assert saved["trust"]["robots_reviewed_status"] == 401 and saved["adapter"] == "ashby"


def read_policy(home):
    from ninshubur.logs import read_jsonl
    return read_jsonl(home / "logs" / "policy.jsonl")
