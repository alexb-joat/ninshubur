from __future__ import annotations

import json
from pathlib import Path

import pytest

from ninshubur.__main__ import main
from tests.conftest import NOW, SAMPLES, write_config, write_profiles

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli(home, capsys, monkeypatch):
    # The replay samples are dated around 2026-10-12; pin the app's clock there so the test does not age.
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    write_config(home)
    write_profiles(home)

    def call(*args: str) -> tuple[int, str]:
        code = main(list(args))
        out = capsys.readouterr()
        return code, out.out + out.err

    return call


def test_check_and_replay_round_trip(cli, home):
    code, out = cli("check")
    assert code == 0 and "ok" in out.splitlines()[-1] and "confs" in out
    code, out = cli("run", "--replay", str(SAMPLES))
    assert code == 0 and "suggestions:" in out and "hn" in out
    code, out = cli("status")
    assert code == 0 and "suspect" in out and "quarantine rows" in out
    code, out = cli("suggest", "--top", "3")
    assert code == 0 and out.startswith("#") and "[" in out
    sid = out.split()[0][1:]
    code, out = cli("show", sid)
    assert code == 0 and "evidence:" in out and "steps:" in out
    code, out = cli("accept", sid)
    assert code == 0 and "accepted" in out and (home / "feeds" / "timings.ics").exists()
    code, out = cli("done", sid, "--outcome", "reply", "--minutes", "10")
    assert code == 0
    code, out = cli("suggest", "--held")
    assert code == 0 and "held:" in out
    code, out = cli("suggest", "--plan", "30m")
    assert code == 0 and "plan for 30 min" in out
    code, out = cli("suggest", "--json")
    assert code == 0 and out.strip().startswith("[")
    code, out = cli("budget")
    assert code == 0 and "bytes today" in out
    code, out = cli("sources", "candidates")
    assert code == 0 and "pycon.pt" in out
    code, out = cli("sources", "candidates", "--reject-host", "pycon.pt")
    assert code == 0 and "rejected 1" in out
    code, out = cli("sources", "candidates")
    assert "pycon.pt" not in out
    code, out = cli("profiles", "check")
    assert code == 0 and "conference_prospecting" in out and "ok" in out
    for fmt, marker in (("md", "# Ninshubur digest"), ("html", "<!doctype html>"), ("csv", "id,kind,profile")):
        code, out = cli("export", "digest", "--format", fmt, "--profile", "agency-eu")
        assert code == 0 and "written" in out
        path = Path(out.split("written ", 1)[1].splitlines()[0].strip())
        assert path.read_text(encoding="utf-8-sig").startswith(marker), fmt
    code, out = cli("export", "prospects")
    assert code == 0 and "written" in out
    ppath = Path(out.split("written ", 1)[1].splitlines()[0].strip())
    ptext = ppath.read_text(encoding="utf-8-sig")
    assert ptext.splitlines()[0] == "event,event_date,role,name,title,company,domain,handle,source_url,fetched_at,seen_events"
    assert "Jane Doe" in ptext and "https://www.linkedin.com/in/janedoe" in ptext and "Orbit Systems" in ptext
    code, out = cli("export", "prospects", "--event", "nope-uid")
    assert code == 0 and Path(out.split("written ", 1)[1].splitlines()[0].strip()).read_text(encoding="utf-8-sig").count("\n") == 1
    code, out = cli("suggest", "--kind", "conference_prospecting", "--top", "1")
    assert code == 0 and out.startswith("#")
    pid = out.split()[0][1:]
    code, out = cli("show", pid)
    assert code == 0 and "prospects (" in out and "unverified" in out and "export prospects --event" in out
    code, out = cli("lookup", "PyCon", "--dry-run", "--replay", str(SAMPLES))
    assert code == 0 and out.startswith("dry run: PyCon Portugal") and "people" in out and "Jane Doe" in out
    code, out = cli("lookup", "#" + pid, "--replay", str(SAMPLES))
    assert code == 0 and "suggestions rebuilt" in out
    code, out = cli("lookup", "no-such-event-zzz")
    assert code == 2
    code, out = cli("sources", "review", "event-sites")
    assert code == 0
    code, out = cli("sources", "review", "event-sites", "--clear-event", "nope")
    assert code == 0 and "no per-event state" in out
    code, out = cli("export", "items", "--format", "md")
    assert code == 0  # items/prospects default to csv
    code, out = cli("export", "digest", "--profile", "nope")
    assert code == 2
    code, out = cli("sources", "review", "hn")
    assert code == 0 and "prompt_injection" in out
    code, out = cli("sources", "review", "hn", "--clear")
    assert code == 0 and "cleared: health ok" in out
    code, out = cli("sources", "disable", "hn")
    assert code == 0
    code, out = cli("sources", "list")
    assert code == 0 and "paused" in out


def test_resuming_a_quarantined_source_keeps_the_quarantine(cli, home):
    from ninshubur.db import connect
    cli("status")
    conn = connect(home)
    conn.execute("UPDATE sources SET health='quarantined', health_reason='4 taint events in 24h' WHERE name='event-sites'")
    conn.commit()
    code, out = cli("sources", "disable", "event-sites")
    assert code == 0 and "still quarantined (4 taint events in 24h)" in out
    code, out = cli("status")
    assert "quarantined (paused)" in out
    code, out = cli("sources", "enable", "event-sites")
    assert code == 0 and "until `ninshubur sources review event-sites --clear`" in out
    code, out = cli("status")
    assert "event-sites" in out and "quarantined" in out and "(paused)" not in out


def test_sources_test_refuses_bad_source(cli, home, monkeypatch):
    """A source whose trust block fails hard criteria is refused before any fetch."""
    import yaml
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    cfg["sources"]["confs"]["trust"]["reviewed_at"] = "2026-01-01"  # review overdue
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    code, out = cli("sources", "test", "confs")
    assert code == 1 and "H8 FAIL" in out and "REFUSED" in out
    code, out = cli("sources", "add", "evil", "--adapter", "statuspage", "--param", "host=status.evil.example", "--tier", "0",
                    "--owner", "x", "--owner-url", "https://x.example", "--terms-url", "https://x.example/t", "--licence", "none",
                    "--rate-limit", "1/s", "--reviewed-by", "alex")
    assert code == 1 and "REFUSED" in out
    assert "evil" not in (home / "config.yaml").read_text()


def test_fails_closed_on_bad_policy(cli, home):
    (home / "policy.yaml").write_text("fetch: [broken")
    code, out = cli("check")
    assert code == 3 and "refusing to run" in out


def test_package_has_no_subprocess_eval_pickle():
    """One exception: explorer/sandbox.py starts bwrap (the parser sandbox) and, for `explore check`, one plain
    Python probe; tests/test_explorer_sandbox.py pins down exactly what it may start."""
    import ninshubur
    root = Path(ninshubur.__file__).parent
    for py in root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "pickle" not in text and "eval(" not in text, py
        if py.relative_to(root).as_posix() != "explorer/sandbox.py":
            assert "subprocess" not in text, py


def test_export_euboulia_facts(cli, home):
    code, out = cli("export", "euboulia-facts")
    assert code == 0 and "provider-facts.json" in out
    data = json.loads((home / "feeds" / "provider-facts.json").read_text())
    assert data["$schema"] == "provider-facts.v1" and data["providers"] == {}


def test_crawled_text_reaches_the_terminal_without_control_sequences(cli, home):
    """Even text that skipped ingest cleaning (written straight into the database here) prints with its escape
    sequences defused: no clipboard write (OSC 52), no planted link (OSC 8), no screen clear, no bidi override."""
    from ninshubur.db import connect
    assert cli("run", "--replay", str(SAMPLES))[0] == 0
    hostile = "\x1b]52;c;ZXZpbA==\x07\x1b]8;;https://evil.example\x1b\\click\x1b]8;;\x1b\\\x1b[2J\x9b31m\u202eevil"
    conn = connect(home)
    sid = conn.execute("SELECT id FROM suggestions ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE suggestions SET title = title || ? WHERE id = ?", (hostile, sid))
    conn.commit()
    conn.close()
    for args in (("suggest",), ("show", str(sid))):
        code, out = cli(*args)
        assert code == 0 and "click" in out
        assert not any(c in out for c in ("\x1b", "\x07", "\x9b", "\u202e")), args


def test_term_safe_keeps_ordinary_text():
    from ninshubur.termsafe import term_safe
    assert term_safe("Konferenz 2026\tBerlin\n\u2764 caf\u00e9") == "Konferenz 2026\tBerlin\n\u2764 caf\u00e9"
    assert term_safe("a\rb\x1b[Kc") == "a?b?[Kc"
