"""The explorer's attack table (plan section 5), the E1 rows: every row goes through the explorer's own path
(frontier -> gate -> shared Fetcher) and must be blocked *and* logged.

Rows that need the parser or the web app (hidden Unicode and alt-text injection, hostile HTML in a title, a forged
cross-site write, a wrong Host header) belong to sessions E2 and W and are added there.
"""
from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from ninshubur.config import load_config
from ninshubur.explorer.gate import ExplorerGate
from ninshubur.explorer.store import connect
from ninshubur.logs import read_jsonl
from tests.conftest import NOW, Seen, make_transport, public_resolver

HTML = {"Content-Type": "text/html; charset=utf-8"}
NO_ROBOTS = {"status": 404, "body": ""}
DENIED_ADDRESSES = ["127.0.0.1", "::1", "10.47.83.1", "169.254.169.254", "::ffff:127.0.0.1", "64:ff9b::7f00:1",
                    "192.168.1.1", "100.64.0.1", "fd00::1", "2002:7f00:1::1"]


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def make(home, policy, routes: dict, resolve=None):
    seen, clock = Seen(), Clock()
    gate = ExplorerGate(connect(home), policy, load_config(home, policy=policy), home,
                        transport=make_transport(routes, seen), resolve=resolve or public_resolver,
                        sleep=clock.sleep, now=clock.now)
    return gate, seen, clock


def run(coro):
    return asyncio.run(coro)


def log(home, name="explorer.jsonl"):
    return read_jsonl(home / "explorer" / "logs" / name)


def page(text: str) -> dict:
    return {"body": f"<html><body><p>{text}</p></body></html>", "headers": HTML}


def fetch(gate, url):
    o = gate.frontier.offer(url)
    assert o.queued, o
    return run(gate.fetch(o.url))


# -- where it goes ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("addr", DENIED_ADDRESSES)
def test_a_name_resolving_to_a_denied_address_is_never_contacted(home, policy, addr):
    gate, seen, _ = make(home, policy, {("inside.example", "*"): page("secret")}, resolve=lambda h: [addr])
    out = fetch(gate, "https://inside.example/")
    assert out.code == "private_address" and seen.calls == []
    assert any(e["event"] == "ssrf_attempt" and e["via"] in ("connect", "redirect") for e in log(home))


@pytest.mark.parametrize("target", ["https://127.0.0.1/admin", "https://[::1]/", "https://169.254.169.254/latest/meta-data/",
                                    "https://[::ffff:127.0.0.1]/", "https://[64:ff9b::7f00:1]/", "https://2130706433/",
                                    "https://ex.example:22/", "https://ex.example:6379/",
                                    "https://localhost:11434/api/tags", "http://10.47.83.1/"])
def test_a_redirect_to_an_internal_target_is_offered_never_followed_and_refused(home, policy, target):
    routes = {("site.example", "/robots.txt"): NO_ROBOTS,
              ("site.example", "/go"): {"status": 302, "body": "", "headers": {"Location": target}}}
    gate, seen, _ = make(home, policy, routes)
    out = fetch(gate, "https://site.example/go")
    assert out.code.startswith("redirect_refused:") and out.offered == [target]
    assert [c[1] for c in seen.calls] == ["/robots.txt", "/go"]  # nothing went to the target
    assert any(e["event"] == "ssrf_attempt" and e["via"] in ("connect", "redirect") for e in log(home))


def test_a_location_header_httpx_cannot_parse_ends_the_fetch_unfollowed(home, policy):
    """An octal IP in Location (``0177.0.0.1``) doesn't even parse as a URL: the fetch ends as a protocol error,
    the target is never contacted, and the fetch log names the reason."""
    routes = {("site.example", "/robots.txt"): NO_ROBOTS,
              ("site.example", "/go"): {"status": 302, "body": "", "headers": {"Location": "https://0177.0.0.1/"}}}
    gate, seen, _ = make(home, policy, routes)
    assert fetch(gate, "https://site.example/go").code == "RemoteProtocolError"
    assert [c[1] for c in seen.calls] == ["/robots.txt", "/go"]
    last = log(home, "fetch.jsonl")[-1]
    assert last["error"] == "RemoteProtocolError" and "location" in last["detail"].lower()


def test_a_redirect_to_a_public_name_that_resolves_private_is_refused_when_its_turn_comes(home, policy):
    routes = {("site.example", "/robots.txt"): NO_ROBOTS,
              ("site.example", "/go"): {"status": 301, "body": "", "headers": {"Location": "https://evil.example/x"}},
              ("evil.example", "*"): page("metadata")}
    resolve = lambda h: ["127.0.0.1"] if h == "evil.example" else public_resolver(h)  # noqa: E731
    gate, seen, _ = make(home, policy, routes, resolve=resolve)
    assert fetch(gate, "https://site.example/go").code == "redirect_offered"
    later = run(gate.step(5))
    assert [o.code for o in later] == ["private_address"]
    assert all(c[0] != "evil.example" for c in seen.calls)


def test_dns_rebinding_cannot_swap_the_address_after_the_check(home, policy):
    answers = iter([["93.184.216.34"]] + [["127.0.0.1"]] * 10)
    routes = {("rebind.example", "/robots.txt"): NO_ROBOTS, ("rebind.example", "*"): page("hello")}
    gate, seen, clock = make(home, policy, routes, resolve=lambda h: next(answers))
    assert fetch(gate, "https://rebind.example/a").code == "ok"
    assert fetch(gate, "https://rebind.example/b").code == "ok"
    assert set(seen.addresses) == {"93.184.216.34"}  # the second DNS answer was never used for a connection
    clock.t += 601
    n = len(seen.calls)
    assert fetch(gate, "https://rebind.example/c").code == "private_address" and len(seen.calls) == n


def test_wildcard_subdomain_trap_gets_four_hosts_then_nothing(home, policy):
    gate, _, _ = make(home, policy, {})
    got = [gate.frontier.offer(f"https://x{i}.wild.example/").queued for i in range(10)]
    assert got == [True] * 4 + [False] * 6
    assert {e["code"] for e in log(home)} == {"trap_subdomains"}


def test_calendar_trap_is_capped_by_shape_and_by_date(home, policy):
    gate, _, _ = make(home, policy, {})
    got = [gate.frontier.offer(f"https://cal.example/events/month/2026-{m:02d}/").code for m in range(1, 13)]
    assert got.count(None) == 8 and set(got[8:]) == {"trap_shape"}
    assert gate.frontier.offer("https://cal.example/events/month/2140-01/").code == "trap_calendar"


def test_soft404_site_is_stopped_by_its_probe(home, policy):
    routes = {("soft.example", "/robots.txt"): NO_ROBOTS, ("soft.example", "*"): page("welcome to everything")}
    gate, _, _ = make(home, policy, routes)
    gate.frontier.offer("https://soft.example/real")
    assert run(gate.probe_soft404("soft.example")) is True
    assert run(gate.step(5)) == []
    assert log(home)[-1] == {**log(home)[-1], "event": "site_stopped", "site": "soft.example"}


def test_endless_generated_maze_stops_its_site(home, policy):
    """Every page links onward under a fresh path and is built from one template, like AI Labyrinth or Nepenthes
    pages: the near-duplicate stop ends it within ten pages, whatever links the pages hold."""
    body = "<html><p>" + " ".join(f"generated{i}" for i in range(400)) + "</p>"
    routes = {("maze.example", "/robots.txt"): NO_ROBOTS,
              ("maze.example", "*"): lambda r: httpx.Response(200, content=(body + r.url.path).encode(), headers=HTML)}
    gate, seen, _ = make(home, policy, routes)
    paths = [f"/{w}-{i}" for i, w in enumerate("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod".split())]
    stopped = None
    for p in paths:
        if not gate.frontier.offer(f"https://maze.example{p}").queued:
            break
        gate.frontier.conn.execute("UPDATE explorer_sites SET stage='admitted'")  # the widest day cap (ladder: E2)
        out = run(gate.step(1))[0]
        stopped = stopped or out.stopped_site
    assert stopped and stopped.startswith("near_duplicates")
    assert len([c for c in seen.calls if c[1] != "/robots.txt"]) == 10


# -- what it touches -------------------------------------------------------------------------------------------------
def test_gzip_bomb_is_stopped_near_the_cap(home, policy):
    bomb = gzip.compress(b"<p>" + b"0" * 30_000_000)
    routes = {("bomb.example", "/robots.txt"): NO_ROBOTS,
              ("bomb.example", "/"): lambda r: httpx.Response(200, content=bomb, headers={**HTML, "Content-Encoding": "gzip"})}
    gate, _, _ = make(home, policy, routes)
    out = fetch(gate, "https://bomb.example/")
    assert {"decompression_bomb", "body_too_large"} & set(out.code.split(",")) and out.body == b""


def test_ten_megabyte_page_is_cut_at_the_explorer_cap(home, policy):
    routes = {("big.example", "/robots.txt"): NO_ROBOTS,
              ("big.example", "/"): lambda r: httpx.Response(200, content=b"<p>" + b"x" * 10_000_000, headers=HTML)}
    gate, _, _ = make(home, policy, routes)
    out = fetch(gate, "https://big.example/")
    assert out.code == "body_too_large" and out.body == b"" and gate.max_bytes == 1_000_000


def test_slow_drip_page_hits_the_total_deadline(home, policy):
    async def drip():
        while True:
            yield b"x"
            await asyncio.sleep(0.05)
    routes = {("drip.example", "/robots.txt"): NO_ROBOTS,
              ("drip.example", "/"): lambda r: httpx.Response(200, content=drip(), headers=HTML)}
    gate, _, _ = make(home, policy, routes)
    gate.fetcher.deadline_s = 0.3
    assert fetch(gate, "https://drip.example/").code == "DeadlineExceeded"


def test_images_and_binaries_are_refused_before_the_body(home, policy):
    routes = {("img.example", "/robots.txt"): NO_ROBOTS,
              ("img.example", "/"): {"body": b"\x89PNG\r\n\x1a\n" + b"0" * 1000, "headers": {"Content-Type": "image/png"}}}
    gate, _, _ = make(home, policy, routes)
    assert fetch(gate, "https://img.example/").code == "content_type_mismatch"


def test_robots_redirect_off_site_closes_the_host(home, policy):
    routes = {("r3.example", "/robots.txt"): {"status": 301, "body": "", "headers": {"Location": "https://elsewhere.example/robots.txt"}},
              ("r3.example", "*"): page("x")}
    gate, seen, _ = make(home, policy, routes)
    assert fetch(gate, "https://r3.example/").code == "robots_unreachable"
    assert [c[1] for c in seen.calls] == ["/robots.txt"]


def test_oversized_robots_is_read_to_the_limit_not_ignored(home, policy):
    body = "User-agent: *\nDisallow: /\n" + "# padding line\n" * 40_000  # about 600 KB
    routes = {("bigrobots.example", "/robots.txt"): {"body": body, "headers": {"Content-Type": "text/plain"}},
              ("bigrobots.example", "*"): page("x")}
    gate, seen, _ = make(home, policy, routes)
    assert fetch(gate, "https://bigrobots.example/page").code == "robots_disallow"
    assert [c[1] for c in seen.calls] == ["/robots.txt"]


# -- kill switch -----------------------------------------------------------------------------------------------------
def test_stop_file_blocks_every_request_and_leaves_the_queue_alone(home, policy):
    routes = {("a.example", "/robots.txt"): NO_ROBOTS, ("a.example", "*"): page("a")}
    gate, seen, _ = make(home, policy, routes)
    gate.frontier.offer("https://a.example/one")
    gate.stop_path.write_text("")
    assert run(gate.step(5)) == [] and seen.calls == []
    assert run(gate.fetch("https://a.example/one")).code == "stopped" and seen.calls == []
    gate.stop_path.unlink()
    assert [o.code for o in run(gate.step(5))] == ["ok"]


def test_stop_file_is_checked_before_each_connection_not_once_per_fetch(home, policy):
    """STOP appears while a fetch is under way (after robots.txt was asked): the page itself is never requested."""
    gate = None

    def robots(request):
        gate.stop_path.write_text("")
        return httpx.Response(404, text="")
    routes = {("b.example", "/robots.txt"): robots, ("b.example", "*"): page("b")}
    gate, seen, _ = make(home, policy, routes)
    out = fetch(gate, "https://b.example/page")
    assert out.code == "stopped" and [c[1] for c in seen.calls] == ["/robots.txt"]


def test_the_explorer_keeps_its_own_logs_ledger_and_database(home, policy):
    routes = {("own.example", "/robots.txt"): NO_ROBOTS, ("own.example", "*"): page("own")}
    gate, _, _ = make(home, policy, routes)
    fetch(gate, "https://own.example/")
    assert (home / "explorer" / "logs" / "fetch.jsonl").exists() and not (home / "logs" / "fetch.jsonl").exists()
    assert not (home / "ninshubur.db").exists()
    assert gate.fetcher.daily_bytes <= 50_000_000


def test_a_redirect_target_is_one_hop_further_and_refused_past_max_hops(home, policy):
    """Second review, 20 Nov: every redirect target was queued at depth 1, whatever the depth of the page that
    redirected, so a chain of off-site redirects walked on for ever despite ``max_hops: 2``."""
    routes = {("site.example", "/robots.txt"): NO_ROBOTS}
    for path, to in (("/near", "https://a.example/"), ("/far", "https://b.example/"), ("/far-in", "https://127.0.0.1/")):
        routes[("site.example", path)] = {"status": 302, "body": "", "headers": {"Location": to}}
    gate, seen, _ = make(home, policy, routes)
    depth_of = lambda u: gate.frontier.conn.execute("SELECT depth FROM explorer_frontier WHERE url=?", (u,)).fetchone()  # noqa: E731
    assert gate.frontier.offer("https://site.example/near", depth=1).queued
    assert run(gate.fetch("https://site.example/near")).code == "redirect_offered"
    assert depth_of("https://a.example/")[0] == 2
    assert gate.frontier.offer("https://site.example/far", depth=2).queued
    assert run(gate.fetch("https://site.example/far")).code == "redirect_refused:max_hops"
    assert depth_of("https://b.example/") is None
    assert not any(e["event"] == "ssrf_attempt" for e in log(home))  # too far is no attack
    # an internal target is still what it is, however deep the page that points at it
    assert gate.frontier.offer("https://site.example/far-in", depth=2).queued
    assert run(gate.fetch("https://site.example/far-in")).code == "redirect_refused:url_ip_literal"
    assert [e["via"] for e in log(home) if e["event"] == "ssrf_attempt"] == ["redirect_target", "redirect"]


@pytest.mark.parametrize("target, counted", [
    ("tg://resolve?domain=someone", False), ("ftp://files.example.org/x", False), ("intent://scan/#Intent;end", False),
    ("gopher://127.0.0.1:70/_x", True), ("ftp://localhost/", True), ("ftp://2130706433/", True),
    ("https://ex.example:8443/", True)])
def test_a_redirect_to_another_scheme_counts_only_when_it_names_an_address(home, policy, target, counted):
    """Second review, 20 Nov: a page that redirected to an app link or an FTP server was stopped as an attempt "to
    reach a denied address" and counted; three such in a day would have written the STOP file. (``mailto:`` never
    gets this far: httpx can't build a request from it, and the fetch ends as InvalidURL.)"""
    routes = {("site.example", "/robots.txt"): NO_ROBOTS,
              ("site.example", "/go"): {"status": 302, "body": "", "headers": {"Location": target}}}
    gate, seen, _ = make(home, policy, routes)
    out = fetch(gate, "https://site.example/go")
    assert out.code.startswith("redirect_refused:") and [c[1] for c in seen.calls] == ["/robots.txt", "/go"]
    strong = [e for e in log(home) if e["event"] == "ssrf_attempt" and e["via"] == "redirect"]
    status = gate.frontier.conn.execute("SELECT status FROM explorer_sites WHERE site='site.example'").fetchone()[0]
    assert (len(strong), status) == ((1, "stopped") if counted else (0, "open"))
