#!/usr/bin/env python3
"""The "What it looks like" block of README.md, captured from a real run, never typed.

    python scripts/readme_sample.py            # print the block
    python scripts/readme_sample.py --update   # rewrite it in README.md, between its two marker comments

It makes a temporary home with the shipped config and profile examples, runs ``ninshubur run --replay tests/samples``
(the replay transport: no network, the shipped samples), then captures what ``ninshubur status`` and
``ninshubur suggest --top 1`` print and puts that output, unedited, in a code block. The note under it says the block
comes from the replay of the samples, not from live data, and on what day. Nothing from a real data home is read:
the environment the commands see has no NINSHUBUR_* variable but the temporary home.
"""
from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 - a script, never imported by the package
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SAMPLES = ROOT / "tests" / "samples"
START, END = "<!-- gen:readme-sample -->", "<!-- /gen -->"
SHOWN = (("status",), ("suggest", "--top", "1"))  # the commands whose output the block shows
FENCE = "`" * 3


def cli(home: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NINSHUBUR_")}
    env.update(NINSHUBUR_HOME=str(home), PYTHONPATH=str(ROOT))
    p = subprocess.run([sys.executable, "-m", "ninshubur", *args], env=env, capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        raise SystemExit(f"ninshubur {' '.join(args)} exited {p.returncode}:\n{p.stdout}{p.stderr}")
    return p.stdout


def capture() -> tuple[str, list[str]]:
    """The commands' output as printed, joined as a terminal session; the temporary home is gone afterwards."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        for name in ("config", "profiles"):
            (home / f"{name}.yaml").write_text((ROOT / f"{name}.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        cli(home, "run", "--replay", str(SAMPLES))
        parts, shown = [], []
        for args in SHOWN:
            out = cli(home, *args)
            for private in (tmp, str(Path.home())):
                if private in out:
                    raise SystemExit(f"refusing: the output of `ninshubur {' '.join(args)}` contains a local path")
            parts.append(f"$ ninshubur {' '.join(args)}\n" + "\n".join(x.rstrip() for x in out.rstrip("\n").splitlines()))
            shown.append("ninshubur " + " ".join(args))
    return "\n\n".join(parts), shown


def block(today: str | None = None) -> str:
    body, _ = capture()
    day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    return (f"{START}\n{FENCE}console\n{body}\n{FENCE}\n\n"
            f"<sub>Captured {day} from the replay of `tests/samples` (`ninshubur run --replay tests/samples` in a temporary home), "
            f"not from live data. Rebuild it with `python scripts/readme_sample.py --update`.</sub>\n{END}")


def update(text: str, new: str) -> str:
    if text.count(START) != 1 or text.count(END) < 1:
        raise SystemExit(f"README.md needs exactly one {START} ... {END} block")
    a = text.index(START)
    b = text.index(END, a) + len(END)
    return text[:a] + new + text[b:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="rewrite the block in README.md (default: print it)")
    args = ap.parse_args(argv)
    new = block()
    if not args.update:
        print(new)
        return 0
    text = README.read_text(encoding="utf-8")
    README.write_text(update(text, new), encoding="utf-8", newline="\n")
    print(f"README.md: block rewritten ({len(new.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
