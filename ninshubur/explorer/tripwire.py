"""The explorer's tripwire: three attempts to reach a denied address in 24 hours stop the whole explorer.

An *attempt* is a request the explorer was about to make that would have reached this machine or its network:
a site whose name resolves to a private or reserved address (refused at connect time, ``via: "connect"``), or a
redirect that points at one, or at a port other than 443 (``via: "redirect"``). Each attempt also stops the site that
made it. A redirect to another scheme (``ftp:``, an app link) is refused without counting, unless it names an IP
address or localhost. Links a page
merely *mentions* (``http://localhost:8000`` in a tutorial) are refused by the frontier and logged as
``ssrf_attempt`` with ``via: "link"``, but they don't count here: nothing was asked for, and counting them would
let any blog post pause the explorer.

The count covers events since the latest of ``tripwire_window_s`` ago, the last ``resume`` and the tripwire's own
last stop, so starting again starts from zero, whether by ``explore resume`` or by deleting the STOP file by hand
(the kill switch allows both): the attempts that tripped it once don't trip it again. When the count reaches
``tripwire_ssrf`` the tripwire writes the STOP file (``by: "tripwire"``). It fails closed: a log that exists but
can't be read refuses the run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .killswitch import stop
from .store import explorer_home

STRONG = frozenset({"connect", "redirect"})
TAIL_BYTES = 8_000_000  # the log's last 8 MB: far more than a day of events at the explorer's page caps


@dataclass
class Count:
    n: int = 0
    since: float = 0.0
    error: str | None = None
    last: list[dict] | None = None


def log_path(home: Path) -> Path:
    return explorer_home(home) / "logs" / "explorer.jsonl"


def count(home: Path, *, now: float, window_s: float) -> Count:
    """Strong ``ssrf_attempt`` events since the latest of ``now - window_s``, the last ``resumed`` and the tripwire's
    last ``stopped``."""
    path = log_path(home)
    since = now - window_s
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return Count(since=since)
    except OSError as e:
        return Count(since=since, error=f"can't read {path}: {e.strerror or e}")
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - TAIL_BYTES)
        raw = os.pread(fd, size - start, start)
    except OSError as e:
        return Count(since=since, error=f"can't read {path}: {e.strerror or e}")
    finally:
        os.close(fd)
    hits: list[dict] = []
    for line in raw.split(b"\n")[1 if start else 0:]:  # the first line of a tail may be cut
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or type(e.get("at")) not in (int, float):
            continue
        if e.get("event") == "resumed" and e["at"] > since:
            since, hits = e["at"], [h for h in hits if h["at"] >= e["at"]]
        elif e.get("event") == "stopped" and e.get("by") == "tripwire":
            since, hits = max(since, e["at"]), []  # these attempts have had their stop; the log's order says which
        elif e.get("event") == "ssrf_attempt" and e.get("via") in STRONG and e["at"] >= since:
            hits.append(e)
    return Count(n=len(hits), since=since, last=hits[-3:])


def check(home: Path, limits, *, now: float) -> str | None:
    """None when the explorer may run; otherwise why not (and, at the threshold, the STOP file is written)."""
    c = count(home, now=now, window_s=limits.tripwire_window_s)
    if c.error:
        return f"tripwire can't count ({c.error}); refusing to run"
    if c.n >= limits.tripwire_ssrf:
        hosts = ", ".join(dict.fromkeys(str(h.get("site") or h.get("url", ""))[:80] for h in c.last or []))
        reason = f"tripwire: {c.n} attempts to reach denied addresses in {limits.tripwire_window_s / 3600:g} h ({hosts})"
        stop(home, reason, by="tripwire", now=now)
        return reason
    return None
