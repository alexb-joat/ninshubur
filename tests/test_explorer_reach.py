"""Reach adapters: logged links (no network)."""
from __future__ import annotations

import sqlite3

from ninshubur import db
from ninshubur.explorer import store
from ninshubur.explorer.reach import logged
from ninshubur.explorer.reach.run import Ctx, reach
from tests.conftest import _Clock, make_transport, public_resolver


def ctx(home, *, dry_run=False, routes=None, profiles=None, clock=None, seen=None, max_queries=4):
    from ninshubur.config import load_config
    from ninshubur.policy import Policy
    clock = clock or _Clock()
    policy = Policy.load(home)
    return Ctx(home=home, policy=policy, cfg=load_config(home, policy=policy), profiles=profiles or {},
               now=clock.now, dry_run=dry_run, max_queries=max_queries,
               transport=make_transport(routes or {}, seen), resolve=public_resolver, sleep=clock.sleep)


def _main_with_candidates(home, rows):
    conn = db.connect(home)
    for host, ex, src, reason in rows:
        conn.execute("INSERT INTO candidates(host, example_url, found_in_source, reason, first_seen, last_seen) "
                     "VALUES (?,?,?,?,1,1)", (host, ex, src, reason))
    conn.close()


def test_logged_links_use_example_on_host_else_root(home, now):
    _main_with_candidates(home, [
        ("conf.example.org", "https://conf.example.org/2027", "confs", "conference site"),
        ("sponsor.example.com", "https://events.example.net/sponsors", "event-sites", "linked from a sponsor section"),
        ("old.example.com", "http://old.example.com/page", "hn", "linked from HN story"),
        ("127.0.0.1", None, "hn", "x"),
    ])
    rep = reach(ctx(home), only="logged")
    assert vars(rep.added["logged_link"]) == {"new": 3, "seen_again": 0, "refused": 1}
    conn = store.connect(home)
    got = {r["url"]: r["via"] for r in conn.execute("SELECT url, via FROM explorer_candidates WHERE kind='logged_link'")}
    assert got == {"https://conf.example.org/2027": "confs", "https://sponsor.example.com/": "event-sites",
                   "https://old.example.com/": "hn"}
    again = reach(ctx(home), only="logged")
    assert (again.added["logged_link"].new, again.added["logged_link"].seen_again) == (0, 3)


def test_main_database_is_never_written(home, now):
    _main_with_candidates(home, [("a.example.org", None, "hn", "x")])
    main = logged.open_main_readonly(home)
    try:
        try:
            main.execute("UPDATE candidates SET status='x'")
            raise AssertionError("the read-only connection accepted a write")
        except sqlite3.OperationalError as e:
            assert "readonly" in str(e)
    finally:
        main.close()


def test_dry_run_writes_nothing(home, now):
    _main_with_candidates(home, [("a.example.org", None, "hn", "x")])
    rep = reach(ctx(home, dry_run=True), only="logged")
    assert rep.planned == [("logged", "logged hosts in the main database: 1 (no request)")]
    assert not (home / "explorer").exists()


def test_missing_main_database_names_the_fix(home, now):
    try:
        reach(ctx(home), only="logged")
    except FileNotFoundError as e:
        assert "ninshubur run" in str(e) and "NINSHUBUR_HOME" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_cli_reach_logged(home, capsys, monkeypatch):
    from ninshubur.__main__ import main
    from tests.conftest import write_config, write_profiles
    write_config(home)
    write_profiles(home)
    _main_with_candidates(home, [("a.example.org", None, "hn", "x")])
    assert main(["explore", "reach", "--only", "logged", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run: no request made, nothing written" in out and "logged hosts in the main database: 1" in out
    assert main(["explore", "reach", "--only", "logged"]) == 0
    assert "logged_link  new 1, seen again 0, refused 0" in capsys.readouterr().out


def test_cli_unknown_profile_lists_the_known_ones(home, capsys):
    from ninshubur.__main__ import main
    from tests.conftest import write_config, write_profiles
    write_config(home)
    write_profiles(home)
    assert main(["explore", "reach", "--profile", "nope", "--dry-run"]) == 2
    assert "no profile 'nope' in profiles.yaml; have:" in capsys.readouterr().err
