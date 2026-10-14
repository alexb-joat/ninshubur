from __future__ import annotations

import pytest

from ninshubur.adapters.base import FetchPlan, Fetched, all_adapters, get_adapter
from ninshubur.config import SourceConfig
from ninshubur.schemas import SCHEMAS
from tests.conftest import NOW, load_sample


def fetched(plan: FetchPlan, body: bytes) -> Fetched:
    return Fetched(plan=plan, status=200, body=body, headers={"content-type": "application/json"}, changed=True, fetched_at=NOW)


def test_every_adapter_declares_known_schemas_and_hosts():
    for name, cls in all_adapters().items():
        spec = cls.spec
        assert spec.name == name
        assert spec.schema in SCHEMAS and all(s in SCHEMAS for s in spec.schemas)
        assert spec.hosts or spec.dynamic_hosts, f"{name} has no hosts and is not dynamic"
        assert all("/" not in h and h == h.lower() for h in spec.hosts)
        if spec.dynamic_hosts:
            assert spec.needs, f"{name} is dynamic but declares no scheduler-provided data"
        assert spec.questions, f"{name} declares no business question (H7)"
        assert spec.legal_basis in ("public_business_info", "licensed", "legitimate_interest", "contract")


def test_confs_tech_plan_and_parse():
    a = get_adapter("confs_tech")
    src = SourceConfig(name="confs", adapter="confs_tech", params={"year": 2026, "topics": ["general", "../evil", "python"]},
                       trust={"legal_basis": "public_business_info"})
    plans = a.plan(src, {}, NOW)
    assert [p.key for p in plans] == ["confs_tech:2026:general", "confs_tech:2026:python"]
    assert all(p.url.startswith("https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/2026/") for p in plans)
    res = a.parse(src, fetched(plans[0], load_sample("confs_tech/general.json")), NOW)
    assert [i.title for i in res.items] == ["PyCon Portugal", "SaaStock Dublin", "Remote Growth Summit"]
    py = res.items[0]
    assert py.kind == "event" and py.attrs["city"] == "Lisbon" and py.attrs["topic"] == "general"
    assert {d.kind for d in py.dates} == {"event_start", "cfp_deadline"}
    assert "twitter" not in py.attrs and "cocUrl" not in py.attrs and "bluesky" not in py.attrs
    assert res.projection.dropped_rows == 1
    assert ("pycon.pt", "https://pycon.pt/2026", "conference site") in res.candidates
    assert res.followups == []


def test_greenhouse_plan_and_parse():
    a = get_adapter("greenhouse")
    src = SourceConfig(name="gh", adapter="greenhouse",
                       params={"boards": ["acme", "Bad Board!"], "companies": {"acme": {"name": "Acme Inc", "domain": "acme.example"}}},
                       trust={"legal_basis": "public_business_info"})
    plans = a.plan(src, {}, NOW)
    assert [p.url for p in plans] == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false"]
    res = a.parse(src, fetched(plans[0], load_sample("greenhouse/jobs.json")), NOW)
    assert [i.title for i in res.items] == ["Sales Development Representative", "Head of Growth", "Senior Backend Engineer"]
    sdr = res.items[0]
    assert sdr.kind == "hiring" and sdr.entities[0].key == "company:acme.example" and sdr.entities[0].name == "Acme Inc"
    assert sdr.attrs["location"] == "Lisbon, Portugal" and "content" not in sdr.attrs
    assert sdr.dates[0].kind == "posted" and sdr.published_at == sdr.dates[0].at
    assert res.items[2].published_at is not None  # falls back to updated_at


def test_hn_stories_followups_from_ids_only():
    a = get_adapter("hn")
    src = SourceConfig(name="hn", adapter="hn", params={"show_hn_queries": ["lead gen", "<bad>"]}, trust={"legal_basis": "public_business_info"})
    plans = a.plan(src, {}, NOW)
    assert [p.key for p in plans] == ["hn:whoishiring:latest", "hn:whoishiring:freelancer", "hn:show:lead gen"]
    res = a.parse(src, fetched(plans[1], load_sample("hn/freelancer_stories.json")), NOW)
    assert len(res.items) == 3 and all(i.kind == "thread" and i.tier == 2 for i in res.items)
    assert [f.key for f in res.followups] == ["hn:comments:45400001"]  # only the fresh freelancer thread
    assert res.followups[0].url == "https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_45400001&hitsPerPage=200&page=0"
    assert res.followups[0].schema == "hn_comment.v1"
    assert ("example.org", "https://example.org/old", "linked from HN story") in res.candidates


def test_hn_comments_classified_and_taint_scanned():
    a = get_adapter("hn")
    src = SourceConfig(name="hn", adapter="hn", trust={"legal_basis": "public_business_info"})
    plan = FetchPlan(url="https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_45400001", key="hn:comments:45400001", schema="hn_comment.v1")
    res = a.parse(src, fetched(plan, load_sample("hn/comments.json")), NOW)
    by = {i.attrs["author"]: i for i in res.items}
    assert by["founder_a"].kind == "service_request" and by["founder_a"].attrs["classification"] == "SEEKING_FREELANCER"
    assert not by["founder_a"].tainted
    assert by["dev_b"].kind == "social" and by["dev_b"].attrs["classification"] == "SEEKING_WORK"
    assert by["spammer"].taint_severity == "high" and "prompt_injection" in by["spammer"].taint_codes
    assert by["ops_c"].taint_severity == "medium" and "shell_fragment" in by["ops_c"].taint_codes
    assert by["x"].attrs["classification"] == "OTHER"
    assert all(i.tier == 3 for i in res.items)
    assert all(i.url.startswith("https://news.ycombinator.com/item?id=") for i in res.items)


def test_statuspage_known_hosts_only():
    a = get_adapter("statuspage")
    assert a.plan(SourceConfig(name="s", adapter="statuspage", params={"host": "status.evil.example"}), {}, NOW) == []
    src = SourceConfig(name="s", adapter="statuspage", params={"host": "status.claude.com", "provider": "anthropic"},
                       trust={"legal_basis": "public_business_info"})
    plans = a.plan(src, {}, NOW)
    assert plans[0].url == "https://status.claude.com/api/v2/summary.json"
    res = a.parse(src, fetched(plans[0], load_sample("statuspage/summary.json")), NOW)
    kinds = {(i.kind, i.title): i for i in res.items}
    inc = kinds[("incident", "Elevated error rates on the API")]
    assert inc.attrs["impact"] == "major" and inc.attrs["resolved"] is False and inc.attrs["provider"] == "anthropic"
    assert inc.url == "https://stspg.io/abc1" and {d.kind for d in inc.dates} == {"incident_started"}
    assert kinds[("incident", "Resolved earlier blip")].attrs["resolved"] is True
    assert kinds[("status_component", "API")].attrs["status"] == "degraded_performance"
    assert not any("incident_updates" in i.attrs for i in res.items)  # body text never read


def test_not_json_is_a_structure_finding():
    a = get_adapter("confs_tech")
    src = SourceConfig(name="c", adapter="confs_tech")
    plan = a.plan(src, {}, NOW)[0]
    res = a.parse(src, fetched(plan, b"<html>served as json</html>"), NOW)
    assert res.items == [] and [f.code for f in res.findings] == ["not_json"]


@pytest.mark.parametrize("name", ["confs_tech", "greenhouse", "hn", "statuspage"])
def test_adapter_never_fetches_urls_from_content(name):
    """Every follow-up URL must be built on the adapter's own hosts."""
    a = get_adapter(name)
    src = SourceConfig(name=name, adapter=name, params={"host": "status.claude.com", "boards": ["acme"]})
    for plan in a.plan(src, {}, NOW):
        from ninshubur.policy import host_of
        assert host_of(plan.url) in a.spec.hosts


def test_hn_who_is_hiring_threads_and_comments():
    a = get_adapter("hn")
    src = SourceConfig(name="hn", adapter="hn", trust={"legal_basis": "public_business_info"})
    plan = a.plan(src, {}, NOW)[0]
    assert plan.key == "hn:whoishiring:latest" and "query=" not in plan.url
    res = a.parse(src, fetched(plan, load_sample("hn/whoishiring_stories.json")), NOW)
    # October + September "Who is hiring?" are fresh (< 45 d); "Who wants to be hired?" skipped; 2025 freelancer thread stale
    assert [f.key for f in res.followups] == ["hn:comments:49922569", "hn:comments:49522897"]
    cplan = FetchPlan(url="https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_49922569", key="hn:comments:49922569", schema="hn_comment.v1")
    res = a.parse(src, fetched(cplan, load_sample("hn/whoishiring_comments.json")), NOW)
    by = {i.attrs["author"]: i for i in res.items}
    acme = by["acme_cto"]
    assert acme.kind == "hiring" and acme.attrs["company"] == "Acme Inc" and acme.attrs["board"] == "acme-inc"
    assert set(acme.attrs["roles"]) >= {"sdr", "account executive", "sales"} and acme.entities[0].key == "company:acme-inc"
    assert acme.tier == 3 and not acme.tainted and acme.url.endswith("49922701")
    assert by["nimbus_hr"].kind == "hiring" and "growth" in by["nimbus_hr"].attrs["roles"]
    assert by["quietlabs"].kind == "social" and by["quietlabs"].attrs["classification"] == "OTHER"  # engineer role: no signal
    assert by["spammer"].taint_severity == "high" and "prompt_injection" in by["spammer"].taint_codes
    assert by["ops_d"].kind == "hiring" and by["ops_d"].taint_severity == "medium"
    assert by["x"].kind == "social"
    assert not any("@" in (i.attrs.get("company") or "") for i in res.items)  # no emails extracted


def test_company_of():
    from ninshubur.adapters.hn import company_of
    assert company_of("Acme Inc | SDR | Lisbon") == "Acme Inc"
    assert company_of("no pipes here") is None
    assert company_of("We are a small team looking for help with our sales process and more | SDR") is None
    assert company_of("Acme <b>Inc</b>​ | SDR") == "Acme Inc"
