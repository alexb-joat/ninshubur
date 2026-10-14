from __future__ import annotations

import json
import os
import stat

import pytest

from ninshubur.analyze import relevance as rel
from ninshubur.analyze import timing
from ninshubur.analyze.suggest import Suggester, fingerprint
from ninshubur.db import one, rows
from ninshubur.feedback import Feedback, FeedbackError
from ninshubur.profiles import load_profiles
from ninshubur.sinks.ics import IcsSink
from tests.conftest import NOW, SAMPLE_ROUTES, Env, hn_router, load_sample, write_config, write_profiles

DAY = 86400.0


def build_env(home):
    write_config(home)
    write_profiles(home)
    routes = dict(SAMPLE_ROUTES)
    routes[("hn.algolia.com", "/api/v1/search_by_date")] = hn_router(load_sample("hn/freelancer_stories.json"), load_sample("hn/comments.json"))
    env = Env(home, routes)
    env.run_once()
    profiles = load_profiles(home)
    sug = Suggester(env.conn, env.policy, env.cfg, profiles, now=env.clock.now)
    return env, sug


def test_lexical_and_recency():
    p = load_profiles.__globals__["Profile"](name="p", keywords=["lead generation", "outbound"], negative_keywords=["internship"], pains=["sdr hiring"])
    s, matched, neg = rel.lexical("We need outbound lead generation help", p)
    assert matched == ("lead generation", "outbound") and s == pytest.approx(0.65) and neg == ()
    s2, _, neg2 = rel.lexical("outbound internship", p)
    assert neg2 == ("internship",) and s2 == pytest.approx(0.15)
    assert rel.lexical("leadership generation", p)[1] == ()  # whole words only
    assert rel.recency(NOW, "service_request", NOW) == 1.0
    assert rel.recency(NOW - 7 * DAY, "service_request", NOW) == pytest.approx(0.5)
    r = rel.score("outbound lead generation", "service_request", NOW, 0.5, p, NOW)
    assert r.score == pytest.approx(0.70 * 0.65 + 0.20 + 0.05, abs=1e-3)


def test_timing_windows():
    w = timing.cfp(NOW + 30 * DAY, NOW)
    assert w.start == NOW and w.end == NOW + 27 * DAY and w.deadline == NOW + 30 * DAY and w.factor(NOW) == 1.0
    assert w.factor(NOW + 31 * DAY) == 0.0
    t = timing.event_trip(NOW + 50 * DAY, NOW)
    assert t.decide_by == NOW + 5 * DAY and t.deadline == NOW + 5 * DAY and t.start == NOW + 29 * DAY
    late = timing.event_trip(NOW + 20 * DAY, NOW)
    assert late.deadline == NOW + 17 * DAY  # decide-by passed: deadline becomes start - 3 d
    h = timing.hiring_post(NOW)
    assert (h.start, h.end, h.deadline) == (NOW + 2 * DAY, NOW + 21 * DAY, NOW + 30 * DAY)
    assert 0.4 <= h.factor(NOW) < 1.0  # a day before the window opens


def test_fingerprint_is_stable_within_a_week_bucket():
    a = fingerprint("cfp_submit", ["event:x"], NOW)
    assert a == fingerprint("cfp_submit", ["event:x"], NOW + 3600) and len(a) == 24
    assert a != fingerprint("cfp_submit", ["event:y"], NOW)


def test_builders_end_to_end(home):
    env, sug = build_env(home)
    built = sug.build_all()
    by_kind: dict[str, list] = {}
    for s in built:
        by_kind.setdefault(s.kind, []).append(s)
    # cfp_submit: PyCon PT (CFP 2026-10-30, 18 d) and Remote Growth Summit (2026-10-20, 8 d, topic general? no; keyword 'growth' in title)
    cfp_titles = {s.title for s in by_kind["cfp_submit"]}
    assert any("PyCon Portugal" in t for t in cfp_titles) and any("Remote Growth Summit" in t for t in cfp_titles)
    pycon = next(s for s in by_kind["cfp_submit"] if "PyCon" in s.title)
    assert pycon.status == "new" and pycon.deadline is not None and pycon.evidence[0]["tier"] == 1
    assert pycon.evidence[0]["values"]["city"] == "Lisbon" and pycon.evidence[0]["fetched_at"] >= NOW
    assert any("value =" in r for r in pycon.reasons)
    # conference_trip: Lisbon (home base -> 0 travel) and Dublin (profile city), not the online summit
    trips = {s.title: s for s in by_kind["conference_trip"]}
    assert any("Lisbon" in t for t in trips) and any("Dublin" in t for t in trips) and not any("Remote Growth" in t for t in trips)
    lis = next(s for t, s in trips.items() if "Lisbon" in t)
    assert lis.cost_parts["travel_usd"] == 0.0 and lis.est_cost == pytest.approx(240 / 60 * 60)
    # service_request_lead: the HN source went suspect (one injection comment), so every lead from it is held:
    # founder_a (clean item, suspect source) and ops_c (medium taint). Nothing from a suspect source reaches "new".
    leads = {s.title: s for s in by_kind["service_request_lead"]}
    clean = next(s for t, s in leads.items() if "We need help with outbound" in t)
    assert clean.status == "held" and clean.held_reason == "source_suspect" and clean.confidence <= 0.5  # Tier 3 alone caps at 0.5
    tainted = next(s for t, s in leads.items() if "outbound SDR contractor" in t)
    assert tainted.status == "held" and tainted.held_reason == "evidence_tainted"
    assert not any("Ignore all previous" in s.title for s in built)
    # hiring_signal_outreach: Acme is a target company -> all three postings, SDR first by title match too
    hires = by_kind["hiring_signal_outreach"]
    assert len(hires) == 3 and all(s.evidence[0]["tier"] == 0 for s in hires)
    assert all(s.entity_keys[0] == "company:acme.example" for s in hires)
    # tool_outage: major incident on anthropic/claude, tools_in_use matches
    out = by_kind["tool_outage"]
    assert len(out) == 1 and out[0].est_cost == 0 and "major impact" in out[0].reasons[0]
    # persisted + ranked
    listed = sug.list(top=50)
    assert listed and listed[0]["roi"] >= listed[-1]["roi"] and all(s["status"] == "new" for s in listed)
    assert sug.list(status=("held",))
    # rebuild is idempotent (same fingerprints, no duplicates)
    n1 = one(env.conn, "SELECT COUNT(*) AS n FROM suggestions")["n"]
    sug.build_all()
    assert one(env.conn, "SELECT COUNT(*) AS n FROM suggestions")["n"] == n1


def test_plan_knapsack(home):
    env, sug = build_env(home)
    sug.build_all()
    picked = sug.plan(30)
    assert sum(s["cost_parts"]["human_minutes"] for s in picked) <= 30 and picked


def test_feedback_and_ics(home):
    env, sug = build_env(home)
    sug.build_all()
    ics = IcsSink(env.conn, home, now=env.clock.now)
    fb = Feedback(env.conn, [ics], now=env.clock.now)
    new = sug.list(top=100)
    held = sug.list(status=("held",))
    with pytest.raises(FeedbackError):
        fb.accept(held[0]["id"])  # held never leaves the machine
    target = next(s for s in new if s["kind"] == "cfp_submit")
    s, pushed = fb.accept(target["id"])
    assert s["status"] == "accepted" and pushed == [("ics", f"{s['fingerprint']}@ninshubur")]
    s2, pushed2 = fb.accept(target["id"])
    assert pushed2 == []  # idempotent
    text = (home / "feeds" / "timings.ics").read_text()
    assert text.startswith("BEGIN:VCALENDAR") and f"UID:{s['fingerprint']}-deadline@ninshubur" in text
    assert "Deadline: Submit a talk to PyCon Portugal" in text.replace("\r\n ", "")
    if os.name == "posix":
        assert stat.S_IMODE((home / "feeds" / "timings.ics").stat().st_mode) == 0o600
    d, _ = fb.done(target["id"], "won", minutes=90)
    assert d["status"] == "done"
    assert rows(env.conn, "SELECT * FROM feedback")[-1]["outcome"] == "won"
    assert one(env.conn, "SELECT SUM(amount) AS m FROM budget_ledger WHERE kind='human_minutes'")["m"] == 90
    cal = one(env.conn, "SELECT * FROM calibration WHERE kind='cfp_submit'")
    assert cal["accepted"] == 1 and cal["won"] == 1
    other = next(s for s in new if s["kind"] == "hiring_signal_outreach")
    assert fb.dismiss(other["id"], "not now")["status"] == "dismissed"
    # a rebuild keeps human decisions
    sug.build_all()
    assert sug.get(target["id"])["status"] == "done" and sug.get(other["id"])["status"] == "dismissed"
    assert "Submit a talk to PyCon Portugal" not in (home / "feeds" / "timings.ics").read_text()  # done -> not in feed


def test_expiry_and_outage_resolution(home):
    env, sug = build_env(home)
    sug.build_all()
    outage = sug.list(kind="tool_outage")[0]
    env.conn.execute("UPDATE items SET attrs=json_set(attrs, '$.resolved', json('true')) WHERE kind='incident'")
    assert sug.expire() >= 1 and sug.get(outage["id"])["status"] == "expired"
    env.clock.advance(400 * DAY)
    sug.expire()
    assert sug.list(top=100) == []


def test_corroboration_hold_for_tier2_single_source(home):
    env, sug = build_env(home)
    env.conn.execute("UPDATE items SET held=0, tainted=0, taint_codes='' WHERE source='hn'")  # pretend the source is clean
    env.conn.execute("UPDATE sources SET health='ok' WHERE name='hn'")
    env.cfg.data["base_value_usd"]["service_request_lead"] = 5000
    sug2 = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    built = [s for s in sug2.build_service_request_lead(list(sug2.profiles.values())[0])]
    assert built and all(s.status == "held" and s.held_reason == "needs_corroboration" for s in built if s.est_value > 300)


def test_costs_from_config_and_home_country(home):
    env, sug = build_env(home)
    env.cfg.data["costs"] = {"travel_same_city_usd": 0, "travel_same_country_usd": 100, "travel_abroad_usd": 999,
                             "minutes": {"conference_trip": 60}}
    profiles = load_profiles(home)
    p = list(profiles.values())[0]
    p.home_country = "Ireland"  # Dublin is now a domestic trip
    sug2 = Suggester(env.conn, env.policy, env.cfg, profiles, now=env.clock.now)
    trips = {s.title: s for s in sug2.build_conference_trip(p)}
    dublin = next(s for t, s in trips.items() if "Dublin" in t)
    lisbon = next(s for t, s in trips.items() if "Lisbon" in t)
    assert dublin.cost_parts["travel_usd"] == 100 and dublin.cost_parts["human_minutes"] == 60
    assert lisbon.cost_parts["travel_usd"] == 0 and lisbon.est_cost == pytest.approx(60)
    assert "60 min" in dublin.reasons[-1]


def test_topic_only_trip_ranks_below_geo(home):
    env, sug = build_env(home)
    profiles = load_profiles(home)
    p = list(profiles.values())[0]
    p.cities, p.countries, p.topics = [], [], ["general"]
    sug2 = Suggester(env.conn, env.policy, env.cfg, profiles, now=env.clock.now)
    trips = sug2.build_conference_trip(p)
    assert trips and all("profile topic general" in s.reasons[0] for s in trips)


def test_hn_hiring_comment_matches_target_company_by_name(home):
    from tests.conftest import DEFAULT_SOURCES, hn_router, load_sample
    write_config(home, {"hn": DEFAULT_SOURCES["hn"]})
    write_profiles(home)
    routes = {("hn.algolia.com", "/robots.txt"): {"status": 404, "body": ""},
              ("hn.algolia.com", "/api/v1/search_by_date"): hn_router(load_sample("hn/whoishiring_stories.json"), load_sample("hn/whoishiring_comments.json"))}
    env = Env(home, routes)
    env.run_once()
    sug2 = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    built = sug2.build_all()
    hires = [s for s in built if s.kind == "hiring_signal_outreach"]
    acme = next(s for s in hires if "Acme Inc" in s.title)
    assert acme.reasons[0] == "target company Acme Inc" and acme.evidence[0]["tier"] == 3
    assert acme.status == "held"  # the thread had an injection comment -> source suspect; nothing from it is actionable yet
    assert not any("Evil Corp" in s.title for s in built)


def test_hiring_roles_alone_are_a_signal_and_past_decide_by_is_said(home):
    from tests.conftest import DEFAULT_SOURCES, hn_router, load_sample
    write_config(home, {"hn": DEFAULT_SOURCES["hn"], "confs": DEFAULT_SOURCES["confs"]})
    write_profiles(home, {"narrow": {"keywords": ["zzz-nothing"], "geos": {"cities": ["Lisbon"]}, "home_base": "Lisbon"}})
    routes = dict(SAMPLE_ROUTES)
    routes[("hn.algolia.com", "/api/v1/search_by_date")] = hn_router(load_sample("hn/whoishiring_stories.json"), load_sample("hn/whoishiring_comments.json"))
    env = Env(home, routes)
    env.run_once()
    sug2 = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    built = sug2.build_all()
    hires = [s for s in built if s.kind == "hiring_signal_outreach"]
    assert {s.title.split(" is hiring")[0] for s in hires} >= {"Acme Inc", "Nimbus Analytics"}
    nimbus = next(s for s in hires if "Nimbus" in s.title)
    assert nimbus.reasons[0].startswith("hiring for ") and "growth" in nimbus.reasons[0]
    trip = next(s for s in built if s.kind == "conference_trip" and "Lisbon" in s.title)  # PyCon PT starts in 29 d: decide-by passed
    assert trip.steps[1].startswith("Decide-by date has passed")
