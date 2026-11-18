"""Lever end to end: robots.txt on api.lever.co, items, companies, suggestions."""
from __future__ import annotations

from ninshubur.analyze.suggest import Suggester
from ninshubur.db import rows
from ninshubur.profiles import load_profiles
from tests.conftest import Env, grep_home, load_sample, trust, write_config, write_profiles

# api.lever.co's real robots.txt, as fetched on 18 Nov 2026
ROBOTS = {"body": "User-agent: *\nAllow: /\nCrawl-delay: 1\n", "headers": {"Content-Type": "text/plain; charset=UTF-8"}}
ROUTES = {
    ("api.lever.co", "/robots.txt"): ROBOTS,
    ("api.lever.co", "/v0/postings/acme"): {"body": load_sample("lever/postings.json"), "etag": 'W/"l1"'},
}


def _config(home):
    # one page: the test transport answers by path, so a second page would repeat the first
    write_config(home, {"lv-jobs": {
        "adapter": "lever",
        "params": {"boards": ["acme"], "max_postings": 200, "companies": {"acme": {"name": "Acme Inc", "domain": "acme.example"}}},
        "trust": trust(0, "lever_postings.v1")}})


def test_lever_stores_jobs_and_groups_them_by_company(home):
    _config(home)
    write_profiles(home, {"p": {"titles": ["Head of Growth"], "keywords": ["sdr"],
                                "target_companies": [{"name": "Acme Inc", "lever": "acme"}]}})
    env = Env(home, ROUTES)
    (report,) = env.run_once()
    assert report.fetched == 1 and report.refused == 0 and report.errors == 0
    assert [c for c in env.seen.calls if c[0] == "api.lever.co"][-1][1] == "/v0/postings/acme"
    items = rows(env.conn, "SELECT title, url FROM items ORDER BY title")
    assert [i["title"] for i in items] == ["Data Analyst", "Head of Growth", "Sales Development Representative",
                                           "Senior Backend Engineer"]
    assert all(i["url"].startswith("https://jobs.lever.co/acme/") for i in items)
    assert grep_home(home, "DESCRIPTION-MARKER") == []  # descriptions never reach the database or logs
    assert env.source("lv-jobs")["health"] == "ok"
    # a profile target named by its Lever site joins the company from config: one suggestion, all postings
    sug = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    hiring = sug.build_hiring_signal_outreach(sug.profiles["p"])
    assert len(hiring) == 1 and hiring[0].entity_keys[0] == "company:acme.example" and len(hiring[0].item_ids) == 4
