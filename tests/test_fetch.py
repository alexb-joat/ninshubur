from __future__ import annotations

import asyncio
import gzip
import os
import stat
from pathlib import Path

import httpx
import pytest

from ninshubur.adapters.base import AdapterSpec, FetchPlan
from ninshubur.config import Config, SourceConfig, load_config
from ninshubur.db import connect, rows
from ninshubur.fetch import Fetcher, RobotsCache
from ninshubur.logs import read_jsonl
from ninshubur.policy import Policy
from tests.conftest import NOW, Seen, make_transport, public_resolver

HOST = "api.example"
SPEC = AdapterSpec(name="t", kind="events", hosts=frozenset({HOST}), schema="confs_tech.v1", schemas=("confs_tech.v1",),
                   expected_content_type="application/json", base_interval_s=60, max_interval_s=600, default_tier=1,
                   legal_basis="public_business_info", questions=("q?",))
LICENSED = AdapterSpec(name="lic", kind="people_company", hosts=frozenset({HOST}), schema="confs_tech.v1",
                       expected_content_type="application/json", base_interval_s=60, max_interval_s=600, default_tier=0,
                       legal_basis="licensed", needs_key_env="LIC_KEY", questions=("q?",))
SRC = SourceConfig(name="t-src", adapter="t", trust={"expected_content_type": "application/json"})


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make(home: Path, policy: Policy, routes: dict, seen: Seen | None = None, clock: Clock | None = None, resolve=None):
    clock = clock or Clock()
    conn = connect(home)
    cfg = load_config(home, policy=policy)
    seen = seen or Seen()
    f = Fetcher(conn, policy, cfg, home, transport=make_transport(routes, seen), sleep=clock.sleep, now=clock.now,
                resolve=resolve or public_resolver)
    return f, conn, seen, clock


def run(coro):
    return asyncio.run(coro)


def test_basic_fetch_records_state_and_logs(home, policy):
    routes = {(HOST, "/robots.txt"): {"body": "User-agent: *\nAllow: /\n", "headers": {"Content-Type": "text/plain"}},
              (HOST, "/x.json"): {"body": [{"a": 1}], "etag": '"v1"'}}
    f, conn, seen, clock = make(home, policy, routes)
    plan = FetchPlan(url=f"https://{HOST}/x.json", key="t:x")
    r = run(f.fetch(plan, SRC, SPEC))
    assert r.ok and r.changed and r.status == 200 and r.json() == [{"a": 1}]
    r2 = run(f.fetch(plan, SRC, SPEC))
    assert r2.status == 304 and r2.unchanged and not r2.changed
    st = rows(conn, "SELECT * FROM fetch_state")
    assert len(st) == 1 and st[0]["etag"] == '"v1"' and st[0]["size"] > 0
    assert [c[1] for c in seen.calls] == ["/robots.txt", "/x.json", "/x.json"]
    log = read_jsonl(home / "logs" / "fetch.jsonl")
    assert [l["status"] for l in log] == [200, 304] and log[1]["conditional"] is True
    assert "ts" in log[0] and set(log[0]) >= {"source", "url", "status", "bytes", "changed", "duration_ms", "conditional", "boosted", "error"}
    if os.name == "posix":
        assert stat.S_IMODE((home / "logs" / "fetch.jsonl").stat().st_mode) == 0o600
        assert stat.S_IMODE((home / "ninshubur.db").stat().st_mode) == 0o600
    fetches = rows(conn, "SELECT * FROM fetches ORDER BY id")
    assert [x["changed"] for x in fetches] == [1, 0]
    assert rows(conn, "SELECT SUM(amount) AS s FROM budget_ledger WHERE kind='bytes'")[0]["s"] > 0


def test_unchanged_body_without_etag_is_hash_detected(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x.json"): {"body": {"k": "same"}}}
    f, conn, seen, _ = make(home, policy, routes)
    plan = FetchPlan(url=f"https://{HOST}/x.json", key="t:x")
    assert run(f.fetch(plan, SRC, SPEC)).changed is True
    assert run(f.fetch(plan, SRC, SPEC)).changed is False


@pytest.mark.parametrize("url,code", [
    ("https://other.example/x", "host_not_in_spec"),
    ("http://api.example/x", "url_not_https"),
    ("https://www.linkedin.com/x", "host_denied"),
    ("https://user:pw@api.example/x", "url_userinfo"),
])
def test_policy_refusals_never_touch_the_network(home, policy, url, code):
    f, conn, seen, _ = make(home, policy, {(HOST, "*"): {"body": {}}})
    spec = SPEC if "linkedin" not in url else AdapterSpec(**{**SPEC.__dict__, "hosts": frozenset({"www.linkedin.com"})})
    r = run(f.fetch(FetchPlan(url=url, key="k"), SRC, spec))
    assert r.error == f"refused:{code}" and r.status is None
    assert seen.calls == []
    pol = read_jsonl(home / "logs" / "policy.jsonl")
    assert pol[-1]["event"] == "refused_fetch" and pol[-1]["code"] == code


def test_health_and_review_gate(home, policy):
    f, conn, seen, _ = make(home, policy, {(HOST, "*"): {"body": {}}})
    plan = FetchPlan(url=f"https://{HOST}/x", key="k")
    assert run(f.fetch(plan, SRC, SPEC, health="quarantined")).error == "refused:source_health"
    assert run(f.fetch(plan, SRC, SPEC, review_due_at=NOW - 10)).error == "refused:review_overdue"
    assert seen.calls == []


def test_credential_header_only_for_licensed(home, policy):
    f, conn, seen, _ = make(home, policy, {(HOST, "*"): {"body": {}}})
    plan = FetchPlan(url=f"https://{HOST}/x", key="k", headers=(("Authorization", "Bearer x"),))
    assert run(f.fetch(plan, SRC, SPEC)).error == "refused:credential_header"
    assert seen.calls == []
    r = run(f.fetch(plan, SRC, LICENSED))
    assert r.status == 200 and seen.calls[-1][3] is True


def test_off_host_redirect_is_integrity(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/go"): {"status": 302, "headers": {"Location": "https://evil.example/landing"}, "body": ""},
              ("evil.example", "*"): {"body": "should never be reached"}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/go", key="k"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["off_host_redirect"] and r.findings[0].severity == "integrity"
    assert "evil.example" not in seen.hosts()


def test_same_host_https_redirect_followed_within_hops(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/a"): {"status": 301, "headers": {"Location": "/b"}, "body": ""},
              (HOST, "/b"): {"status": 301, "headers": {"Location": f"https://{HOST}/c"}, "body": ""},
              (HOST, "/c"): {"body": {"done": True}}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/a", key="k"), SRC, SPEC))
    assert r.ok and r.json() == {"done": True} and r.final_url.endswith("/c")
    routes[(HOST, "/c")] = {"status": 301, "headers": {"Location": "/d"}, "body": ""}
    routes[(HOST, "/d")] = {"body": {}}
    f2, *_ = make(home, policy, routes)
    r = run(f2.fetch(FetchPlan(url=f"https://{HOST}/a", key="k2"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["off_host_redirect"]  # too many hops
    routes[(HOST, "/a")] = {"status": 301, "headers": {"Location": f"http://{HOST}/b"}, "body": ""}
    f3, *_ = make(home, policy, routes)
    r = run(f3.fetch(FetchPlan(url=f"https://{HOST}/a", key="k3"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["off_host_redirect"]  # downgrade to http


def test_size_cap_streams_and_aborts(home, policy):
    big = b'["' + b"x" * 3_000_000 + b'"]'
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/big"): lambda req: httpx.Response(200, content=big, headers={"Content-Type": "application/json"}),
              (HOST, "/declared"): {"body": b"{}", "headers": {"Content-Length": "9000000"}}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/big", key="k", max_bytes=2_000_000), SRC, SPEC))
    assert [x.code for x in r.findings] == ["body_too_large"] and len(r.body) == 0
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/declared", key="k2"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["body_too_large"]
    assert rows(conn, "SELECT * FROM fetch_state") == []  # nothing marked good


def test_decompression_bomb_guard(home, policy):
    payload = gzip.compress(b"0" * 20_000_000)  # ~20 KB compressed -> 20 MB
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/bomb"): lambda req: httpx.Response(200, content=payload,
                                                          headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/bomb", key="k", max_bytes=8_000_000), SRC, SPEC))
    assert {x.code for x in r.findings} & {"decompression_bomb", "body_too_large"}
    assert r.findings[0].severity == "integrity"


def test_content_type_mismatch_and_binary(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/html"): {"body": "<html>hi</html>", "headers": {"Content-Type": "text/html"}},
              (HOST, "/bin"): {"body": b"MZ\x90\x00binary", "headers": {"Content-Type": "application/json"}}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/html", key="k"), SRC, SPEC))
    assert "content_type_mismatch" in [x.code for x in r.findings]
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/bin", key="k2"), SRC, SPEC))
    assert "binary_magic" in [x.code for x in r.findings]


def test_robots_disallow_and_crawl_delay(home, policy):
    routes = {(HOST, "/robots.txt"): {"body": "User-agent: *\nDisallow: /private\nCrawl-delay: 5\n",
                                      "headers": {"Content-Type": "text/plain"}},
              (HOST, "/private/x"): {"body": {}}, (HOST, "/public"): {"body": {}}}
    f, conn, seen, clock = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/private/x", key="k"), SRC, SPEC))
    assert r.error == "refused:robots_disallow" and [c[1] for c in seen.calls] == ["/robots.txt"]
    run(f.fetch(FetchPlan(url=f"https://{HOST}/public", key="k2"), SRC, SPEC))
    run(f.fetch(FetchPlan(url=f"https://{HOST}/public", key="k2"), SRC, SPEC))
    assert [c[1] for c in seen.calls].count("/robots.txt") == 1  # cached
    assert any(s >= 5.0 for s in clock.sleeps)  # crawl-delay honoured between requests
    assert rows(conn, "SELECT crawl_delay FROM robots")[0]["crawl_delay"] == 5.0


def test_robots_403_means_no_robots_allowed():
    assert RobotsCache.evaluate("", 403, "https://h/x", "Ninshubur/0.1") == (False, None)
    assert RobotsCache.evaluate("", 404, "https://h/x", "Ninshubur/0.1") == (True, None)
    allowed, delay = RobotsCache.evaluate("User-agent: Ninshubur\nDisallow: /\nUser-agent: *\nAllow: /", 200,
                                          "https://h/x", "Ninshubur/0.1")
    assert allowed is False


def test_per_host_pacing(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): {"body": {}}}
    f, conn, seen, clock = make(home, policy, routes)
    for i in range(3):
        run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key=f"k{i}"), SRC, SPEC))
    assert len(clock.sleeps) >= 3 and all(s >= 1.0 for s in clock.sleeps)


def test_retry_after_and_circuit_breaker(home, policy):
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "120", "Content-Type": "application/json"}, content=b"{}")

    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): flaky}
    f, conn, seen, clock = make(home, policy, routes)
    plan = FetchPlan(url=f"https://{HOST}/x", key="k")
    run(f.fetch(plan, SRC, SPEC))
    t_before = clock.t
    run(f.fetch(plan, SRC, SPEC))
    assert clock.t - t_before >= 120  # Retry-After honoured
    for _ in range(3):
        run(f.fetch(plan, SRC, SPEC))
    r = run(f.fetch(plan, SRC, SPEC))
    assert r.error == "refused:circuit_open"
    assert calls["n"] == 5


def test_byte_budget_gate(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): {"body": {"a": "b" * 1000}}}
    f, conn, seen, _ = make(home, policy, routes)
    f.daily_bytes = 500
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k1"), SRC, SPEC)).ok
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k2"), SRC, SPEC))
    assert r.error == "refused:byte_budget"


def test_unknown_host_in_transport_is_599(home, policy):
    """The mock answers 599 for anything unrouted, so a stray fetch cannot pass silently."""
    f, conn, seen, _ = make(home, policy, {})
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.status == 599 and not r.ok


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "172.16.0.1", "169.254.169.254", "100.64.0.1", "::1", "fd00::1", "::ffff:10.0.0.5", "0.0.0.0"])
def test_private_addresses_are_never_contacted(home, policy, addr):
    f, conn, seen, _ = make(home, policy, {(HOST, "*"): {"body": {}}}, resolve=lambda h: [addr])
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.error == "refused:private_address" and seen.calls == []
    assert read_jsonl(home / "logs" / "policy.jsonl")[-1]["code"] == "private_address"


def test_public_address_and_resolver_failure(home, policy):
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): {"body": {}}},
                            resolve=lambda h: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).status == 200
    f2, conn2, seen2, _ = make(home, policy, {(HOST, "/x"): {"body": {}}}, resolve=lambda h: (_ for _ in ()).throw(OSError("dns down")))
    r = run(f2.fetch(FetchPlan(url=f"https://{HOST}/x", key="k2"), SRC, SPEC))
    assert r.error != "refused:private_address"  # unresolvable is left to the transport, not treated as private


def test_is_public_address():
    from ninshubur.fetch import is_public_address
    assert is_public_address("93.184.216.34") and is_public_address("2606:2800:220:1:248:1893:25c8:1946")
    assert not is_public_address("10.1.2.3") and not is_public_address("fe80::1") and not is_public_address("not-an-ip")
