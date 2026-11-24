"""The explorer is Linux only. This file runs on every system: it stands on another one by changing
``ninshubur.explorer.PLATFORM`` and checks that every ``explore`` command refuses there, with the reason, before it
touches anything. (tests/test_explorer_*.py are skipped off Linux; see conftest.py.)"""
from __future__ import annotations

import pytest

from ninshubur.__main__ import build_parser, main
from ninshubur.explorer.sandbox import Sandbox, SandboxError
from tests.conftest import NOW, write_config, write_profiles

COMMANDS = {  # every explore subcommand, with the least it needs to parse
    "candidates": [], "reach": ["--dry-run"], "offer": ["https://ex.org/a"], "queue": [], "frontier": [], "step": [],
    "check": [], "run": [], "sites": [], "inbox": [], "stop": [], "resume": [], "status": [],
}


@pytest.fixture
def elsewhere(home, monkeypatch):
    monkeypatch.setattr("ninshubur.explorer.PLATFORM", "win32")
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    write_config(home)
    write_profiles(home)
    return home


def test_the_list_above_is_every_explore_command():
    explore = next(a for a in build_parser()._actions if getattr(a, "choices", None) and "explore" in a.choices)
    sub = next(a for a in explore.choices["explore"]._actions if getattr(a, "choices", None))
    assert set(sub.choices) == set(COMMANDS)


@pytest.mark.parametrize("cmd", sorted(COMMANDS))
def test_off_linux_every_explore_command_says_so_and_touches_nothing(elsewhere, capsys, cmd):
    code = main(["explore", cmd, *COMMANDS[cmd]])
    err = capsys.readouterr().err
    assert code == 2 and f"explore {cmd}: the explorer is Linux only" in err and "this is win32" in err
    assert not (elsewhere / "explorer").exists()  # no database, no STOP file, no log


def test_off_linux_the_sandbox_parses_nothing(elsewhere):
    sb = Sandbox(bwrap="/usr/bin/bwrap")
    assert "Linux only" in sb.unavailable()
    with pytest.raises(SandboxError) as e:
        sb.parse(b"<p>x</p>", "https://ex.org/", "text/html")
    assert e.value.code == "sandbox_unavailable"


def test_the_rest_of_ninshubur_still_runs_there(elsewhere, capsys):
    assert main(["status"]) == 0
