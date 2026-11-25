#!/usr/bin/env python3
"""Fresh-clone rehearsal: what a stranger gets from the committed repository, install path by install path.

    python scripts/fresh_clone.py [--ref REF] [--dry-run] [--keep] [--workdir DIR]

For each documented way to install (README's quick start, CONTRIBUTING's setup and lint install, and the requirements file the
repository ships) it clones the repository's committed HEAD (or REF) with ``git clone --no-hardlinks`` of the local path into a new
empty folder, so an uncommitted or ignored file cannot sneak in, makes a virtual environment there, runs the install
command, and prints one result line. In the CONTRIBUTING clone it then runs the checks CONTRIBUTING and CI name: the test
suite, ``scripts/smoke.py`` and ``python -m ninshubur check`` on ``config.example.yaml``; in the lint clone it installs the
hash-pinned ruff and runs ``ruff check .``, as CI's lint job does. Exit status 0 only if every line passed. The test
suite's line carries pytest's whole final tally (passed, skipped, deselected, xfailed, failed...) and, under it, the
reason for each skipped test, so a count that differs from another run says why.

It reads from PyPI (pip) and nothing else. After the install it runs offline commands only: never ``sources test``, a
run that is not a replay, ``qualify`` or ``explore``. The commands that fetch from the internet, ``ninshubur sources test``
and ``ninshubur run``, are in the README's quick start and are *not* rehearsed. A throwaway data home inside the work
folder keeps ``~/.ninshubur`` out of it, and no environment variable is printed.

``--dry-run`` prints the plan and runs nothing. ``--keep`` leaves the work folder (it is deleted otherwise).
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess  # noqa: S404 - a script, never imported by the package
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Check:
    name: str
    command: str                     # as written in the docs; `python` and `ruff` are the clone's venv programs
    env: tuple[tuple[str, str], ...] = ()
    timeout: int = 1800


@dataclass(frozen=True)
class InstallPath:
    key: str
    label: str
    install: str                     # as written in the docs: `pip install ...`
    checks: tuple[Check, ...] = ()


# The install commands are the ones written in README.md (quick start) and CONTRIBUTING.md (set up, lint); tests/test_fresh_clone.py
# fails if either document, or the CI workflow, says something else.
PATHS: tuple[InstallPath, ...] = (
    InstallPath("readme", "README quick start", "pip install -e ."),
    InstallPath("contributing", "CONTRIBUTING set up", 'pip install -e ".[dev]"', checks=(
        # -rs through the environment, so the command stays the documented one and pytest still names each skip
        Check("pytest", "python -m pytest -q", env=(("PYTEST_ADDOPTS", "-rs"),)),
        Check("smoke", "python scripts/smoke.py"),
        Check("check", "python -m ninshubur check", env=(("NINSHUBUR_CONFIG", "config.example.yaml"),)),
    )),
    InstallPath("requirements-dev", "requirements-dev.in", "pip install -r requirements-dev.in"),
    InstallPath("lint", "CONTRIBUTING lint check", "pip install --require-hashes --no-deps -r requirements-lint.txt", checks=(
        Check("ruff", "ruff check ."),
    )),
)


def venv_python(venv: Path) -> Path:
    return venv_tool(venv, "python")


def venv_tool(venv: Path, name: str) -> Path:
    """A program the venv provides: its python, or a console script such as ruff."""
    return venv / (f"Scripts/{name}.exe" if os.name == "nt" else f"bin/{name}")


def render_plan(paths: tuple[InstallPath, ...] = PATHS) -> str:
    out = ["fresh_clone plan (nothing is run)",
           "ref: the committed HEAD of this repository (or --ref), resolved when it runs",
           "work folder: a new empty temporary folder (or --workdir), deleted afterwards unless --keep"]
    for i, p in enumerate(paths, 1):
        out += [f"path {i} [{p.key}] {p.label}",
                "  git clone --no-hardlinks --no-checkout <this repository> <work>/" + p.key + "/repo, then git checkout --detach <commit>",
                f"  python -m venv <work>/{p.key}/venv",
                f"  {p.install}"]
        for c in p.checks:
            env = "".join(f"{k}={v} " for k, v in c.env)
            out.append(f"  then: {env}{c.command}")
    out.append("not rehearsed (they fetch from the internet): ninshubur sources test, ninshubur run")
    return "\n".join(out)


def _base_env() -> dict[str, str]:
    """The caller's environment without any NINSHUBUR_* variable and without the ones that point Python elsewhere."""
    return {k: v for k, v in os.environ.items() if not k.startswith("NINSHUBUR_") and k not in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")}


def _clean_env(home: Path) -> dict[str, str]:
    return {**_base_env(), "NINSHUBUR_HOME": str(home), "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def sh(args: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    except OSError as e:
        return 127, f"could not start: {type(e).__name__}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 8) -> str:
    lines = [x for x in text.strip().splitlines() if x.strip()]
    return "\n".join("    " + x[:200] for x in lines[-n:])


_TALLY = re.compile(r"^=*\s*(\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d[\d.]*s\b")
_SKIP = re.compile(r"^SKIPPED \[\d+\] (.+)$")


def pytest_tally(out: str) -> tuple[str | None, list[str]]:
    """pytest's last final-tally line without its time ("1109 passed, 1 skipped"), or None if it printed none, and the
    reason line of each skip from the short summary (``-rs``), such as "tests/test_x.py:192: needs a file"."""
    lines = [ln.strip() for ln in out.splitlines()]
    tallies = [m.group(1) for ln in lines if (m := _TALLY.match(ln))]
    return (tallies[-1] if tallies else None), [m.group(1) for ln in lines if (m := _SKIP.match(ln))]


def resolve_commit(ref: str | None) -> str:
    code, out = sh(["git", "rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}"], ROOT, _base_env(), 60)
    if code != 0:
        raise SystemExit(f"cannot resolve {ref or 'HEAD'} in this repository")
    return out.strip()


def rehearse(p: InstallPath, commit: str, work: Path) -> list[tuple[str, bool, str]]:
    """Run one install path; the result lines as (label, passed, detail)."""
    base = work / p.key
    repo, venv, home = base / "repo", base / "venv", base / "home"
    base.mkdir(parents=True)
    home.mkdir()
    env = _clean_env(home)
    results: list[tuple[str, bool, str]] = []
    for args, what in (
        (["git", "clone", "--no-hardlinks", "--no-checkout", "--quiet", str(ROOT), str(repo)], "clone"),
        (["git", "-C", str(repo), "checkout", "--detach", "--quiet", commit], "checkout"),
        ([sys.executable, "-m", "venv", str(venv)], "venv"),
    ):
        code, out = sh(args, base, env, 600)
        if code != 0:
            return [("install", False, f"[{p.key}] {what} failed\n{tail(out)}")]
    py = str(venv_python(venv))
    code, out = sh([py, "-m", *shlex.split(p.install)], repo, env, 1800)
    if code != 0:
        return [("install", False, f"[{p.key}] {p.install}\n{tail(out)}")]
    results.append(("install", True, f"[{p.key}] {p.install}"))
    for c in p.checks:
        cenv = {**env, **dict(c.env)}
        argv = shlex.split(c.command)
        assert argv[0] in ("python", "ruff")
        code, out = sh([str(venv_tool(venv, argv[0])), *argv[1:]], repo, cenv, c.timeout)
        detail = f"[{p.key}] {c.command}"
        if c.name == "pytest":
            tally, skips = pytest_tally(out)
            detail += f"  {tally}" if tally else "  (no final tally in pytest's output)"
            detail += "".join(f"\n    skipped: {s[:200]}" for s in skips)
        if code != 0:
            results.append((c.name, False, f"{detail}\n{tail(out)}"))
            break
        results.append((c.name, True, detail))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", help="the commit, branch or tag to clone (default: this repository's HEAD)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    ap.add_argument("--keep", action="store_true", help="leave the work folder in place and say where it is")
    ap.add_argument("--workdir", type=Path, help="the new, empty folder to work in (default: a fresh temporary one)")
    args = ap.parse_args(argv)
    if args.dry_run:
        print(render_plan())
        return 0
    commit = resolve_commit(args.ref)
    _, dirty = sh(["git", "status", "--porcelain"], ROOT, _base_env(), 60)
    if args.workdir:
        work = args.workdir.resolve()
        if work.exists() and any(work.iterdir()):
            print(f"--workdir {work} is not empty", file=sys.stderr)
            return 2
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="ninshubur-fresh-"))
    print(f"fresh clone of {commit[:12]} ({'--ref ' + args.ref if args.ref else 'committed HEAD'}), work folder {work}")
    if dirty.strip():
        print(f"note: this working tree has {len(dirty.strip().splitlines())} uncommitted change(s); the clone does not")
    failed = 0
    started = time.monotonic()
    try:
        for p in PATHS:
            for label, ok, detail in rehearse(p, commit, work):
                print(f"{label}: {'PASS' if ok else 'FAIL'}  {detail}")
                failed += 0 if ok else 1
    finally:
        if args.keep:
            print(f"kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"{'all passed' if not failed else f'{failed} failed'} in {time.monotonic() - started:.0f} s; "
          "not rehearsed (they fetch from the internet): ninshubur sources test, ninshubur run")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
