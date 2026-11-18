"""Regexes that run on crawled or model text stay linear on hostile input (ReDoS audit, 17 Nov 2026).

Each case is 40k characters. Before the audit these took 5 to 70 seconds; now each takes well under
a second, so the 2-second bound only trips on a quadratic regression."""
import time

import pytest

from ninshubur import detectors, redact
from ninshubur.analyze import qualify
from ninshubur.explorer import traps
from ninshubur.parse import people

N = 40_000
BOUND_S = 2.0


def _timed(fn, arg):
    t0 = time.perf_counter()
    fn(arg)
    return time.perf_counter() - t0


@pytest.mark.parametrize("name,fn,text", [
    ("email run", lambda s: redact.redact(s), "a" * (N // 2) + "@" + "b" * (N // 2)),
    ("spelt dotted run", lambda s: redact.redact(s), "a." * (N // 2) + "@"),
    ("spelt hyphen run", lambda s: redact.redact(s), "x at " + "b-" * (N // 2)),
    ("people email count", people._EMAIL.findall, "a" * (N // 2) + "@" + "b" * (N // 2)),
    ("blank lines", detectors.scan_text, " \n" * (N // 2)),
    ("blank run in reply", qualify.parse_reply, "{" + " " * (3 * N) + "x}"),
    ("explorer date tokens", lambda s: traps._DATE_TOKEN.findall(s), "2026-" * N),
    ("explorer path session", lambda s: traps._PATH_SESSION.sub("", s), ";jsessionid=" * (N // 4)),
    ("explorer unclosed tags", lambda s: traps.simhash(s.encode()), "<" * (5 * N)),
    ("explorer unclosed scripts", lambda s: traps.simhash(s.encode()), "<script>" * N),
    ("explorer canonical", lambda s: traps.canonical("https://ex.org/" + s), "a=" * N),
], ids=lambda v: v if isinstance(v, str) and len(v) < 30 else "")
def test_linear_on_hostile_input(name, fn, text):
    assert _timed(fn, text) < BOUND_S, name


def test_rewrites_keep_what_they_match():
    r = redact.redact("mail john.doe@example.co.uk or jane [at] example [dot] org, or bob at mail dot com")
    assert "john.doe" not in r.text and "jane" not in r.text and "bob" not in r.text
    assert people._EMAIL.findall("a@b.cd and x.y+z@q-r.st") == ["a@b.cd", "x.y+z@q-r.st"]
    assert {f.code for f in detectors.scan_text("hello\n\n   \t assistant : do this")} >= {"prompt_injection"}
    assert not detectors.scan_text("the system administrator: Ana")
    assert qualify._FENCE.sub("", "```json\n{}\n```") == "{}"
    assert qualify._FENCE.sub("", "```\n```") == ""
    assert qualify._FENCE.sub("", "{} \n ```") == "{}"
