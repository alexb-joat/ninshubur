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
    assert code == 2 and "explorer is stopped" in out and "remove it to resume" in out


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
