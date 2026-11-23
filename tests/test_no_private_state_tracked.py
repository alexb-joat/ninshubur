"""Guard: private runtime state must never be committed to this repository."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".key", ".pem")
FORBIDDEN_NAMES = ("owner.key", "password", "plan-salt", ".env", ".netrc", ".npmrc")


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def test_no_private_state_tracked():
    bad = [
        f
        for f in _tracked_files()
        if f.endswith(FORBIDDEN_SUFFIXES) or f.rsplit("/", 1)[-1] in FORBIDDEN_NAMES
    ]
    assert not bad, f"private/secret-looking files are tracked: {bad}"
