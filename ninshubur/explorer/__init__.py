"""The explorer: Ninshubur's open-web discovery, fenced off from the main crawler.

Reach adapters turn what Ninshubur already knows (logged links, profile topics, job-board tokens) into
*candidates*. The frontier decides which candidates are worth a request and refuses traps; the gate fetches
through the shared hardened ``Fetcher``. The explorer keeps its own database (``~/.ninshubur/explorer/``), so it
never contends with the main crawler's locks, and nothing it learns moves trust in the main database.

Linux only: the parser runs in bubblewrap, the STOP and hand-off files are opened with Linux file flags, and the
timer is a systemd unit. On any other system every ``explore`` command says so and does nothing; the rest of
Ninshubur runs there as before.
"""
from __future__ import annotations

import sys

PLATFORM = sys.platform  # read through this name so a test can stand on another system


def linux_only() -> str | None:
    """Why the explorer can't run on this system; None on Linux."""
    if PLATFORM.startswith("linux"):
        return None
    return ("the explorer is Linux only (its parser runs in bubblewrap and its stop and hand-off files use Linux "
            f"file flags); this is {PLATFORM}")
