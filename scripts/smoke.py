"""Smoke test: a temporary home, the shipped samples replayed through the real pipeline, then
the CLI round trip (check, run --replay, status, suggest, show, accept, done, budget). No network."""
from __future__ import annotations

import os
import subprocess  # noqa: S404 - smoke runner only, never imported by the package
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"


def run(home: Path, *args: str, expect: int = 0) -> str:
    env = {**os.environ, "NINSHUBUR_HOME": str(home), "PYTHONPATH": str(ROOT)}
    p = subprocess.run([sys.executable, "-m", "ninshubur", *args], env=env, capture_output=True, text=True, cwd=ROOT)
    print(f"$ ninshubur {' '.join(args)}\n{p.stdout}{p.stderr}")
    if p.returncode != expect:
        raise SystemExit(f"exit {p.returncode}, expected {expect}")
    return p.stdout


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        (home / "config.yaml").write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        (home / "profiles.yaml").write_text((ROOT / "profiles.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        run(home, "check")
        out = run(home, "run", "--replay", str(SAMPLES))
        assert "suggestions:" in out
        run(home, "status")
        run(home, "sources", "list")
        run(home, "sources", "candidates")
        out = run(home, "suggest", "--top", "5")
        first = next((line for line in out.splitlines() if line.startswith("#")), None)
        assert first, "no suggestions produced"
        sid = first[1:].split(" ", 1)[0]
        run(home, "show", sid)
        run(home, "accept", sid)
        assert (home / "feeds" / "timings.ics").read_text().startswith("BEGIN:VCALENDAR")
        run(home, "done", sid, "--outcome", "meeting", "--minutes", "15")
        run(home, "budget")
        run(home, "suggest", "--held")
        run(home, "suggest", "--plan", "45m")
        # second run: nothing due, no network
        out = run(home, "run", "--replay", str(SAMPLES))
        assert "nothing due" in out
        for f in ("logs/fetch.jsonl", "logs/policy.jsonl", "ninshubur.db"):
            assert (home / f).exists(), f
        if os.name == "posix":
            import stat
            for f in ("logs/fetch.jsonl", "ninshubur.db", "feeds/timings.ics"):
                assert stat.S_IMODE((home / f).stat().st_mode) == 0o600, f
    print("smoke ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
