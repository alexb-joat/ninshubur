"""The explorer's kill switch: a ``STOP`` file in its folder, outside every other part of the app.

The gate checks the file before *every* request (robots.txt, probes and redirect hops included), so a cycle that
is running stops at its next request, and nothing starts while the file exists. It works even if something else
in the app is compromised: creating a file is all it takes to stop, and only ``resume`` (or deleting the file by
hand) starts the explorer again. The second layer is the service manager: ``systemctl --user stop
ninshubur-explorer.timer ninshubur-explorer.service``.

The file holds a small JSON note (who stopped it, when, why). A file with any other content still stops the
explorer: whatever is in it, its existence is the switch.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..logs import JsonlLog
from .store import explorer_home


def stop_path(home: Path) -> Path:
    return explorer_home(home) / "STOP"


def _log(home: Path) -> JsonlLog:
    return JsonlLog(explorer_home(home) / "logs" / "explorer.jsonl")


def read_stop(home: Path) -> dict | None:
    """What the STOP file says, or None when the explorer isn't stopped."""
    p = stop_path(home)
    if not p.exists():
        return None
    try:
        raw = p.read_bytes()[:4096]
        note = json.loads(raw)
        if isinstance(note, dict):
            return {"by": str(note.get("by", "?"))[:40], "at": note.get("at"), "reason": str(note.get("reason", ""))[:300]}
    except (OSError, ValueError):
        pass
    return {"by": "?", "at": None, "reason": "STOP file present (not written by `explore stop`)"}


def stop(home: Path, reason: str, *, by: str = "alex", now: float | None = None) -> dict:
    """Write the STOP file (atomically, 0600) and log it. Stopping a stopped explorer keeps the first note."""
    current = read_stop(home)
    if current:
        return current
    note = {"by": by, "at": round(now if now is not None else time.time(), 3), "reason": reason[:300]}
    p = stop_path(home)
    tmp = p.with_name(f".STOP.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(note, f)
    os.replace(tmp, p)
    _log(home).write({"event": "stopped", **note})
    return note


def resume(home: Path, *, now: float | None = None) -> dict | None:
    """Remove the STOP file; return the note it held (None if the explorer wasn't stopped)."""
    note = read_stop(home)
    if note is None:
        return None
    stop_path(home).unlink(missing_ok=True)
    _log(home).write({"event": "resumed", "at": round(now if now is not None else time.time(), 3),
                      "was": note["reason"][:200], "stopped_by": note["by"]})
    return note
