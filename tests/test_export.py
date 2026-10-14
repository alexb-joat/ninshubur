from __future__ import annotations

import csv
import io
import os
import stat
from zoneinfo import ZoneInfo

from ninshubur.analyze.suggest import Suggester
from ninshubur.db import one
from ninshubur.export import export, gather, parse_since, render_csv_suggestions, render_html, render_md, weekly_digests
from ninshubur.profiles import load_profiles
from tests.conftest import NOW, SAMPLE_ROUTES, Env, hn_router, load_sample, write_config, write_profiles

TZ = ZoneInfo("Europe/Lisbon")


def built_env(home):
    write_config(home)
    write_profiles(home)
    routes = dict(SAMPLE_ROUTES)
    routes[("hn.algolia.com", "/api/v1/search_by_date")] = hn_router(load_sample("hn/freelancer_stories.json"), load_sample("hn/comments.json"))
    env = Env(home, routes)
    env.run_once()
    sug = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    sug.build_all()
    return env, sug


def test_parse_since():
    assert parse_since("7d", NOW) == NOW - 7 * 86400
    assert parse_since("2w", NOW) == NOW - 14 * 86400
    assert parse_since("12h", NOW) == NOW - 12 * 3600
    assert parse_since(None, NOW) is None


def test_gather_and_renderers(home):
    env, sug = built_env(home)
    d = gather(env.conn, sug, profile="leadget-eu", statuses=("new", "accepted", "held"), since=None, now=env.clock.now(), tz=TZ)
    assert d["suggestions"] and d["held"] and d["sources"] and d["candidates"]
    assert set(d["by_kind"]) >= {"cfp_submit", "conference_prospecting", "hiring_signal_outreach", "tool_outage", "service_request_lead"}
    md = render_md(d)
    assert md.startswith("# Ninshubur digest: leadget-eu")
    assert "## Conference prospecting" in md and "## Held" in md and "## Sources" in md and "## Candidate hosts" in md
    assert "value = " in md and "fetched 2026-10-12" in md and "https://pycon.pt/2026" in md
    assert "source_suspect" in md or "evidence_tainted" in md
    h = render_html(d)
    assert h.startswith("<!doctype html>") and "<script" not in h and "http://" not in h.split("<body>")[1].replace("https://", "")
    assert "@media print" in h and "Print" not in h.split("<body>")[0] and "<h2>Conference prospecting</h2>" in h
    assert "&lt;" not in md  # markdown is not html-escaped
    c = render_csv_suggestions(d)
    assert c.startswith("﻿")
    reader = csv.DictReader(io.StringIO(c.lstrip("﻿")))
    recs = list(reader)
    assert recs and set(recs[0]) >= {"id", "kind", "status", "value_usd", "cost_usd", "roi", "evidence_url", "steps"}
    assert any(r["status"] == "held" for r in recs)


def test_export_writes_private_files(home):
    env, sug = built_env(home)
    now = env.clock.now()
    for fmt in ("md", "html", "csv"):
        p = export(env.conn, sug, home, what="digest", fmt=fmt, profile=None, statuses=("new", "held"), since=None, now=now, tz=TZ)
        assert p.parent == home / "feeds" and p.suffix == f".{fmt}" and p.stat().st_size > 200
        if os.name == "posix":
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
    p = export(env.conn, sug, home, what="items", fmt="csv", profile=None, statuses=(), since=None, now=now, tz=TZ)
    text = p.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == "source,kind,title,url,published_at,city,country,company,dates,tainted,held"
    assert "PyCon Portugal" in text and "event_start 2026-11-10" in text
    assert "Ignore all previous" not in text  # dropped items never reach an export
    custom = home / "out" / "x.md"
    assert export(env.conn, sug, home, what="digest", fmt="md", profile=None, statuses=("new",), since=None, now=now, tz=TZ, out=custom) == custom


def test_since_filter_and_profile_scope(home):
    env, sug = built_env(home)
    now = env.clock.now()
    d_all = gather(env.conn, sug, profile=None, statuses=("new", "held"), since=None, now=now, tz=TZ)
    d_old = gather(env.conn, sug, profile=None, statuses=("new", "held"), since=now + 1, now=now, tz=TZ)
    assert d_all["suggestions"] and d_old["suggestions"] == []
    d_other = gather(env.conn, sug, profile="nobody", statuses=("new",), since=None, now=now, tz=TZ)
    assert d_other["suggestions"] == [] and "0 minutes" in render_md(d_other)


def test_weekly_digest_once_per_week(home):
    env, sug = built_env(home)
    now = env.clock.now()
    first = weekly_digests(env.conn, sug, home, ["leadget-eu"], now, TZ)
    assert len(first) == 1 and first[0].name.startswith("digest-2026-W42-leadget-eu")
    assert weekly_digests(env.conn, sug, home, ["leadget-eu"], now + 3600, TZ) == []
    assert weekly_digests(env.conn, sug, home, ["leadget-eu"], now + 8 * 86400, TZ)
    assert one(env.conn, "SELECT value FROM meta WHERE key='last_digest_week'")["value"] == "2026-W43"


def test_prospect_rows_and_csv(home):
    from ninshubur.export import prospect_rows, render_csv_prospects
    env, sug = built_env(home)
    rows_ = prospect_rows(env.conn, tz=TZ)
    assert rows_ and {r["role"] for r in rows_} >= {"speaker", "sponsor", "partner"}
    jane = next(r for r in rows_ if r["name"] == "Jane Doe")
    assert jane["event"] == "PyCon Portugal" and jane["event_date"] == "2026-11-10" and jane["handle"].endswith("/in/janedoe")
    assert jane["source_url"] == "https://pycon.pt/speakers/" and jane["seen_events"] >= 1
    only = prospect_rows(env.conn, event="nope", tz=TZ)
    assert only == []
    by_profile = prospect_rows(env.conn, profile="leadget-eu", tz=TZ)
    assert by_profile and all(r["event"] in {"PyCon Portugal", "SaaStock Dublin", "Remote Growth Summit"} for r in by_profile)
    csv_text = render_csv_prospects(rows_)
    assert csv_text.startswith("\ufeff") and "hello@expo.example" not in csv_text and "Eve Mallory" not in csv_text
    d = gather(env.conn, sug, profile="leadget-eu", statuses=("new",), since=None, now=env.clock.now(), tz=TZ)
    assert d["prospects"] and "| Role | Name |" in render_md(d) and "<th>Handle</th>" in render_html(d)
