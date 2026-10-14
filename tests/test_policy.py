from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from ninshubur.policy import Check, Policy, PolicyError, check_url, merge_tightening


@dataclass(frozen=True)
class Spec:
    hosts: frozenset
    needs_key_env: str | None = None
    questions: tuple = ("which conferences are coming up?",)


GOOD_TRUST = {
    "tier": 1, "owner": "tech-conferences", "owner_url": "https://github.com/tech-conferences",
    "terms_url": "https://github.com/tech-conferences/conference-data/blob/main/LICENSE.md",
    "licence": "MIT", "legal_basis": "public_business_info", "rate_limit": "1/day", "expected_content_type": "text/plain",
    "schema": "confs_tech.v1", "reviewed_by": "alex", "reviewed_at": "2026-10-12", "review_every_days": 120,
}
GOOD_TEST = {"status": 200, "urls": ["https://raw.githubusercontent.com/x/y.json"], "tls_ok": True,
             "off_host_redirect": False, "field_validity": 0.99, "size": 1000, "robots_allowed": True,
             "unauthenticated": True, "schema_known": True}
SPEC = Spec(frozenset({"raw.githubusercontent.com"}))


def test_shipped_policy_loads(policy: Policy):
    assert "linkedin.com" in policy.denied_hosts
    assert policy.tier(0)["reputation"] == 0.90
    assert policy.notes == []


def test_user_policy_can_only_tighten(home: Path):
    (home / "policy.yaml").write_text(yaml.safe_dump({
        "fetch": {"denied_hosts": ["evil.example"], "max_body_bytes_hard": 100_000_000, "max_hops": 1,
                  "robots": {"honour": False}},
        "person_data": {"allowed_fields": ["name", "title", "home_address"]},
        "retention": {"months": 3},
        "made_up": {"x": 1},
    }))
    p = Policy.load(home)
    assert "evil.example" in p.denied_hosts and "linkedin.com" in p.denied_hosts
    assert p.get("fetch", "max_body_bytes_hard") == 8_000_000  # loosening ignored
    assert p.get("fetch", "max_hops") == 1
    assert p.get("fetch", "robots", "honour") is True
    assert p.allowed_person_fields == {"name", "title"}  # intersection; home_address never allowed
    assert p.get("retention", "months") == 3
    assert any("max_body_bytes_hard" in n for n in p.notes)
    assert any("robots.honour" in n for n in p.notes)
    assert any("made_up.x" in n for n in p.notes)


def test_malformed_user_policy_fails_closed(home: Path):
    (home / "policy.yaml").write_text("fetch: [unclosed")
    with pytest.raises(PolicyError):
        Policy.load(home)


def test_merge_rejects_non_mapping():
    with pytest.raises(PolicyError):
        merge_tightening({"fetch": {}}, ["x"])  # type: ignore[arg-type]


@pytest.mark.parametrize("url,code", [
    ("https://example.com/a", None),
    ("http://example.com/a", "url_not_https"),
    ("http://127.0.0.1:11434/v1", "url_ip_literal"),
    ("https://user:pw@example.com/", "url_userinfo"),
    ("https://93.184.216.34/", "url_ip_literal"),
    ("https://[2001:db8::1]/", "url_ip_literal"),
    ("https://xn--80ak6aa92e.com/", "url_punycode"),
    ("https://*.example.com/", "url_wildcard"),
    ("ftp://example.com/", "url_scheme"),
    ("https://exa mple.com/", "url_control_chars"),
    ("https://example.com/" + "a" * 2100, "url_too_long"),
    ("", "url_empty"),
])
def test_check_url(url, code):
    assert check_url(url) == code


def test_check_url_allows_local_http_only_when_asked():
    assert check_url("http://127.0.0.1:11434/v1", allow_http_local=True) is None
    assert check_url("http://localhost:11434/v1", allow_http_local=True) is None
    assert check_url("http://10.0.0.5:11434/v1", allow_http_local=True) == "url_ip_literal"


def test_allow_fetch_gate(policy: Policy, now: float):
    hosts = {"hn.algolia.com"}
    assert policy.allow_fetch("https://hn.algolia.com/api/v1/search", spec_hosts=hosts, now=now) is None
    assert policy.allow_fetch("http://hn.algolia.com/x", spec_hosts=hosts, now=now).code == "url_not_https"
    assert policy.allow_fetch("https://www.linkedin.com/in/x", spec_hosts={"www.linkedin.com"}, now=now).code == "host_denied"
    assert policy.allow_fetch("https://sub.linkedin.com/x", spec_hosts={"sub.linkedin.com"}, now=now).code == "host_denied"
    assert policy.allow_fetch("https://evil.example/x", spec_hosts=hosts, now=now).code == "host_not_in_spec"
    assert policy.allow_fetch("https://hn.algolia.com/x", spec_hosts=hosts, method="POST", now=now).code == "method_not_allowed"
    assert policy.allow_fetch("https://hn.algolia.com/x", spec_hosts=hosts, source_health="quarantined", now=now).code == "source_health"
    assert policy.allow_fetch("https://hn.algolia.com/x", spec_hosts=hosts, review_due_at=now - 1, now=now).code == "review_overdue"
    assert policy.allow_fetch("https://hn.algolia.com/x", spec_hosts=hosts, source_health="suspect", now=now) is None


def test_admission_passes_good_source(policy: Policy, now: float):
    checks = policy.admission_check(GOOD_TRUST, SPEC, GOOD_TEST, now=now)
    assert [c.criterion for c in checks] == ["H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8"]
    assert Policy.admitted(checks), [c for c in checks if not c.ok]


@pytest.mark.parametrize("criterion,trust_patch,test_patch,spec", [
    ("H1", {"owner": None}, {}, SPEC),
    ("H1", {"owner_url": "http://example.com"}, {}, SPEC),
    ("H2", {"terms_url": None}, {}, SPEC),
    ("H2", {"legal_basis": "vibes"}, {}, SPEC),
    ("H2", {}, {"robots_allowed": False}, SPEC),
    ("H3", {}, {"urls": ["http://raw.githubusercontent.com/x"]}, SPEC),
    ("H3", {}, {"urls": ["https://93.184.216.34/x"]}, SPEC),
    ("H3", {}, {"off_host_redirect": True}, SPEC),
    ("H3", {}, {"tls_ok": False}, SPEC),
    ("H3", {}, {"urls": ["https://other.example/x"]}, SPEC),
    ("H4", {}, {"field_validity": 0.8}, SPEC),
    ("H4", {"schema": None}, {"schema_known": False}, SPEC),
    ("H5", {}, {"status": 403}, SPEC),
    ("H5", {}, {"unauthenticated": False}, SPEC),
    ("H6", {"rate_limit": None}, {}, SPEC),
    ("H6", {}, {"size": 9_000_000}, SPEC),
    ("H7", {}, {}, Spec(frozenset({"raw.githubusercontent.com"}), questions=())),
    ("H8", {"reviewed_by": None}, {}, SPEC),
    ("H8", {"review_every_days": 365}, {}, SPEC),
    ("H8", {"reviewed_at": "2025-01-01"}, {}, SPEC),
])
def test_admission_refuses_each_hard_criterion(policy: Policy, now: float, criterion, trust_patch, test_patch, spec):
    trust = {**GOOD_TRUST, **trust_patch}
    test = {**GOOD_TEST, **test_patch}
    checks = policy.admission_check(trust, spec, test, now=now)
    failed = {c.criterion for c in checks if not c.ok}
    assert criterion in failed, checks
    assert not Policy.admitted(checks)


def test_licensed_source_may_use_key(policy: Policy, now: float):
    spec = Spec(frozenset({"api.apollo.io"}), needs_key_env="APOLLO_KEY")
    trust = {**GOOD_TRUST, "tier": 0, "legal_basis": "licensed", "review_every_days": 180}
    test = {**GOOD_TEST, "urls": ["https://api.apollo.io/v1/x"], "unauthenticated": False}
    assert Policy.admitted(policy.admission_check(trust, spec, test, now=now))


def test_allow_store_whitelist(policy: Policy):
    kept, dropped = policy.allow_store(
        {"name": "Ada", "title": "CTO", "home_address": "1 Main St", "personal_email": "a@x.y", "office_city": "Lisbon",
         "street": "x", "shoe_size": 7, "tz": ""}, source_adapter="apollo")
    assert kept == {"name": "Ada", "title": "CTO", "office_city": "Lisbon"}
    assert dropped == ["home_address", "personal_email", "shoe_size", "street", "tz"]
    kept, dropped = policy.allow_store({"name": "Ada"}, source_adapter="hn")
    assert kept == {} and dropped == ["name"]


def test_allow_llm_person_data_local_only(policy: Policy):
    assert policy.allow_llm(person_data=True, local=False).code == "person_data_remote_llm"
    assert policy.allow_llm(person_data=True, local=True) is None
    assert policy.allow_llm(person_data=False, local=False) is None


def test_review_due_and_retention(policy: Policy, now: float):
    due = policy.review_due_at(GOOD_TRUST)
    assert due is not None and due > now
    assert policy.retention_until(now) > now + 364 * 86400


def test_never_invariants_have_no_code_path():
    """The policy lists things that must have no implementation. Grep the package for them."""
    import ninshubur
    root = Path(ninshubur.__file__).parent
    banned = ("import subprocess", "from subprocess", "import pickle", "from pickle", "eval(", "exec(",
              "selenium", "playwright", "2captcha", "anticaptcha", "capsolver", "capmonster", "undetected_chromedriver")
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8").lower()
        for b in banned:
            assert b not in text, f"{py.name} contains {b!r}"
    assert isinstance(Check("H1", True, ""), Check)


def test_derived_links_policy(policy: Policy):
    base = "https://event.example/"
    ok = policy.allow_derived_link("https://event.example/speakers/", base_url=base, adapter="event_sites")
    assert ok is None
    assert policy.allow_derived_link("https://event.example/sponsors", base_url=base, adapter="hn").code == "derived_links_not_allowed"
    assert policy.allow_derived_link("https://other.example/speakers", base_url=base, adapter="event_sites").code == "derived_off_host"
    assert policy.allow_derived_link("https://event.example/tickets", base_url=base, adapter="event_sites").code == "derived_path_term"
    assert policy.allow_derived_link("https://event.example/people", base_url=base, adapter="event_sites", anchor_text="Our speakers") is None
    assert policy.allow_derived_link("https://event.example/speakers#top", base_url=base, adapter="event_sites").code == "derived_fragment"
    assert policy.allow_derived_link("https://event.example/speakers?x=" + "a" * 300, base_url=base, adapter="event_sites").code == "derived_query_too_long"
    assert policy.allow_derived_link("http://event.example/speakers", base_url=base, adapter="event_sites").code == "url_not_https"
    assert policy.allow_derived_link("https://www.linkedin.com/in/x", base_url="https://www.linkedin.com/", adapter="event_sites").code == "host_denied"
    assert policy.allow_derived_link("javascript:alert(1)", base_url=base, adapter="event_sites").code == "url_scheme"


def test_derived_links_user_policy_tightens_only(home: Path):
    (home / "policy.yaml").write_text(yaml.safe_dump({"fetch": {
        "derived_links": {"allowed_adapters": ["event_sites", "hn"], "max_per_document": 2, "path_terms": ["speaker"], "same_host_only": False},
        "refuse_private_addresses": False}}))
    p = Policy.load(home)
    dl = p.get("fetch", "derived_links")
    assert dl["allowed_adapters"] == ["event_sites"] and dl["max_per_document"] == 2 and dl["path_terms"] == ["speaker"]
    assert dl["same_host_only"] is True and p.get("fetch", "refuse_private_addresses") is True
    assert p.allow_derived_link("https://e.example/sponsors", base_url="https://e.example/", adapter="event_sites").code == "derived_path_term"


def test_allow_fetch_extra_hosts(policy: Policy, now: float):
    assert policy.allow_fetch("https://event.example/x", spec_hosts=set(), now=now).code == "host_not_in_spec"
    assert policy.allow_fetch("https://event.example/x", spec_hosts=set(), extra_hosts={"event.example"}, now=now) is None
    assert policy.allow_fetch("https://www.linkedin.com/x", spec_hosts=set(), extra_hosts={"www.linkedin.com"}, now=now).code == "host_denied"
