"""README's "What it looks like" block is captured by scripts/readme_sample.py from the replay of the shipped samples:
every command shown is one the CLI accepts, the note says where the output came from, and the script that rewrites
the block works. The test does not compare the output's numbers: they move with the clock the replay runs under."""
from __future__ import annotations

import importlib.util
import re
import shlex
from datetime import date, timedelta
from pathlib import Path

import pytest

from ninshubur.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("readme_sample", ROOT / "scripts" / "readme_sample.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

README = (ROOT / "README.md").read_text(encoding="utf-8")


def readme_block() -> str:
    assert README.count(rs.START) == 1 and README.count(rs.END) == 1
    return README[README.index(rs.START):README.index(rs.END) + len(rs.END)]


def accepted(command: str) -> bool:
    argv = shlex.split(command)
    assert argv[0] == "ninshubur", command
    try:
        build_parser().parse_args(argv[1:])
    except SystemExit:
        return False
    return True


def test_the_block_is_under_its_own_heading_before_the_three_rules():
    heads = [x for x in README.splitlines() if x.startswith("## ")]
    assert heads[0] == "## What it looks like" and heads[1] == "## Three rules"
    assert README.index("## What it looks like") < README.index(rs.START) < README.index("## Three rules")


def test_every_command_in_the_block_is_accepted_by_the_cli_parser():
    block = readme_block()
    shown = re.findall(r"^\$ (ninshubur .+)$", block, re.M)
    assert shown == ["ninshubur " + " ".join(a) for a in rs.SHOWN] and len(shown) == 2
    for command in shown:
        assert accepted(command), command
    # the replay command named in the note, too
    replay = re.search(r"`(ninshubur run --replay [^`]+)`", block)
    assert replay and accepted(replay.group(1))
    assert not accepted("ninshubur nonsense --top 1") and not accepted("ninshubur suggest --top")  # the check can fail


def test_the_capture_note_says_replay_not_live_and_gives_a_real_date():
    block = readme_block()
    m = re.search(r"Captured (\d{4}-\d{2}-\d{2}) from the replay of `tests/samples` .*not from live data", block)
    assert m, "the note under the block must say it comes from the replay of tests/samples, not from live data"
    assert date.fromisoformat(m.group(1)) <= date.today() + timedelta(days=1)  # the note's date is UTC


def test_the_block_holds_output_only_and_no_local_path_or_error():
    body = re.search(r"```console\n(.*?)\n```", readme_block(), re.S).group(1)
    for bad in ("Traceback", "/home/", "/tmp/", "/Users/", "C:\\", "refusing"):
        assert bad not in body
    assert body.startswith("$ ninshubur status\n") and "\n\n$ ninshubur suggest --top 1\n" in body
    assert all(x == x.rstrip() for x in body.splitlines())  # trailing spaces trimmed by the script


def test_update_replaces_only_the_block():
    text = f"before\n{rs.START}\nold\n{rs.END}\nafter <!-- /gen --> still after\n"
    new = f"{rs.START}\nnew\n{rs.END}"
    assert rs.update(text, new) == f"before\n{new}\nafter <!-- /gen --> still after\n"
    with pytest.raises(SystemExit):
        rs.update("no markers here", new)
    with pytest.raises(SystemExit):
        rs.update(f"{rs.START}\n{rs.START}\n{rs.END}", new)


def test_the_script_captures_a_real_replay_in_a_temporary_home(tmp_path):
    out = rs.block(today="2026-01-02")
    assert out.startswith(rs.START) and out.endswith(rs.END)
    assert "$ ninshubur status\nsources: " in out and "\n\n$ ninshubur suggest --top 1\n" in out
    assert "Captured 2026-01-02 from the replay of `tests/samples`" in out and "not from live data" in out
    assert str(tmp_path) not in out and str(Path.home()) not in out
