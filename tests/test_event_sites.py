from __future__ import annotations

import json

from ninshubur.adapters.base import FetchPlan, Fetched, get_adapter
from ninshubur.config import SourceConfig
from ninshubur.db import one, rows, uj
from ninshubur.logs import read_jsonl
from tests.conftest import DEFAULT_SOURCES, MALICIOUS, NOW, SAMPLE_ROUTES, SAMPLES, Env, grep_home, page, write_config, write_profiles

DAY = 86400.0
SRC = SourceConfig(name="event-sites", adapter="event_sites", params={"events_per_day": 2, "pages_per_event": 3},
                   trust={"legal_basis": "public_business_info", "tier": 2})


def events(n=5, **over):
    out = []
    for i in range(n):
        ev = {"uid": f"ev{i}", "url": f"https://ev{i}.example/", "title": f"Event {i}", "start": NOW + (i + 2) * 10 * DAY, "end": None}
        ev.update(over)
        out.append(ev)
    return out


def fetched(plan: FetchPlan, body: bytes, url: str | None = None) -> Fetched:
    return Fetched(plan=plan, status=200, body=body, headers={"content-type": "text/html"}, changed=True, fetched_at=NOW, final_url=url or plan.url)


def test_plan_ranks_caps_and_skips():
    a = get_adapter("event_sites")
    evs = events(5)
    evs[1]["url"] = "http://insecure.example/"  # refused by check_url
    evs[2]["start"] = NOW - 400 * DAY  # too old
    evs[3]["start"] = NOW - 30 * DAY  # held recently: eligible
    state = {"events": evs, "per_event": {"ev4": {"flagged": ["off_host_redirect"]}}}
    plans = a.plan(SRC, state, NOW)
    assert [p.key for p in plans] == ["event_sites:ev0:home", "event_sites:ev3:home"]  # cap 2; upcoming before held
    assert all(p.expected_content_type == "text/html" and p.max_bytes == 2_000_000 for p in plans)
    # daily counter: two homepages done -> nothing more today; tomorrow again
    a.update_state(state, plans[0], ok=True, codes=(), now=NOW)
    a.update_state(state, plans[1], ok=True, codes=(), now=NOW)
    assert a.plan(SRC, state, NOW) == []
    later = a.plan(SRC, state, NOW + DAY)
    assert [p.key for p in later] == []  # ev0 crawled < 7 d ago, ev3 held (once), others excluded
    assert a.plan(SRC, state, NOW + 8 * DAY)[0].key == "event_sites:ev0:home"  # upcoming: weekly recrawl


def test_update_state_flags_and_fails():
    a = get_adapter("event_sites")
    state: dict = {}
    plan = FetchPlan(url="https://ev0.example/", key="event_sites:ev0:home")
    a.update_state(state, plan, ok=False, codes=("http_404",), now=NOW)
    a.update_state(state, plan, ok=False, codes=("content_type_mismatch",), now=NOW)
    st = state["per_event"]["ev0"]
    assert st["fails"] == 2 and st["flagged"] == ["content_type_mismatch"]
    assert a.plan(SRC, {"events": events(1), "per_event": state["per_event"]}, NOW) == []


def test_parse_home_follows_same_host_term_links_only():
    a = get_adapter("event_sites")
    plan = FetchPlan(url="https://pycon.pt/2026", key="event_sites:ev0:home", purpose="event homepage: PyCon")
    res = a.parse(SRC, fetched(plan, (SAMPLES / "event_sites" / "home.html").read_bytes()), NOW)
    urls = [f.url for f in res.followups]
    assert urls == ["https://pycon.pt/sponsors", "https://pycon.pt/speakers/", "https://pycon.pt/agenda?day=1"]  # paying companies first
    assert all(f.derived_from == "https://pycon.pt/2026" and f.key.startswith("event_sites:ev0:") for f in res.followups)
    assert ("espc.example", "https://espc.example/speakers", "event subpage on another host") in res.candidates
    assert res.items[0].kind == "prospect_page" and res.items[0].attrs["event_uid"] == "ev0" and res.items[0].attrs["page"] == 0


def test_parse_speakers_page_into_entities():
    a = get_adapter("event_sites")
    plan = FetchPlan(url="https://pycon.pt/speakers/", key="event_sites:ev0:1", derived_from="https://pycon.pt/2026", purpose="Speakers of PyCon")
    res = a.parse(SRC, fetched(plan, (SAMPLES / "event_sites" / "cards_linkedin.html").read_bytes()), NOW)
    item = res.items[0]
    assert item.kind == "prospect_page" and item.person_data and item.attrs["persons"] == 5 and item.attrs["layer"] == "cards"
    people = {e.name: e for e in item.entities if e.type == "person"}
    assert people["Jane Doe"].handle == "https://www.linkedin.com/in/janedoe" and people["Jane Doe"].key == "person:janedoe"
    assert people["Jane Doe"].company == "Rocket Labs" and people["Jane Doe"].role == "speaker"
    orgs = {e.name: e for e in item.entities if e.type == "company"}
    assert orgs["BigCloud"].domain == "bigcloud.example" and orgs["BigCloud"].role == "partner"
    assert res.followups == []  # follow-ups only from the homepage
    assert item.attrs["contact_data"] >= 1 and "info@event.example" not in json.dumps(item.attrs)


def test_parse_hostile_rows_are_dropped_and_page_held():
    a = get_adapter("event_sites")
    plan = FetchPlan(url="https://ev0.example/speakers", key="event_sites:ev0:1")
    res = a.parse(SRC, fetched(plan, (MALICIOUS / "event_sites" / "bio_injection.html").read_bytes()), NOW)
    item = res.items[0]
    names = {e.name for e in item.entities}
    assert "Eve Mallory" not in names and {"Honest Person", "Normal Two", "Shelly Script"} <= names
    assert res.dropped_rows and res.dropped_rows[0][2] == "Eve Mallory" and "prompt_injection" in res.dropped_rows[0][1]
    assert item.taint_severity == "medium" and "dropped_rows" in item.taint_codes and "shell_fragment" in item.taint_codes


# --- through the scheduler ---------------------------------------------------------------------------
def test_end_to_end_crawl_and_prospecting_suggestion(home):
    write_config(home)
    write_profiles(home)
    env = Env(home, dict(SAMPLE_ROUTES))
    reports = {r.source: r for r in env.run_once()}
    ev = reports["event-sites"]
    # PyCon PT homepage + 3 subpages; SaaStock homepage; the online summit's script-heavy homepage
    assert ev.fetched == 6 and ev.refused == 0 and ev.errors == 0
    assert env.seen.hosts() <= {"raw.githubusercontent.com", "boards-api.greenhouse.io", "hn.algolia.com", "status.claude.com",
                                "pycon.pt", "www.saastock.com", "growthsummit.example"}
    assert "espc.example" not in env.seen.hosts()
    pages = rows(env.conn, "SELECT * FROM items WHERE kind='prospect_page'")
    assert len(pages) == 6
    people = rows(env.conn, "SELECT * FROM entities WHERE type='person' ORDER BY name")
    assert {p["name"] for p in people} >= {"Jane Doe", "Ada Lovelace", "Marie Curie", "Pat Smith", "Real Speaker"}
    jane = next(p for p in people if p["name"] == "Jane Doe")
    assert jane["handle"] == "https://www.linkedin.com/in/janedoe" and jane["company"] == "Rocket Labs" and jane["retention_until"] > NOW
    assert jane["legal_basis"] == "public_business_info"
    orgs = {o["name"]: o for o in rows(env.conn, "SELECT * FROM entities WHERE type='company'")}
    assert orgs["Acme Inc"]["domain"] == "acme.example" and orgs["Orbit Systems"]["role"] == "sponsor"
    links = rows(env.conn, "SELECT role, COUNT(*) AS n FROM item_entities GROUP BY role")
    assert {l["role"] for l in links} >= {"speaker", "sponsor", "partner", "organizer"}
    # state persisted per event
    st = uj(env.source("event-sites")["state"])
    assert st["daily"]["n"] == 3 and all(v.get("last") for v in st["per_event"].values())
    assert env.source("event-sites")["health"] == "ok"
    # nothing hostile on disk
    assert grep_home(home, "steal()") == [] and grep_home(home, "document.cookie") == [] and grep_home(home, "hello@expo.example") == []
    # the builder
    from ninshubur.analyze.suggest import Suggester
    from ninshubur.profiles import load_profiles
    sug = Suggester(env.conn, env.policy, env.cfg, load_profiles(home), now=env.clock.now)
    built = [s for s in sug.build_all() if s.kind == "conference_prospecting"]
    assert built, "no prospecting suggestions"
    py = next(s for s in built if "PyCon" in s.title)
    assert py.title.startswith("Prospect PyCon Portugal (Lisbon, 2026-11-10)") and "paying companies" in py.title
    assert py.status == "new" and 0 < py.est_cost <= 120 and py.cost_parts["travel_usd"] == 0
    assert any("unverified" in r for r in py.reasons) and py.evidence[0]["values"]["name"] == "PyCon Portugal"
    assert len(py.evidence) == 5 and all(e["tier"] == 2 for e in py.evidence[1:])
    from ninshubur.export import badges
    assert badges({"evidence": py.evidence, "status": "new"})[:2] == ["T2", "unverified"]  # weakest evidence, not the T1 dataset row
    assert "Keynote TBA" not in " ".join(py.steps)  # no title -> not a matched speaker
    assert not any("trip" in s.kind for s in built)


def test_malicious_event_site_is_flagged_alone(home):
    write_config(home)
    routes = dict(SAMPLE_ROUTES)
    routes[("pycon.pt", "/2026")] = page("meta_refresh_offhost.html")  # links to evil.example + a 300-char query
    routes[("www.saastock.com", "/")] = page("five_thousand_cards.html")
    env = Env(home, routes)
    env.run_once()
    assert "evil.example" not in env.seen.hosts()
    st = uj(env.source("event-sites")["state"])["per_event"]
    saas_uid = next(uid for uid, v in st.items() if v.get("flagged"))
    assert "too_many_items" in st[saas_uid]["flagged"]
    # candidates recorded for the off-host links, policy refusals logged for the long query
    assert one(env.conn, "SELECT COUNT(*) AS n FROM candidates WHERE host='evil.example'")["n"] >= 1
    pol = read_jsonl(home / "logs" / "policy.jsonl")
    assert any(p.get("code") == "derived_query_too_long" for p in pol)
    assert env.source("event-sites")["health"] in ("ok", "suspect")  # the batch source survives one bad site
    assert one(env.conn, "SELECT COUNT(*) AS n FROM entities WHERE name LIKE 'Fake Person%'")["n"] <= 300


def test_injection_row_quarantined_through_pipeline(home):
    write_config(home)
    routes = dict(SAMPLE_ROUTES)
    routes[("pycon.pt", "/speakers/")] = page("bio_injection.html")
    env = Env(home, routes)
    env.run_once()
    q = rows(env.conn, "SELECT * FROM quarantine WHERE action='dropped_row'")
    assert len(q) == 1 and "prompt_injection" in q[0]["detector_codes"] and q[0]["excerpt_hash"]
    assert grep_home(home, "Ignore all previous instructions") == [] and grep_home(home, "eve@evil.example") == []
    assert one(env.conn, "SELECT COUNT(*) AS n FROM entities WHERE name='Eve Mallory'")["n"] == 0
    assert one(env.conn, "SELECT COUNT(*) AS n FROM entities WHERE name='Honest Person'")["n"] == 1
    held_page = one(env.conn, "SELECT held, tainted FROM items WHERE kind='prospect_page' AND url='https://pycon.pt/speakers/'")
    assert held_page["tainted"] == 1


def test_followups_prefer_list_pages_and_sponsors_first():
    a = get_adapter("event_sites")
    html = b"""<html><body>
    <a href="/speaker/aibda/alice">Alice</a><a href="/speaker/aibda/bob">Bob</a><a href="/speaker/aibda/carol">Carol</a>
    <a href="/aibda/speakers">Speakers</a><a href="/aibda/agenda">Agenda</a><a href="/aibda/sponsors">Sponsors</a>
    <a href="https://www.ev0.example/aibda/exhibitors">Exhibitors (www twin)</a><a href="/aibda/tickets">Tickets</a>
    <a href="https://other.example/sponsors">Sponsors elsewhere</a></body></html>"""
    plan = FetchPlan(url="https://ev0.example/aibda", key="event_sites:ev0:home", purpose="event homepage: AIBDA")
    res = a.parse(SourceConfig(name="e", adapter="event_sites", params={"pages_per_event": 3}, trust={"legal_basis": "public_business_info"}),
                  fetched(plan, html), NOW)
    urls = [f.url for f in res.followups]
    assert urls == ["https://ev0.example/aibda/sponsors", "https://www.ev0.example/aibda/exhibitors", "https://ev0.example/aibda/speakers"]
    assert ("other.example", "https://other.example/sponsors", "event subpage on another host") in res.candidates
