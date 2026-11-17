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
    assert RobotsCache.evaluate("", 503, "https://h/x", "Ninshubur/0.1") == (False, None)  # unreachable: RFC 9309 2.3.1.4
    assert RobotsCache.evaluate("", None, "https://h/x", "Ninshubur/0.1") == (False, None)  # network error
    allowed, delay = RobotsCache.evaluate("User-agent: Ninshubur\nDisallow: /\nUser-agent: *\nAllow: /", 200,
                                          "https://h/x", "Ninshubur/0.1")
    assert allowed is False


def _src(**trust_over) -> SourceConfig:
    return SourceConfig(name="t-src", adapter="t", trust={"expected_content_type": "application/json", **trust_over})


def test_robots_401_passes_only_with_the_exact_reviewed_status(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 401, "body": "Unauthorized"}, (HOST, "/x"): {"body": {"jobs": []}}}
    f, conn, seen, clock = make(home, policy, routes)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), _src(), SPEC)).error == "refused:robots_disallow"
    wrong = _src(robots_reviewed_status=403)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), wrong, SPEC)).error == "refused:robots_disallow"
    reviewed = _src(robots_reviewed_status=401)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), reviewed, SPEC)).ok
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k2"), reviewed, SPEC)).ok
    events = [e for e in read_jsonl(home / "logs" / "policy.jsonl") if e["event"] == "robots_reviewed_exception"]
    assert len(events) == 1 and events[0]["host"] == HOST and events[0]["status"] == 401  # logged once, visible


def test_robots_exception_never_covers_a_host_outside_the_adapters_fixed_hosts(home, policy):
    """An adapter whose hosts arrive at run time (event sites) gets no exception: nobody reviewed those hosts."""
    other = "events.example"
    routes = {(other, "/robots.txt"): {"status": 401, "body": ""}, (other, "/x"): {"body": {}}}
    f, conn, seen, clock = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{other}/x", key="k"), _src(robots_reviewed_status=401), SPEC, extra_hosts=[other]))
    assert r.error == "refused:robots_disallow" and "/x" not in [c[1] for c in seen.calls]
    reason = [e for e in read_jsonl(home / "logs" / "policy.jsonl") if e["event"] == "refused_fetch"][-1]["reason"]
    assert "robots_reviewed_status" not in reason  # the fix is not offered where it cannot apply


@pytest.mark.parametrize("robots", [{"status": 503, "body": ""},
                                    {"body": "User-agent: *\nDisallow: /\n", "headers": {"Content-Type": "text/plain"}}])
def test_robots_exception_never_covers_an_unreachable_or_a_real_robots_txt(home, policy, robots):
    routes = {(HOST, "/robots.txt"): robots, (HOST, "/x"): {"body": {}}}
    f, conn, seen, clock = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), _src(robots_reviewed_status=401), SPEC))
    assert r.error in ("refused:robots_unreachable", "refused:robots_disallow") and "/x" not in [c[1] for c in seen.calls]


@pytest.mark.parametrize("value, ok", [(401, True), (403, True), ("401", True), (404, False), (503, False), ("yes", False)])
def test_config_accepts_only_401_or_403_as_a_reviewed_robots_status(home, policy, value, ok):
    from ninshubur.config import ConfigError
    from tests.conftest import trust, write_config
    write_config(home, {"x": {"adapter": "greenhouse", "params": {"boards": ["acme"]},
                              "trust": trust(0, "greenhouse_jobs.v1", robots_reviewed_status=value)}})
    if ok:
        assert load_config(home, policy=policy).sources["x"].robots_reviewed_status == int(value)
    else:
        with pytest.raises(ConfigError, match="robots_reviewed_status"):
            load_config(home, policy=policy)


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
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}})
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.status == 599 and not r.ok


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "172.16.0.1", "169.254.169.254", "100.64.0.1", "::1", "fd00::1",
                                  "::ffff:10.0.0.5", "0.0.0.0", "10.47.83.1", "::ffff:127.0.0.1", "64:ff9b::7f00:1",
                                  "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "2002:7f00:1::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2",
                                  "fec0::1", "::7f00:1", "192.0.0.8", "198.18.0.1", "fe80::1%eth0"])
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
    assert r.error == "AddressRefused" and seen2.calls == []  # unresolvable fails closed: nothing is sent anywhere


def test_connection_goes_to_the_checked_address_with_the_name_kept_for_tls(home, policy):
    got = {}

    def page(request):
        got.update(addr=request.url.host, sni=request.extensions.get("sni_hostname"), host=request.headers.get("host"))
        return httpx.Response(200, json={})
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): page},
                            resolve=lambda h: ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"])
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).status == 200
    assert got == {"addr": "93.184.216.34", "sni": HOST, "host": HOST}  # v4 first; certificate checked against HOST
    f6, _, seen6, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): page},
                           resolve=lambda h: ["2606:2800:220:1:248:1893:25c8:1946"])
    assert run(f6.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).status == 200
    assert got["addr"] == "2606:2800:220:1:248:1893:25c8:1946" and got["sni"] == HOST


def test_dns_rebinding_cannot_swap_the_address_after_the_check(home, policy):
    """The name answers public, then private. The check and the connection share one answer, so every request goes
    to the public address, and once the answer expires the private one is refused before anything is sent."""
    answers = iter([["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]])
    clock = Clock()
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "*"): {"body": {}}},
                            clock=clock, resolve=lambda h: next(answers))
    for i in range(3):
        assert run(f.fetch(FetchPlan(url=f"https://{HOST}/p{i}", key=f"k{i}"), SRC, SPEC)).ok
    assert set(seen.addresses) == {"93.184.216.34"}
    clock.t += 601
    before = len(seen.calls)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/late", key="late"), SRC, SPEC)).error == "refused:private_address"
    assert len(seen.calls) == before


def test_pinned_transport_refuses_what_the_check_would_refuse(home, policy):
    from ninshubur.fetch import AddressRefused, PinnedTransport
    sent = []
    inner = httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    f, *_ = make(home, policy, {}, resolve=lambda h: ["10.0.0.5"])
    t = PinnedTransport(inner, f._pinned)
    for url in (f"https://{HOST}/", f"http://{HOST}/", f"https://{HOST}:22/"):
        with pytest.raises(AddressRefused):
            run(t.handle_async_request(httpx.Request("GET", url)))
    assert sent == []
    assert read_jsonl(home / "logs" / "policy.jsonl")[-1]["event"] == "address_refused"


# Services on this machine (and the planned `ninshubur serve`): every way of naming them stays refused, so a later
# "allow localhost" change fails here, loudly, before it can let a crawled link reach one of them.
LOCAL_SERVICES = {"ollama": 11434, "trioditis": 8787, "hlidskjalv": 8788, "default-https": 443}


@pytest.mark.parametrize("name,port", sorted(LOCAL_SERVICES.items()))
def test_local_services_are_never_contacted(home, policy, name, port):
    hosts = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "loopback.example"})
    spec = AdapterSpec(name="t", kind="events", hosts=hosts, schema="confs_tech.v1", schemas=("confs_tech.v1",),
                       expected_content_type="application/json", base_interval_s=60, max_interval_s=600, default_tier=1,
                       legal_basis="public_business_info", questions=("q?",))
    f, conn, seen, _ = make(home, policy, {}, resolve=lambda h: ["127.0.0.1"])
    for url in (f"https://localhost:{port}/", f"http://localhost:{port}/", f"https://127.0.0.1:{port}/",
                f"http://127.0.0.1:{port}/", f"https://[::1]:{port}/", f"https://loopback.example:{port}/"):
        r = run(f.fetch(FetchPlan(url=url, key="k"), SRC, spec))
        assert (r.error or "").startswith("refused:"), (name, url, r.error)
    assert seen.calls == []


def test_is_public_address():
    from ninshubur.fetch import is_public_address
    assert is_public_address("93.184.216.34") and is_public_address("2606:2800:220:1:248:1893:25c8:1946")
    assert not is_public_address("10.1.2.3") and not is_public_address("fe80::1") and not is_public_address("not-an-ip")


def test_www_twin_redirect_is_followed_but_other_subdomains_are_not(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""}, ("www." + HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/a"): {"status": 308, "headers": {"Location": f"https://www.{HOST}/a"}, "body": ""},
              ("www." + HOST, "/a"): {"body": {"twin": True}},
              (HOST, "/b"): {"status": 301, "headers": {"Location": f"https://cdn.{HOST}/b"}, "body": ""},
              ("cdn." + HOST, "*"): {"body": {"never": True}}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/a", key="k"), SRC, SPEC))
    assert r.ok and r.json() == {"twin": True} and r.final_url == f"https://www.{HOST}/a"
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/b", key="k2"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["off_host_redirect"] and "cdn." + HOST not in seen.hosts()


def test_network_errors_log_their_underlying_reason(home, policy):
    def unreachable(req):
        try:
            raise OSError(101, "Network is unreachable")
        except OSError as e:
            raise httpx.ConnectError("All connection attempts failed") from e

    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): unreachable})
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.error == "ConnectError" and r.error_detail == "[Errno 101] Network is unreachable"
    assert read_jsonl(home / "logs" / "fetch.jsonl")[-1]["detail"] == "[Errno 101] Network is unreachable"


def test_reachable_needs_an_address_and_a_route(monkeypatch):
    from ninshubur import fetch
    monkeypatch.setattr(fetch, "resolve_host", lambda h: [])
    assert fetch.reachable("example.org") is False  # no DNS answer: offline
    monkeypatch.setattr(fetch, "resolve_host", lambda h: ["127.0.0.1"])
    assert fetch.reachable("example.org") is True  # loopback always has a route, and a UDP connect sends nothing


def test_unreadable_robots_disallows_for_ten_minutes_not_a_day(home, policy):
    """A robots.txt lost to a network error used to be remembered for a day as "no robots.txt: everything allowed"."""
    state = {"robots": "down"}

    def robots(req):
        if state["robots"] == "down":
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n", headers={"Content-Type": "text/plain"})

    f, conn, seen, clock = make(home, policy, {(HOST, "/robots.txt"): robots, (HOST, "/x"): {"body": {}}})
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.error == "refused:robots_unreachable" and [c[1] for c in seen.calls] == ["/robots.txt"]
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).error == "refused:robots_unreachable"
    assert [c[1] for c in seen.calls] == ["/robots.txt"]  # not asked again within ten minutes
    state["robots"] = "up"
    clock.t += 601
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).ok
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/private/y", key="k2"), SRC, SPEC)).error == "refused:robots_disallow"


def test_failed_lookup_is_not_remembered_as_a_public_address(home, policy):
    answers = iter([[], ["10.0.0.5"]])  # DNS down, then back with a private address
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/x"): {"body": {}}},
                            resolve=lambda h: next(answers))
    run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.error == "refused:private_address"


def test_slow_drip_and_silent_servers_hit_the_total_deadline(home, policy):
    """One byte every 50 ms never trips a per-read timeout; the total deadline stops it, and the host's breaker counts it."""
    async def drip():
        while True:
            yield b"x"
            await asyncio.sleep(0.05)

    async def silent(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={})
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/drip"): lambda r: httpx.Response(200, content=drip(), headers={"Content-Type": "application/json"}),
              (HOST, "/silent"): silent}
    f, conn, seen, _ = make(home, policy, routes)
    f.deadline_s = 0.3
    import time as _t
    t0 = _t.monotonic()
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/drip", key="d"), SRC, SPEC))
    r2 = run(f.fetch(FetchPlan(url=f"https://{HOST}/silent", key="s"), SRC, SPEC))
    assert r.error == "DeadlineExceeded" and r2.error == "DeadlineExceeded" and _t.monotonic() - t0 < 3
    assert "0.3 s" in r.error_detail and len(f.gate.state(HOST).errors) == 2


def test_request_deadline_comes_from_policy_and_only_tightens(home, policy):
    from ninshubur.policy import merge_tightening
    f, *_ = make(home, policy, {})
    assert f.deadline_s == 20.0
    shipped = {"fetch": {"request_deadline_s": 20}}
    assert merge_tightening(shipped, {"fetch": {"request_deadline_s": 10}})[0]["fetch"]["request_deadline_s"] == 10
    merged, notes = merge_tightening(shipped, {"fetch": {"request_deadline_s": 600}})
    assert merged["fetch"]["request_deadline_s"] == 20 and "would loosen" in notes[0]


def test_sources_sharing_a_fetcher_take_turns_on_a_host(home, policy):
    """Four sources fetching one host at once: one request in flight at a time, each start a full gap after the last."""
    flight = {"now": 0, "max": 0}
    clock = Clock()
    starts: list[float] = []

    async def page(request):
        starts.append(clock.t)
        flight["now"] += 1
        flight["max"] = max(flight["max"], flight["now"])
        await asyncio.sleep(0.01)
        flight["now"] -= 1
        return httpx.Response(200, json={})
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "*"): page}, clock=clock)
    run(f.fetch(FetchPlan(url=f"https://{HOST}/warm", key="w"), SRC, SPEC))  # robots.txt cached first

    async def four():
        return await asyncio.gather(*(f.fetch(FetchPlan(url=f"https://{HOST}/p{i}", key=f"k{i}"), SRC, SPEC) for i in range(4)))
    assert all(r.ok for r in run(four()))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert flight["max"] == 1 and min(gaps) >= f.gate.min_gap - 1e-9


def test_gzip_is_decoded_in_bounded_steps():
    """A 1000x gzip chunk never decodes in one piece: no step is larger than DECODE_STEP."""
    from ninshubur.fetch import DECODE_STEP, BoundedDecoder
    payload = gzip.compress(b"0" * 20_000_000)
    d = BoundedDecoder("gzip")
    sizes = [len(x) for x in d.steps(payload)]
    assert max(sizes) <= DECODE_STEP and sum(sizes) == 20_000_000
    assert b"".join(BoundedDecoder("identity").steps(b"abc")) == b"abc" and list(BoundedDecoder(None).steps(b"")) == []


def test_bomb_stops_within_one_step_of_the_cap(home, policy):
    payload = gzip.compress(b"0" * 50_000_000)  # about 50 KB on the wire
    held = {}

    def bomb(request):
        return httpx.Response(200, content=payload, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "/bomb"): bomb})
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/bomb", key="k", max_bytes=1_000_000), SRC, SPEC))
    held["codes"] = {x.code for x in r.findings}
    assert held["codes"] & {"decompression_bomb", "body_too_large"} and r.body == b""


def wire(raw: bytes, encoding: str) -> httpx.Response:
    """Bytes exactly as sent, with a Content-Encoding httpx must not decode on the test's side."""
    return httpx.Response(200, headers={"Content-Type": "application/json", "Content-Encoding": encoding},
                          stream=httpx.ByteStream(raw))


@pytest.mark.parametrize("enc", ["br", "zstd", "deflate", "gzip, gzip", "compress"])
def test_encodings_never_asked_for_are_refused_unread(home, policy, enc):
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""},
                                           (HOST, "/x"): lambda r: wire(b'{"a": 1}', enc)})
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["unsupported_encoding"] and not r.ok


def test_gzip_body_round_trips_and_corrupt_gzip_is_a_finding(home, policy):
    good = gzip.compress(b'{"a": 1}')
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""},
                                           (HOST, "/g"): lambda r: wire(good, "gzip"),
                                           (HOST, "/bad"): lambda r: wire(b"\x1f\x8b\x08garbage" * 4, "gzip")})
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/g", key="g"), SRC, SPEC)).body == b'{"a": 1}'
    assert "bad_encoding" in {x.code for x in run(f.fetch(FetchPlan(url=f"https://{HOST}/bad", key="b"), SRC, SPEC)).findings}


def test_wrong_content_type_is_refused_before_the_body_is_read(home, policy):
    """An executable (or a 2 GB video) announced in the headers is never downloaded: the type is checked first."""
    pulled = {"bytes": 0}

    async def endless():
        while True:
            pulled["bytes"] += 65536
            yield b"MZ" + b"\0" * 65534

    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (HOST, "/x"): lambda r: httpx.Response(200, headers={"Content-Type": "application/octet-stream"}, content=endless())}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert [x.code for x in r.findings] == ["content_type_mismatch"] and r.body == b"" and pulled["bytes"] == 0


WWW = "www." + HOST


def test_robots_redirect_to_the_www_twin_is_followed_and_its_rules_apply(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 301, "headers": {"Location": f"https://{WWW}/robots.txt"}, "body": ""},
              (WWW, "/robots.txt"): {"body": "User-agent: *\nDisallow: /private\n", "headers": {"Content-Type": "text/plain"}},
              (HOST, "*"): {"body": {}}}
    f, conn, seen, _ = make(home, policy, routes)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/ok", key="a"), SRC, SPEC)).ok
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/private/x", key="b"), SRC, SPEC)).error == "refused:robots_disallow"


@pytest.mark.parametrize("hops,target", [(1, "https://elsewhere.example/robots.txt"), (6, None)])
def test_robots_redirect_off_site_or_past_five_hops_closes_the_host(home, policy, hops, target):
    routes = {(HOST, "*"): {"body": {}}}
    for i in range(hops):
        nxt = target if (target and i == hops - 1) else f"https://{HOST}/robots{i + 1}.txt"
        routes[(HOST, "/robots.txt" if i == 0 else f"/robots{i}.txt")] = {"status": 302, "headers": {"Location": nxt}, "body": ""}
    if target is None:
        routes[(HOST, f"/robots{hops}.txt")] = {"body": "", "headers": {"Content-Type": "text/plain"}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC))
    assert r.error == "refused:robots_unreachable" and "elsewhere.example" not in seen.hosts()
    assert [c[1] for c in seen.calls if c[1] == "/x"] == []
    assert any(e["event"] == "robots_redirect_refused" for e in read_jsonl(home / "logs" / "policy.jsonl"))


def test_robots_five_hops_are_still_followed(home, policy):
    routes = {(HOST, "*"): {"body": {}}}
    for i in range(5):
        routes[(HOST, "/robots.txt" if i == 0 else f"/robots{i}.txt")] = {
            "status": 307, "headers": {"Location": f"https://{HOST}/robots{i + 1}.txt"}, "body": ""}
    routes[(HOST, "/robots5.txt")] = {"body": "User-agent: *\nDisallow: /no\n", "headers": {"Content-Type": "text/plain"}}
    f, conn, seen, _ = make(home, policy, routes)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/yes", key="a"), SRC, SPEC)).ok
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/no", key="b"), SRC, SPEC)).error == "refused:robots_disallow"


def test_oversized_robots_is_parsed_up_to_500_kib_not_ignored(home, policy):
    """Over the cap used to mean "no rules" (allow everything). Now the first 500 KiB are parsed, to the last full line."""
    pad = "# padding line\n" * 40_000  # about 600 KB
    body = "User-agent: *\nDisallow: /early\n" + pad + "Disallow: /late\n"
    routes = {(HOST, "/robots.txt"): {"body": body, "headers": {"Content-Type": "text/plain"}}, (HOST, "*"): {"body": {}}}
    f, conn, seen, _ = make(home, policy, routes)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/early", key="a"), SRC, SPEC)).error == "refused:robots_disallow"
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/late", key="b"), SRC, SPEC)).ok  # past the limit: not read
    stored = conn.execute("SELECT body FROM robots WHERE host=?", (HOST,)).fetchone()["body"]
    assert len(stored.encode()) <= 512_000 and stored.endswith("\n")


@pytest.mark.parametrize("body,blocked", [
    ("User-agent: *\rDisallow: /early\r" + "# padding line\r" * 40_000, "refused:robots_disallow"),  # CR-only lines
    ("User-agent: * Disallow: /early " + "#" * 600_000, "refused:robots_unreachable"),  # no line break at all
], ids=["cr-only-lines", "no-line-break"])
def test_oversized_robots_without_a_newline_never_reads_as_no_rules(home, policy, body, blocked):
    """Cutting at the last "\\n" left nothing here, and an empty robots.txt allows everything."""
    routes = {(HOST, "/robots.txt"): {"body": body, "headers": {"Content-Type": "text/plain"}}, (HOST, "*"): {"body": {}}}
    f, conn, seen, _ = make(home, policy, routes)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/early", key="a"), SRC, SPEC)).error == blocked


def test_robots_bomb_closes_the_host(home, policy):
    payload = gzip.compress(b"#" * 60_000_000)

    async def on_the_wire():  # as a network delivers it: a few KB at a time
        for i in range(0, len(payload), 4096):
            yield payload[i:i + 4096]
    routes = {(HOST, "/robots.txt"): lambda r: httpx.Response(200, headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                                                              content=on_the_wire()),
              (HOST, "*"): {"body": {}}}
    f, conn, seen, _ = make(home, policy, routes)
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).error == "refused:robots_unreachable"


def test_redirect_to_the_www_twin_obeys_the_twins_robots(home, policy):
    routes = {(HOST, "/robots.txt"): {"status": 404, "body": ""},
              (WWW, "/robots.txt"): {"body": "User-agent: *\nDisallow: /a\n", "headers": {"Content-Type": "text/plain"}},
              (HOST, "/a"): {"status": 308, "headers": {"Location": f"https://{WWW}/a"}, "body": ""},
              (WWW, "/a"): {"body": {"twin": True}}}
    f, conn, seen, _ = make(home, policy, routes)
    r = run(f.fetch(FetchPlan(url=f"https://{HOST}/a", key="k"), SRC, SPEC))
    assert r.error == "refused:robots_disallow" and (WWW, "/a", "GET", False) not in seen.calls


def test_a_cached_redirect_from_an_older_version_is_asked_again_soon(home, policy):
    clock = Clock()
    f, conn, seen, _ = make(home, policy, {(HOST, "/robots.txt"): {"status": 404, "body": ""}, (HOST, "*"): {"body": {}}}, clock=clock)
    f.robots.store(HOST, "", 301, None)  # what the old code stored for a redirecting robots.txt (read as "no rules")
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).error == "refused:robots_unreachable"
    clock.t += 601
    assert run(f.fetch(FetchPlan(url=f"https://{HOST}/x", key="k"), SRC, SPEC)).ok
