"""The explorer's frontier and trap rules: canonical URLs, trap shapes, per-site budgets, stop rules."""
from __future__ import annotations

import time

import pytest

from ninshubur.explorer import traps
from ninshubur.explorer.frontier import Frontier, Limits
from ninshubur.explorer.store import connect
from ninshubur.logs import read_jsonl
from tests.conftest import NOW

LIM = dict(max_len=512, max_depth=6, max_query_params=4, year_min=1995, year_max=2028)


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(home, policy, **limits):
    clock = Clock()
    f = Frontier(connect(home), policy, home, now=clock, limits=Limits(**limits) if limits else None)
    return f, clock


def events(home):
    return read_jsonl(home / "explorer" / "logs" / "explorer.jsonl")


# -- traps (pure) --------------------------------------------------------------------------------------------------
def test_canonical_drops_what_makes_one_page_look_like_many():
    got = traps.canonical("HTTPS://Ex.ORG:443//a//b;jsessionid=XYZ?utm_source=x&b=2&a=1&PHPSESSID=9&gclid=q#frag")
    assert got == "https://ex.org/a/b?a=1&b=2"
    assert traps.canonical("https://ex.org") == "https://ex.org/"
    assert traps.canonical("https://ex.org/?q=a%20b") == "https://ex.org/?q=a+b"  # one spelling of the same query


def test_shape_folds_numbers_and_query_values():
    assert traps.shape("https://ex.org/events/2026/11/18?page=3&q=x") == traps.shape("https://ex.org/events/2027/01/02?q=y&page=9")
    assert traps.shape("https://ex.org/events/2026/11/18") != traps.shape("https://ex.org/talks/2026/11/18")


@pytest.mark.parametrize("url,code", [
    ("https://ex.org/blog/2024/05/a-post", None),
    ("https://ex.org/p/2345/12", None),                     # an id, not a date
    ("https://ex.org/calendar/2099/01/", "trap_calendar"),  # a calendar walked decades ahead
    ("https://ex.org/calendar/1901/01/", "trap_calendar"),
    ("https://ex.org/events/?date=2150-01-01", "trap_calendar"),
    ("https://ex.org/events/?tribe-bar-date=1970-01", "trap_calendar"),
    ("https://ex.org/a/b/a/b", "trap_repeated_segment"),
    ("https://ex.org/x/y/x/z/x", "trap_repeated_segment"),
    ("https://ex.org/1/2/3/4/5/6/7", "trap_depth"),
    ("https://ex.org/?a=1&b=2&c=3&d=4&e=5", "trap_query_params"),
    ("https://ex.org/" + "a" * 600, "trap_url_length"),
])
def test_url_traps(url, code):
    assert traps.url_trap(url, **LIM) == code


def test_simhash_tells_near_copies_from_different_pages():
    a = b"<html><p>" + b" ".join(b"word%d" % i for i in range(1000)) + b"</p></html>"
    b = a.replace(b"word7 ", b"other ")
    c = b"<p>" + b" ".join(b"different%d text" % i for i in range(300)) + b"</p>"
    assert traps.hamming(traps.simhash(a), traps.simhash(b)) <= 3
    assert traps.hamming(traps.simhash(a), traps.simhash(c)) > 10


def test_fingerprint_ignores_scripts_and_survives_hostile_markup():
    assert traps.visible_words(b"<p>a b</p><script>var x=1</script><STYLE>p{}</STYLE><p>c</p>") == ["a", "b", "c"]
    for evil in (b"<script" * 50_000, b"<" * 300_000, b"<style>" + b"x" * 300_000):
        t0 = time.monotonic()
        traps.simhash(evil)
        assert time.monotonic() - t0 < 1.0


def test_parked_and_soft404():
    assert traps.parked(b"<h1>This domain is for sale!</h1>") == "this domain is for sale"
    assert traps.parked(b"<h1>Welcome</h1>") is None
    assert traps.soft404(200) and traps.soft404(302, "https://ex.org/") and traps.soft404(301, "/")
    assert not traps.soft404(404) and not traps.soft404(302, "https://ex.org/login")


# -- frontier ------------------------------------------------------------------------------------------------------
def test_offer_queues_the_canonical_https_form_once(home, policy):
    f, _ = make(home, policy)
    o = f.offer("http://Ex.org/a?utm_source=x#top")
    assert o.queued and o.url == "https://ex.org/a" and o.site == "ex.org"
    assert f.offer("https://ex.org/a").code == "already_known"
    assert [r["url"] for r in f.next_batch(5)] == ["https://ex.org/a"]


@pytest.mark.parametrize("url,code", [
    ("https://127.0.0.1/", "url_ip_literal"), ("https://[::1]/", "url_ip_literal"),
    ("https://2130706433/", "url_single_label"), ("https://0177.0.0.1/", "url_numeric_host"),
    ("https://ex.org:6379/", "url_port"), ("https://ex.org:22/", "url_port"), ("http://ex.org:8080/", "url_port"),
    ("https://user:pw@ex.org/", "url_userinfo"), ("file:///etc/passwd", "url_scheme"), ("gopher://ex.org/", "url_scheme"),
    ("https://intranet/", "url_single_label"),
])
def test_attack_urls_are_refused_and_logged_as_ssrf(home, policy, url, code):
    f, _ = make(home, policy)
    assert f.offer(url).code == code
    ev = events(home)[-1]
    assert ev["event"] == "ssrf_attempt" and ev["code"] == code


def test_denied_hosts_and_traps_are_logged(home, policy):
    f, _ = make(home, policy)
    assert f.offer("https://www.linkedin.com/in/x").code == "host_denied"
    assert f.offer("https://ex.org/cal/2099/01/").code == "trap_calendar"
    assert [e["event"] for e in events(home)] == ["frontier_refused", "frontier_refused"]


def test_wildcard_subdomains_share_one_site_and_a_host_cap(home, policy):
    f, _ = make(home, policy)
    got = [f.offer(f"https://h{i}.trap.example.com/").code for i in range(8)]
    assert got == [None] * 4 + ["trap_subdomains"] * 4
    assert f.offer("https://h0.trap.example.com/more").queued  # a host already seen keeps its place


def test_a_calendar_or_pagination_run_is_capped_by_shape(home, policy):
    f, _ = make(home, policy)
    got = [f.offer(f"https://ex.org/events/2026/{m:02d}/").code for m in range(1, 13)]
    assert got.count(None) == 8 and got[8:] == ["trap_shape"] * 4


def test_batches_take_one_url_per_site_and_respect_the_day_budget(home, policy):
    f, clock = make(home, policy, pages_per_site_day=2)
    for p in ("a", "b", "c"):
        f.offer(f"https://one.example/{p}")
    f.offer("https://two.example/a")
    f.conn.execute("UPDATE explorer_sites SET stage='admitted' WHERE site='one.example'")  # the admitted cap applies
    assert [r["url"] for r in f.next_batch(10)] == ["https://one.example/a", "https://two.example/a"]
    for u in ("https://one.example/a", "https://one.example/b"):
        f.record(u, status=200, body=b"<p>x</p>", useful=True)
    assert [r["url"] for r in f.next_batch(10)] == ["https://two.example/a"]  # one.example used its two pages today
    clock.t += 86400
    assert "https://one.example/c" in [r["url"] for r in f.next_batch(10)]


def test_a_maze_of_near_duplicate_pages_stops_its_site(home, policy):
    """Generated pages from one template, each under a fresh path (so no shape cap applies): the site stops once
    most of its last ten pages are near-copies, and nothing more from it is queued."""
    f, _ = make(home, policy)
    template = b"<html><p>" + b" ".join(b"filler%d" % i for i in range(400)) + b"</p>"
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet", "kilo"]
    stop = None
    for i, w in enumerate(words):
        u = f"https://maze.example/{w}"
        assert f.offer(u).queued
        stop = f.record(u, status=200, body=template + b"<p>page %d</p>" % i, useful=False)
        if stop:
            break
    assert stop and stop.startswith("near_duplicates") and i == 9  # the tenth page fills the window
    assert f.offer("https://maze.example/next").code == "site_stopped"
    assert any(e["event"] == "site_stopped" for e in events(home))


def test_a_site_that_never_yields_stops_after_the_limit(home, policy):
    f, _ = make(home, policy, no_yield_pages=5)
    for i in range(5):
        f.offer(f"https://dry.example/p{i}")
    results = [f.record(f"https://dry.example/p{i}", status=200, body=b"<p>%d unique words here %d</p>" % (i, i * 7))
               for i in range(5)]
    assert results[:4] == [None] * 4 and results[4].startswith("no_yield")
    assert f.next_batch(5) == []


def test_parked_and_soft404_sites_are_stopped(home, policy):
    f, _ = make(home, policy)
    f.offer("https://parked.example/")
    assert f.record("https://parked.example/", status=200, body=b"Buy this domain today").startswith("parked")
    f.offer("https://soft.example/x")
    assert f.note_soft404("soft.example", 200)
    assert f.next_batch(5) == []
    assert not f.note_soft404("fine.example", 404)


def test_record_refuses_a_url_that_was_never_offered(home, policy):
    f, _ = make(home, policy)
    with pytest.raises(KeyError, match="offer"):
        f.record("https://ex.org/never", status=200)


def test_limits_come_from_policy_and_only_tighten(home, policy):
    from ninshubur.policy import Policy
    (home / "policy.yaml").write_text("explorer:\n  hosts_per_site: 2\n  pages_per_shape: 50\n  content_types: [text/html]\n")
    lim = Limits.from_policy(Policy.load(home))
    assert lim.hosts_per_site == 2 and lim.pages_per_shape == 8 and lim.content_types == ("text/html",)


def test_stopping_a_site_never_seen_before_still_sticks(home, policy):
    """Regression: stop_site used to update no row for a site with nothing queued yet, so the stop was lost."""
    f, _ = make(home, policy)
    f.stop_site("never.example", "manual")
    assert f.offer("https://never.example/").code == "site_stopped"


def test_a_malformed_port_is_refused_not_raised(home, policy):
    f, _ = make(home, policy)
    assert f.offer("http://ex.org:99999/").code == "url_not_https"  # http is asked about before the port
    assert f.offer("https://ex.org:99999/").code == "url_bad_port"


def test_a_site_gets_one_page_a_day_until_its_trial_then_the_trial_cap(home, policy):
    f, clock = make(home, policy, trial_pages_per_day=2)
    for p in ("a", "b", "c"):
        f.offer(f"https://one.example/{p}")
    f.record("https://one.example/a", status=200, body=b"<p>x</p>", useful=True)
    assert f.next_batch(10) == []  # still a candidate: one page today
    f.conn.execute("UPDATE explorer_sites SET stage='trial'")
    assert [r["url"] for r in f.next_batch(10)] == ["https://one.example/b"]
    f.record("https://one.example/b", status=200, body=b"<p>y</p>", useful=True)
    assert f.next_batch(10) == []  # the trial cap of 2 is used


def test_an_e1_database_gets_the_ladder_columns(home, policy):
    from ninshubur.explorer import frontier as fr
    conn = connect(home)
    conn.execute("DROP TABLE IF EXISTS explorer_sites")
    conn.execute(fr.SCHEMA[0].split(",\n        stage TEXT")[0] + ")")  # the E1 shape
    assert "stage" not in {r[1] for r in conn.execute("PRAGMA table_info(explorer_sites)")}
    fr.ensure_schema(conn)
    assert set(fr.SITE_COLUMNS) <= {r[1] for r in conn.execute("PRAGMA table_info(explorer_sites)")}
