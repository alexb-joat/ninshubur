"""Where Ninshubur keeps its state: $NINSHUBUR_HOME, else ~/.ninshubur."""
from __future__ import annotations

import os
from pathlib import Path

SUBDIRS = ("logs", "feeds")


def data_home() -> Path:
    raw = os.environ.get("NINSHUBUR_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".ninshubur"


def ensure_home(home: Path | None = None) -> Path:
    home = home or data_home()
    for d in (home, *(home / s for s in SUBDIRS)):
        d.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
    return home
