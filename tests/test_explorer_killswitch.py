"""E2: the kill switch. The STOP file's existence is the switch, whatever it holds; the gate refuses every request
while it exists, a running cycle halts at its next request, and only ``resume`` (or deleting the file) undoes it."""
from __future__ import annotations

import json
import os

from ninshubur.explorer.killswitch import read_stop, resume, stop, stop_path
from ninshubur.logs import read_jsonl


def test_any_content_stops_and_the_note_is_bounded(home):
    assert read_stop(home) is None
    p = stop_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    for raw in (b"", b"\xff\xfe not json", b"[1,2]", json.dumps({"by": "x" * 500, "reason": "y" * 5000}).encode()):
        p.write_bytes(raw)
        note = read_stop(home)
        assert note is not None and len(note["by"]) <= 40 and len(note["reason"]) <= 300


def test_stop_is_atomic_private_and_keeps_the_first_note(home):
    first = stop(home, "first", by="tripwire", now=100.0)
    assert stop(home, "second", now=200.0) == read_stop(home) == first
    assert os.stat(stop_path(home)).st_mode & 0o777 == 0o600
    assert not [p for p in stop_path(home).parent.iterdir() if p.name.startswith(".STOP")]
    assert resume(home, now=300.0)["by"] == "tripwire" and read_stop(home) is None and resume(home) is None
    log = read_jsonl(stop_path(home).parent / "logs" / "explorer.jsonl")
    assert [e["event"] for e in log] == ["stopped", "resumed"] and log[1]["stopped_by"] == "tripwire"
