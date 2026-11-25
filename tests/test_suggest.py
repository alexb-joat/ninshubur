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
    # conference_prospecting: PyCon PT (4 pages), SaaStock (1 page) and the online summit (3 visible speakers)
    pros = {s.title: s for s in by_kind["conference_prospecting"]}
    assert any("PyCon" in t for t in pros) and any("SaaStock" in t for t in pros)
    assert "conference_trip" not in by_kind
    # service_request_lead: the HN source went suspect (one injection comment), so every lead from it is held:
    # founder_a (clean item, suspect source) and ops_c (medium taint). Nothing from a suspect source reaches "new".
    leads = {s.title: s for s in by_kind["service_request_lead"]}
    clean = next(s for t, s in leads.items() if "We need help with outbound" in t)
    assert clean.status == "held" and clean.held_reason == "source_suspect" and clean.confidence <= 0.5  # Tier 3 alone caps at 0.5
    tainted = next(s for t, s in leads.items() if "outbound SDR contractor" in t)
    assert tainted.status == "held" and tainted.held_reason == "evidence_tainted"
    assert not any("Ignore all previous" in s.title for s in built)
    # hiring_signal_outreach: Acme is a target company -> one suggestion for its three postings, newest first
    hires = by_kind["hiring_signal_outreach"]
    assert len(hires) == 1 and len(hires[0].evidence) == 3 and all(e["tier"] == 0 for e in hires[0].evidence)
    acme = hires[0]
    assert acme.entity_keys == ["company:acme.example"] and acme.item_ids == sorted(acme.item_ids) and len(acme.item_ids) == 3
    assert acme.title == "Acme Inc is hiring: 3 roles (Sales Development Representative)"
    assert acme.evidence[0]["values"]["title"] == "Sales Development Representative" and acme.reasons[1].startswith("3 open postings")
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


def test_feedback_and_ics(home):  # noqa: C901
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


def test_costs_from_config(home):
    env, sug = build_env(home)
    env.cfg.data["costs"] = {"minutes": {"conference_prospecting": 40, "cfp_submit": 30}}
    sug2 = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    p = list(sug2.profiles.values())[0]
    pro = next(s for s in sug2.build_conference_prospecting(p) if "PyCon" in s.title)
    assert pro.cost_parts["human_minutes"] >= 40 and pro.cost_parts["travel_usd"] == 0
    cfp = sug2.build_cfp_submit(p)[0]
    assert cfp.cost_parts["human_minutes"] == 30 and "30 min" in cfp.reasons[-1]


def test_prospecting_phases_upcoming_and_held(home):
    env, sug = build_env(home)
    p = list(sug.profiles.values())[0]
    up = next(s for s in sug.build_conference_prospecting(p) if "PyCon" in s.title)
    assert up.title.startswith("Prospect") and up.entity_keys[1] == "upcoming" and up.deadline < up.expires_at
    env.clock.advance(60 * DAY)  # PyCon PT (Nov 10-12) is now held
    sug2 = Suggester(env.conn, env.policy, env.cfg, sug.profiles, now=env.clock.now)
    held = next(s for s in sug2.build_conference_prospecting(p) if "PyCon" in s.title)
    assert held.title.startswith("Follow up") and held.entity_keys[1] == "held" and held.deadline is None
    assert "paid for a stand" in held.steps[0]
    env.clock.advance(200 * DAY)  # too old
    sug3 = Suggester(env.conn, env.policy, env.cfg, sug.profiles, now=env.clock.now)
    assert not any("PyCon" in s.title for s in sug3.build_conference_prospecting(p))


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
    assert not any(s.kind == "conference_trip" for s in built)


# --- one hiring suggestion per company ----------------------------------------------------------------
TWO_PROFILES = {
    "agency-eu": {"titles": ["Account Executive"], "target_companies": [{"name": "Acme Inc", "domain": "acme.example", "greenhouse": "acme"}]},
    "agency-us": {"titles": ["Account Executive"], "target_companies": [{"name": "Acme Inc", "domain": "acme.example"}]},
}


def company_env(home, profiles=None):
    from tests.test_companies import SOURCES
    write_config(home, SOURCES)
    write_profiles(home, profiles or TWO_PROFILES)
    env = Env(home, {})
    env.scheduler.sync_sources()
    return env, Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now, home=home)


def live_hiring(conn, profile=None):
    q = "SELECT * FROM suggestions WHERE kind='hiring_signal_outreach' AND status IN ('new','held')"
    return [r for r in rows(conn, q) if profile is None or r["profile"] == profile]


def test_one_suggestion_per_company_with_capped_evidence(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    ids = [add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", title=f"Account Executive {i}", at=NOW - (i + 1) * DAY)
           for i in range(8)]
    add_item(env.conn, "hn", "Quiet Labs", title="Account Executive")
    built = [s for s in sug.build_all() if s.kind == "hiring_signal_outreach"]
    assert sorted(s.entity_keys[0] for s in built) == ["company:acme.example", "company:quiet-labs"]
    acme = next(s for s in built if s.entity_keys == ["company:acme.example"])
    assert acme.item_ids == sorted(ids) and acme.title == "Acme Inc is hiring: 8 roles (Account Executive 0)"
    assert [e["values"]["title"] for e in acme.evidence] == [f"Account Executive {i}" for i in range(5)]  # the 5 newest
    assert "8 open postings" in acme.reasons[1] and "evidence lists the 5 most recent" in acme.reasons[1]
    assert acme.best_window[0] == timing.hiring_post(NOW - DAY).start  # window from the newest posting
    assert not any(k.startswith("job:") for s in built for k in s.entity_keys)
    quiet = next(s for s in built if s.entity_keys == ["company:quiet-labs"])
    assert quiet.title == "Quiet Labs is hiring: Account Executive" and quiet.reasons[1].startswith("posted ")


def test_hold_considers_every_posting_and_the_least_trusted_tier(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    for i in range(6):
        add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - (i + 1) * DAY)
    old_hn = add_item(env.conn, "hn", "Acme Inc", at=NOW - 20 * DAY, held=1)  # oldest, Tier 2 source, held
    s = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    assert old_hn in s.item_ids and s.status == "held" and s.held_reason == "source_suspect"
    assert [e["tier"] for e in s.evidence] == [0, 0, 0, 0, 0, 2]  # the weakest posting is shown, so the badge is honest
    env.conn.execute("UPDATE items SET held=0 WHERE id=?", (old_hn,))
    s = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    assert s.status == "new" and s.confidence <= 0.95


def test_a_new_posting_next_week_updates_the_same_suggestion(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY)
    first = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    env.clock.advance(8 * DAY)
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", title="Account Executive II", at=env.clock.now() - 3600)
    second = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    assert second.id == first.id and second.fingerprint == first.fingerprint and len(second.item_ids) == 2
    assert len(live_hiring(env.conn)) == 1
    assert fingerprint("hiring_signal_outreach", second.entity_keys, NOW) != fingerprint("hiring_signal_outreach", second.entity_keys, NOW + 8 * DAY)


def test_two_profiles_matching_one_company_each_get_their_own(home):
    from tests.test_companies import add_item
    env, sug = company_env(home)
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme")
    built = [s for s in sug.build_all() if s.kind == "hiring_signal_outreach"]
    assert sorted(s.profile for s in built) == ["agency-eu", "agency-us"] and len({s.fingerprint for s in built}) == 2
    assert sorted(r["profile"] for r in live_hiring(env.conn)) == ["agency-eu", "agency-us"]


def test_a_decision_sticks_until_the_company_posts_again(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY, first_seen=NOW - DAY)
    s = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    Feedback(env.conn, [], now=env.clock.now).dismiss(s.id, "not now")
    env.clock.advance(3600)
    sug.build_all()
    assert sug.get(s.id)["status"] == "dismissed" and live_hiring(env.conn) == []
    env.clock.advance(DAY)
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", title="Account Executive II", at=env.clock.now(), first_seen=env.clock.now())
    again = next(x for x in sug.build_all() if x.kind == "hiring_signal_outreach")
    assert again.id != s.id and again.status == "new" and sug.get(s.id)["status"] == "dismissed"
    sug.build_all()
    assert [r["id"] for r in live_hiring(env.conn)] == [again.id]


def _old_row(conn, profile, item_id, job, status, expires=NOW + 30 * DAY):
    conn.execute(
        """INSERT INTO suggestions(fingerprint, kind, profile, title, steps, reasons, evidence, confidence, est_value, est_cost,
           expires_at, status, created_at, updated_at, entity_keys, item_ids)
           VALUES (?, 'hiring_signal_outreach', ?, 'old', '[]', '[]', '[]', 0.5, 10, 5, ?, ?, ?, ?, ?, ?)""",
        (f"old-{item_id}-{status}", profile, expires, status, NOW - DAY, NOW - DAY,
         json.dumps(["company:acme.example", f"job:{job}"]), json.dumps([item_id])))


def test_regroup_dry_run_counts_exactly_what_the_rebuild_expires(home):
    from tests.test_companies import add_item
    env, sug = company_env(home)
    a1 = add_item(env.conn, "acme-jobs", "Acme Inc", board="acme")
    a2 = add_item(env.conn, "acme-jobs", "Acme Inc", board="acme")
    q = add_item(env.conn, "hn", "Quiet Labs", title="Account Executive")
    gone = add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - 60 * DAY)  # no longer qualifies
    _old_row(env.conn, "agency-eu", a1, 1, "new")
    _old_row(env.conn, "agency-eu", a2, 2, "held")
    _old_row(env.conn, "agency-us", q, 3, "new")
    _old_row(env.conn, "agency-eu", gone, 4, "new")
    _old_row(env.conn, "agency-eu", a1, 5, "dismissed")
    _old_row(env.conn, "agency-us", a2, 6, "accepted")
    stale = add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - 70 * DAY)
    _old_row(env.conn, "agency-us", stale, 7, "held", expires=NOW - DAY)  # past its own expiry date
    before = env.conn.total_changes
    dry = sug.regroup(dry_run=True)
    assert env.conn.total_changes == before  # counts only
    assert dry == {"live_per_posting": 5, "expire": 3, "companies": 2, "stay_live": 1, "past_expiry": 1, "moved": 0,
                   "new_rows": {"agency-eu": 2, "agency-us": 2}, "decided_untouched": 2}
    company_rows = lambda: [r for r in live_hiring(env.conn) if r["title"] != "old"]  # noqa: E731
    assert company_rows() == []
    sug.build_all()
    assert sug.last_regroup == dry
    made = {}
    for r in company_rows():
        made[r["profile"]] = made.get(r["profile"], 0) + 1
    assert made == dry["new_rows"]  # dry-run rows per profile == rows the real rebuild created
    olds = {r["fingerprint"]: r["status"] for r in rows(env.conn, "SELECT fingerprint, status FROM suggestions WHERE title='old'")}
    assert olds == {f"old-{a1}-new": "expired", f"old-{a2}-held": "expired", f"old-{q}-new": "expired",
                    f"old-{gone}-new": "new", f"old-{stale}-held": "expired", f"old-{a1}-dismissed": "dismissed", f"old-{a2}-accepted": "accepted"}
    assert sug.regroup(dry_run=True)["expire"] == 0


def test_cli_regroup_dry_run(home, capsys, monkeypatch):
    from ninshubur.__main__ import main
    from tests.test_companies import add_item
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    env, _ = company_env(home)
    a1 = add_item(env.conn, "acme-jobs", "Acme Inc", board="acme")
    _old_row(env.conn, "agency-eu", a1, 1, "new")
    _old_row(env.conn, "agency-us", a1, 2, "done")
    assert main(["suggest", "--regroup-dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "dry run, nothing written: 1 live per-posting hiring suggestion(s)",
        "  1 of them belong to 1 company key(s) and expire on the next `ninshubur run`",
        "  2 per-company suggestion row(s) will be created: agency-eu 1, agency-us 1",
        "  0 live per-company suggestion(s) whose postings moved to another key (an alias) expire",
        "  0 are past their own expiry date and expire on the next run anyway",
        "  0 cover no posting that still qualifies; they stay live until their own expiry date",
        "  1 per-posting suggestion(s) a person accepted, dismissed or marked done stay untouched"]
    assert one(env.conn, "SELECT status FROM suggestions WHERE fingerprint=?", (f"old-{a1}-new",))["status"] == "new"


def test_untrusted_name_matches_never_move_trust(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY)
    alone = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    hn = add_item(env.conn, "hn", "Evil Ltd (acme.example)", at=NOW - 3600)
    mixed = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    assert hn in mixed.item_ids and mixed.confidence == alone.confidence and mixed.title.startswith("Acme Inc")
    assert mixed.status == alone.status
    marked = [e for e in mixed.evidence if e["values"].get("match")]
    assert [e["values"]["match"] for e in marked] == ["name only, unverified"]


def test_an_untrusted_only_group_counts_as_one_source_and_is_held(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"wide": {"titles": ["Account Executive"]}})
    add_item(env.conn, "hn", "Zorbit Systems", title="Account Executive")
    add_item(env.conn, "hn", "Zorbit Systems", title="Account Executive II")
    s = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    assert len(s.item_ids) == 2 and s.confidence <= 0.5


def test_a_name_only_post_after_a_dismissal_does_not_resurface_it(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY, first_seen=NOW - DAY)
    s = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    Feedback(env.conn, [], now=env.clock.now).dismiss(s.id, "not now")
    env.clock.advance(DAY)
    add_item(env.conn, "hn", "Acme Inc (acme.example)", at=env.clock.now(), first_seen=env.clock.now())
    sug.build_all()
    assert live_hiring(env.conn) == [] and sug.get(s.id)["status"] == "dismissed"


def test_a_status_set_without_feedback_counts_from_its_last_update(home):
    from tests.test_companies import add_item
    env, sug = company_env(home, {"agency-eu": TWO_PROFILES["agency-eu"]})
    add_item(env.conn, "acme-jobs", "Acme Inc", board="acme", at=NOW - DAY, first_seen=NOW - DAY)
    s = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    env.conn.execute("UPDATE suggestions SET status='done', updated_at=? WHERE id=?", (NOW, s.id))
    env.clock.advance(3600)
    sug.build_all()
    assert live_hiring(env.conn) == [] and sug.get(s.id)["status"] == "done"


def test_an_alias_added_later_expires_the_old_company_row(home):
    from ninshubur.companies import aliases_path
    from tests.test_companies import add_item
    env, sug = company_env(home, {"wide": {"titles": ["Account Executive"]}})
    add_item(env.conn, "hn", "Quokka Robotics", title="Account Executive")
    old = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    aliases_path(home).write_text("Quokka Robotics: quokka.example\n", encoding="utf-8")
    dry = sug.regroup(dry_run=True)
    assert dry["moved"] == 1 and sug.get(old.id)["status"] in ("new", "held")
    new = next(s for s in sug.build_all() if s.kind == "hiring_signal_outreach")
    assert new.entity_keys != old.entity_keys and sug.get(old.id)["status"] == "expired"
    assert one(env.conn, "SELECT COUNT(*) AS n FROM suggestions WHERE id=?", (old.id,))["n"] == 1  # never deleted
