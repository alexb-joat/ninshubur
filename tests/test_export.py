from __future__ import annotations

import csv
import io
import os
import stat
from zoneinfo import ZoneInfo

from ninshubur.analyze.suggest import Suggester
from ninshubur.db import one
from ninshubur.export import _cell, export, gather, parse_since, render_csv_items, render_csv_prospects, render_csv_suggestions, render_html, render_md, weekly_digests
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
    d = gather(env.conn, sug, profile="agency-eu", statuses=("new", "accepted", "held"), since=None, now=env.clock.now(), tz=TZ)
    assert d["suggestions"] and d["held"] and d["sources"] and d["candidates"]
    assert set(d["by_kind"]) >= {"cfp_submit", "conference_prospecting", "hiring_signal_outreach", "tool_outage", "service_request_lead"}
    md = render_md(d)
    assert md.startswith("# Ninshubur digest: agency-eu")
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
    first = weekly_digests(env.conn, sug, home, ["agency-eu"], now, TZ)
    assert len(first) == 1 and first[0].name.startswith("digest-2026-W42-agency-eu")
    assert weekly_digests(env.conn, sug, home, ["agency-eu"], now + 3600, TZ) == []
    assert weekly_digests(env.conn, sug, home, ["agency-eu"], now + 8 * 86400, TZ)
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
    by_profile = prospect_rows(env.conn, profile="agency-eu", tz=TZ)
    assert by_profile and all(r["event"] in {"PyCon Portugal", "SaaStock Dublin", "Remote Growth Summit"} for r in by_profile)
    csv_text = render_csv_prospects(rows_)
    assert csv_text.startswith("\ufeff") and "hello@expo.example" not in csv_text and "Eve Mallory" not in csv_text
    d = gather(env.conn, sug, profile="agency-eu", statuses=("new",), since=None, now=env.clock.now(), tz=TZ)
    assert d["prospects"] and "| Role | Name |" in render_md(d) and "<th>Handle</th>" in render_html(d)


HOSTILE = ["=HYPERLINK(\"http://evil.example\",\"x\")", "+SUM(1+1)", "-2+3", "@SUM(1+1)", "\t=1+1", "\r=1+1", "  =1+1", "\n=1+1"]


def test_cell_rule():
    for bad in HOSTILE:
        assert _cell(bad) == "'" + bad
    assert _cell(None) == "" and _cell("") == "" and _cell("   ") == "   "
    for same in ("Jane Doe", "PyCon Portugal 2026", "a=b", "x-y", "me@example.test", "'=already", "-5", "+3.50", "-1.00", "0.5", "é=ü"):
        assert _cell(same) == same
    assert _cell(7) == 7 and _cell(-1.5) == -1.5 and _cell(0) == 0
    assert _cell(True) is True

    class Sneaky:
        def __str__(self):
            return "=1+1"

    assert _cell(Sneaky()) == "'=1+1" and _cell(["a", "b"]) == "['a', 'b']"


def _parsed(text, columns):
    recs = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert len(recs) > 1 and {len(r) for r in recs} == {columns}
    return recs


def test_csv_separator_inside_a_value_stays_inside_its_cell():
    from ninshubur.export import PROSPECT_COLUMNS
    base = {c: "ok" for c in PROSPECT_COLUMNS}
    text = render_csv_prospects([dict(base, name="a;=1+1", company="x\t=cmd|' /c calc'!A0", seen_events=3)])
    header, row = text.lstrip("\ufeff").splitlines()
    assert header == ",".join(PROSPECT_COLUMNS)  # our own text, bare
    assert '"a;=1+1"' in row and "\"x\t=cmd|' /c calc'!A0\"" in row and '"3"' in row  # every data cell quoted
    assert _parsed(text, len(PROSPECT_COLUMNS))[1][PROSPECT_COLUMNS.index("name")] == "a;=1+1"


def test_csv_prospects_guards_formulas(home):
    from ninshubur.export import PROSPECT_COLUMNS
    base = {c: "ok" for c in PROSPECT_COLUMNS}
    base["seen_events"] = 3
    rows_ = [dict(base, name=h, title=h, company=h) for h in HOSTILE]
    rows_.append(dict(base, name="Ada Example", title="", company=None, seen_events=0))
    recs = _parsed(render_csv_prospects(rows_), len(PROSPECT_COLUMNS))
    ni, ti, ci, si = (PROSPECT_COLUMNS.index(c) for c in ("name", "title", "company", "seen_events"))
    for rec, h in zip(recs[1:], HOSTILE):
        assert rec[ni] == rec[ti] == rec[ci] == "'" + h
    last = recs[-1]
    assert last[ni] == "Ada Example" and last[ti] == "" and last[ci] == "" and last[si] == "0" and last[0] == "ok"


def test_csv_suggestions_guards_formulas(home):
    env, sug = built_env(home)
    d = gather(env.conn, sug, profile="agency-eu", statuses=("new", "accepted", "held"), since=None, now=env.clock.now(), tz=TZ)
    clean = list(csv.reader(io.StringIO(render_csv_suggestions(d).lstrip("\ufeff"))))
    d["suggestions"] = d["suggestions"][:len(HOSTILE)]
    for s, h in zip(d["suggestions"], HOSTILE):
        s["title"] = h
        s["steps"] = [h, "Ordinary step"]
    d["suggestions"][0]["roi"] = -2.5
    recs = _parsed(render_csv_suggestions(d), len(clean[0]))
    title_i, steps_i, roi_i = (clean[0].index(c) for c in ("title", "steps", "roi"))
    for rec, h in zip(recs[1:], HOSTILE):
        assert rec[title_i] == "'" + h and rec[steps_i] == "'" + h + " | Ordinary step"
    assert recs[1][roi_i] == "-2.50"  # a formatted negative number is not a formula
    assert len(recs) == len(d["suggestions"]) + 1 and recs[0] == clean[0]
    assert all(r[title_i] not in HOSTILE for r in recs[1:])


def test_csv_items_guards_formulas(home):
    env, sug = built_env(home)
    clean = _parsed(render_csv_items(env.conn, since=None, tz=TZ), 11)
    ids = [r["id"] for r in env.conn.execute("SELECT id FROM items ORDER BY id LIMIT ?", (len(HOSTILE),)).fetchall()]
    assert len(ids) == len(HOSTILE)
    for i, h in zip(ids, HOSTILE):
        env.conn.execute("UPDATE items SET title=? WHERE id=?", (h, i))
    env.conn.commit()
    recs = _parsed(render_csv_items(env.conn, since=None, tz=TZ), 11)
    assert len(recs) == len(clean)
    titles = {r[2] for r in recs[1:]}
    assert {"'" + h for h in HOSTILE} <= titles and not titles & set(HOSTILE)
    assert [r[3:] for r in recs[1:]] == [r[3:] for r in clean[1:]]  # the other columns are untouched


def test_ics_text_escaping_leaves_no_raw_line_break():
    from ninshubur.sinks.ics import _esc
    hostile = "Talk\rATTACH:https://evil.example/x\r\nBEGIN:VEVENT\nEND"
    out = _esc(hostile)
    assert "\r" not in out and "\n" not in out
    assert out == "Talk\\nATTACH:https://evil.example/x\\nBEGIN:VEVENT\\nEND"
    assert _esc("a;b,c\\d") == "a\\;b\\,c\\\\d"
