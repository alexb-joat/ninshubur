"""``ninshubur explore`` commands of lane E1: offer, queue, frontier, step (dry runs and the STOP file)."""
from __future__ import annotations

import pytest

from ninshubur.__main__ import main
from ninshubur.explorer import store
from ninshubur.explorer.store import Candidate
from tests.conftest import NOW, write_config, write_profiles


@pytest.fixture
def cli(home, capsys, monkeypatch):
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    write_config(home)
    write_profiles(home)

    def call(*args: str) -> tuple[int, str]:
        code = main(list(args))
        out = capsys.readouterr()
        return code, out.out + out.err

    return call


def test_offer_dry_run_writes_nothing(cli, home):
    code, out = cli("explore", "offer", "--dry-run", "https://ex.org/a", "https://127.0.0.1/")
    assert code == 0 and "would queue" in out and "refused: url_ip_literal" in out
    assert not (home / "explorer" / "logs" / "explorer.jsonl").exists()
    code, out = cli("explore", "frontier")
    assert "urls:  none" in out
    code, out = cli("explore", "offer", "https://ex.org/a")
    assert code == 0 and "queued" in out
    code, out = cli("explore", "offer", "https://127.0.0.1/")
    assert code == 1  # every URL refused


def test_queue_offers_candidates_once_and_skips_boards(cli, home):
    conn = store.connect(home)
    store.add_candidates(conn, [Candidate("https://one.example/", "logged_link", "src"),
                                Candidate("https://two.example/x", "hn", "hn-algolia"),
                                Candidate("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "board", "probe")], NOW)
    conn.close()
    code, out = cli("explore", "queue", "--dry-run")
    assert "2 candidates would be offered: queued 2" in out
    code, out = cli("explore", "queue")
    assert "2 candidates were offered: queued 2" in out
    code, out = cli("explore", "queue")
    assert "0 candidates were offered" in out


def test_step_dry_run_sends_nothing_and_stop_refuses(cli, home):
    cli("explore", "offer", "https://ex.org/a")
    code, out = cli("explore", "step", "--dry-run")
    assert code == 0 and "would fetch https://ex.org/a" in out and "nothing was sent" in out
    (home / "explorer" / "STOP").write_text("")
    code, out = cli("explore", "step")
    assert code == 2 and "explorer is stopped" in out and "explore resume" in out


def test_frontier_lists_caps_and_stopped_sites(cli, home):
    from ninshubur.explorer.frontier import Frontier
    from ninshubur.policy import Policy
    conn = store.connect(home)
    Frontier(conn, Policy.load(home), home).stop_site("maze.example", "near_duplicates: 9 of the last 10 pages")
    conn.close()
    code, out = cli("explore", "frontier")
    assert "stopped maze.example: near_duplicates" in out and "caps: 4 hosts" in out


# -- lane E2: run, sites, check ------------------------------------------------------------------------------------
def test_run_dry_run_lists_pages_and_their_probes_and_sends_nothing(cli, home):
    cli("explore", "offer", "https://ex.org/a")
    code, out = cli("explore", "run", "--dry-run")
    assert code == 0 and "would fetch https://ex.org/a (+ soft-404 probe)" in out and "nothing was sent" in out
    (home / "explorer" / "STOP").write_text("test")
    code, out = cli("explore", "run")
    assert code == 2 and "explore resume" in out


def test_run_refuses_without_the_sandbox(cli, home, monkeypatch):
    monkeypatch.setattr("ninshubur.explorer.sandbox.shutil.which", lambda name: None)
    cli("explore", "offer", "https://ex.org/a")
    code, out = cli("explore", "run")
    assert code == 2 and "install bubblewrap" in out and "nothing is fetched" in out
    code, out = cli("explore", "check")
    assert code == 2 and "install bubblewrap" in out


def test_sites_counts_every_rung(cli, home):
    cli("explore", "offer", "https://ex.org/a", "https://two.example/b")
    code, out = cli("explore", "sites")
    assert code == 0 and "candidate 2" in out and "admitted 0" in out and "ex.org" in out
    code, out = cli("explore", "sites", "--stage", "stopped")
    assert code == 0 and "ex.org" not in out


def test_inbox_dry_run_counts_and_the_real_read_stores_held(cli, home):
    from ninshubur.explorer.handoff import Writer
    from ninshubur.logs import JsonlLog
    Writer(home, JsonlLog(home / "x.jsonl"), now=lambda: NOW).write([
        {"type": "event", "name": "PyDay", "url": "", "start": "2027-05-01", "end": "", "place": "", "org": "",
         "site": "one.example", "page": "https://one.example/", "stage": "trial", "taint": []}])
    code, out = cli("explore", "inbox", "--dry-run")
    assert code == 0 and "1 would be stored" in out
    code, out = cli("explore", "inbox")
    assert code == 0 and "1 stored (held, Tier 3" in out
    code, out = cli("explore", "inbox")
    assert "0 lines" in out


# -- lane E2: the kill switch and status ---------------------------------------------------------------------------
def test_stop_resume_and_status_round_trip(cli, home):
    from ninshubur.logs import read_jsonl
    cli("explore", "offer", "https://ex.org/a")
    code, out = cli("explore", "status")
    assert code == 0 and "running (no STOP file)" in out and "candidate 1" in out and "in the last 24 h" in out
    code, out = cli("explore", "stop", "--reason", "odd traffic")
    assert code == 0 and "stopped: odd traffic" in out and "systemctl --user stop" in out
    code, out = cli("explore", "stop", "--reason", "second")
    assert "already stopped" in out and "odd traffic" in out
    code, out = cli("explore", "run")
    assert code == 2 and "by alex" in out and "odd traffic" in out
    code, out = cli("explore", "status")
    assert "stopped (by alex" in out
    code, out = cli("explore", "resume", "--dry-run")
    assert "would start it again" in out and (home / "explorer" / "STOP").exists()
    code, out = cli("explore", "resume")
    assert code == 0 and "resumed; it was stopped by alex: odd traffic" in out and not (home / "explorer" / "STOP").exists()
    code, out = cli("explore", "resume")
    assert "isn't stopped" in out
    events = [e["event"] for e in read_jsonl(home / "explorer" / "logs" / "explorer.jsonl")]
    assert events.count("stopped") == 1 and events.count("resumed") == 1


def test_run_refuses_when_tripped(cli, home):
    import json
    from ninshubur.explorer.killswitch import read_stop
    from ninshubur.explorer.tripwire import log_path
    from tests.conftest import NOW
    p = log_path(home)
    p.parent.mkdir(parents=True)
    p.write_text("".join(json.dumps({"event": "ssrf_attempt", "via": "connect", "site": f"s{i}.example",
                                     "at": NOW - 60 * i}) + "\n" for i in range(3)))
    cli("explore", "offer", "https://ex.org/a")
    code, out = cli("explore", "status")
    assert "3 of 3 attempts" in out
    code, out = cli("explore", "run")
    assert code == 2 and "tripwire: 3 attempts" in out and read_stop(home)["by"] == "tripwire"
