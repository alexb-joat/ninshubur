"""Environment helpers. Secrets live only in env vars named by config."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def load_dotenv(path: Path = Path(".env")) -> int:
    """Load KEY=VALUE lines; never override variables already set. Returns count loaded."""
    if not path.is_file():
        return 0
    if os.name == "posix":
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            print(f"warning: {path} is readable by others; run: chmod 600 {path}", file=sys.stderr)
    n = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n
