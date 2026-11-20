"""E2: the tripwire (plan section 5: three attempts to reach a denied range in 24 h pause the explorer, fail
closed). Attempts are requests that would have reached one (connect-time refusals, redirects); links a page only
mentions are refused and logged but never counted."""
from __future__ import annotations

import json
import os

import pytest

from ninshubur.explorer.frontier import Limits
from ninshubur.explorer.killswitch import read_stop, resume
from ninshubur.explorer.tripwire import check, count, log_path
from tests.conftest import NOW
from tests.test_explorer_attacks import NO_ROBOTS, fetch, log, make, page, run


def private(host):
    return ["10.0.0.7"] if host.startswith("inside") else ["93.184.215.14"]


def test_three_attempts_stop_the_explorer_and_each_stops_its_site(home, policy):
    routes = {("site.example", "/robots.txt"): NO_ROBOTS,
              ("site.example", "/go"): {"status": 302, "body": "", "headers": {"Location": "https://127.0.0.1/admin"}},
              ("inside-a.example", "*"): page("x"), ("inside-b.example", "*"): page("x")}
    gate, seen, clock = make(home, policy, routes, resolve=private)
    fetch(gate, "https://inside-a.example/")
    fetch(gate, "https://site.example/go")
    assert not gate.stopped and gate.frontier.offer("https://inside-a.example/other").code == "site_stopped"
    fetch(gate, "https://inside-b.example/")
    note = read_stop(home)
    assert gate.stopped and note["by"] == "tripwire" and "3 attempts" in note["reason"] and gate.tripped
    o = gate.frontier.offer("https://fine.example/")
    assert run(gate.fetch(o.url)).code == "stopped"  # nothing more is asked for


def test_links_a_page_only_mentions_never_count(home, policy):
    gate, _, _ = make(home, policy, {})
    for i in range(10):
        assert gate.frontier.offer(f"http://localhost:{8000 + i}/", found_from="https://blog.example/post").code
    assert {e["via"] for e in log(home)} == {"link"} and count(home, now=NOW, window_s=86400).n == 0
    assert check(home, Limits(), now=NOW) is None and read_stop(home) is None


def test_the_window_and_resume_reset_the_count(home, policy):
    gate, _, clock = make(home, policy, {("inside.example", "*"): page("x")}, resolve=private)
    fetch(gate, "https://inside.example/")
    assert count(home, now=clock.t, window_s=86400).n == 1
    assert count(home, now=clock.t + 86401, window_s=86400).n == 0
    lim = Limits(tripwire_ssrf=1)
    assert "1 attempts" in check(home, lim, now=clock.t) and read_stop(home)["by"] == "tripwire"
    resume(home, now=clock.t + 5)
    assert count(home, now=clock.t + 10, window_s=86400).n == 0 and check(home, lim, now=clock.t + 10) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_deleting_the_stop_file_by_hand_does_not_trip_again(home, policy):
    """Review finding 6: only `explore resume` reset the count, so after `rm STOP` (which the kill switch allows) the
    next run saw the same three attempts, wrote STOP again and stayed stuck for up to 24 h."""
    gate, _, clock = make(home, policy, {("inside-a.example", "*"): page("x"), ("inside-b.example", "*"): page("x"),
                                         ("inside-c.example", "*"): page("x"), ("inside-d.example", "*"): page("x")},
                          resolve=private)
    lim = gate.frontier.limits
    for h in "abc":
        fetch(gate, f"https://inside-{h}.example/")
    assert gate.stopped and read_stop(home)["by"] == "tripwire"
    gate.stop_path.unlink()  # by hand, no `explore resume`
    clock.t += 60
    assert check(home, lim, now=clock.t) is None and not gate.stopped
    assert count(home, now=clock.t, window_s=lim.tripwire_window_s).n == 0
    fetch(gate, "https://inside-d.example/")  # a new attempt counts from the stop: one of three
    assert count(home, now=clock.t, window_s=lim.tripwire_window_s).n == 1 and not gate.stopped


def test_a_stop_by_alex_does_not_reset_the_count(home, policy):
    from ninshubur.explorer.killswitch import stop
    gate, _, clock = make(home, policy, {("inside-a.example", "*"): page("x"), ("inside-b.example", "*"): page("x")},
                          resolve=private)
    lim = gate.frontier.limits
    fetch(gate, "https://inside-a.example/")
    fetch(gate, "https://inside-b.example/")
    stop(home, "lunch", now=clock.t)
    gate.stop_path.unlink()
    assert count(home, now=clock.t, window_s=lim.tripwire_window_s).n == 2


def test_an_unreadable_log_fails_closed(home):
    p = log_path(home)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"event": "noise", "at": NOW}) + "\n")
    p.chmod(0)
    try:
        assert "refusing to run" in check(home, Limits(), now=NOW)
    finally:
        p.chmod(0o600)


def test_a_symlinked_log_fails_closed(home, tmp_path):
    p = log_path(home)
    p.parent.mkdir(parents=True)
    (tmp_path / "empty.jsonl").write_text("")
    p.symlink_to(tmp_path / "empty.jsonl")
    assert "refusing to run" in check(home, Limits(), now=NOW)

