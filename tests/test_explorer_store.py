"""The explorer's own store: a separate 0600 file, candidates deduplicated by URL, bad URLs dropped."""
from __future__ import annotations

import stat

import pytest

from ninshubur.explorer import store
from ninshubur.explorer.store import Candidate


def test_own_private_file_with_main_schema(home):
    conn = store.connect(home)
    path = home / "explorer" / "explorer.db"
    assert path.exists() and not (home / "ninshubur.db").exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "explorer").stat().st_mode) == 0o700
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"explorer_candidates", "fetch_state", "budget_ledger", "robots"} <= tables  # the Fetcher can run on it


def test_dedup_by_url_and_refuse_bad_urls(home, now):
    conn = store.connect(home)
    a = Candidate("https://example.org/a", "hn", "hn-algolia", profile="p", query="topic:ai")
    got = store.add_candidates(conn, [a, a, Candidate("http://example.org/", "hn", "x"),
                                      Candidate("https://127.0.0.1/", "hn", "x"),
                                      Candidate("https://example.org:22/", "hn", "x")], now)
    assert (got.new, got.seen_again, got.refused) == (1, 1, 3)
    r = conn.execute("SELECT host, count, status FROM explorer_candidates").fetchone()
    assert (r["host"], r["count"], r["status"]) == ("example.org", 2, "candidate")


def test_unknown_kind_and_status_fail_loudly(home, now):
    conn = store.connect(home)
    with pytest.raises(ValueError, match="unknown candidate kind"):
        store.add_candidates(conn, [Candidate("https://example.org/", "crawl", "x")], now)
    with pytest.raises(ValueError, match="unknown status"):
        store.set_status(conn, 1, "great", "", now)
