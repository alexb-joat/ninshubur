"""E2: the explorer's systemd unit. ``--print`` shows it without installing; the unit's limits and fences are the
ones the installer itself checks with a transient ``systemd-run --user`` before it installs (bwrap starts inside,
``explore check`` fenced, only ``explorer/`` written)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.test_explorer_sandbox import needs_bwrap

SCRIPT = Path(__file__).parents[1] / "deploy" / "linux" / "install-explorer.sh"


def printed(tmp_path) -> str:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NINSHUBUR_HOME": str(tmp_path / "nh")}
    r = subprocess.run(["sh", str(SCRIPT), "--print"], capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".config").exists() and not (tmp_path / "nh").exists()  # --print installs nothing
    return r.stdout


def test_print_shows_a_fenced_oneshot_that_writes_only_its_folder(tmp_path):
    out = printed(tmp_path)
    lines = set(out.splitlines())
    nh = tmp_path / "nh"
    assert f"ConditionPathExists=!{nh}/explorer/STOP" in lines  # the kill switch stops the unit from starting
    assert f"ReadWritePaths={nh}/explorer" in lines and "ProtectHome=read-only" in lines
    assert {"Type=oneshot", "NoNewPrivileges=yes", "CapabilityBoundingSet=", "MemoryMax=1536M", "CPUQuota=50%",
            "TasksMax=64", "SystemCallFilter=@system-service @mount"} <= lines
    assert "RestrictNamespaces=yes" not in lines  # bwrap needs them; see the comment in the script
    assert "explore run --limit 10" in out and "OnCalendar=hourly" in out


def test_unknown_arguments_install_nothing(tmp_path):
    r = subprocess.run(["sh", str(SCRIPT), "--yes"], capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, timeout=10)
    assert r.returncode == 2 and "usage" in r.stderr and not (tmp_path / ".config").exists()


# -- the check under the unit's own limits (review finding 7) -----------------------------------------------------------
@pytest.fixture
def in_unit(tmp_path):
    """A folder a transient user unit can see (the unit has a private /tmp), and the script's environment; skipped
    where `systemd-run --user` can't run."""
    if not shutil.which("systemd-run") or subprocess.run(
            ["systemd-run", "--user", "--wait", "--collect", "--quiet", "true"], capture_output=True, timeout=30).returncode:
        pytest.skip("no systemd user session to run a transient unit in")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cache.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="ninshubur-test-", dir=cache))
    env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
    env |= {"NINSHUBUR_HOME": str(d / "nh"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    try:
        yield d, env
    finally:
        shutil.rmtree(d, ignore_errors=True)


def stub(d: Path, body: str) -> str:
    p = d / "python-stub"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o700)
    return str(p)


def script(env: dict, *args: str, py: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, env={**env, "NINSHUBUR_PYTHON": py},
                          timeout=120)


@needs_bwrap
def test_check_runs_under_the_units_limits_and_installs_nothing(in_unit, tmp_path):
    """The installer used to run `explore check` in the caller's shell, so a sandbox the unit's limits break still
    installed. The stub can tell where it runs: under the unit only explorer/ is writable."""
    d, env = in_unit
    fenced = stub(d, 'touch "$NINSHUBUR_HOME/explorer/yes" || exit 3\n'
                     'touch "$NINSHUBUR_HOME/logs/no" 2>/dev/null && exit 4\nexit 0\n')
    r = script(env, "--check", py=fenced)
    assert r.returncode == 0 and "nothing was installed" in r.stdout, r.stderr
    assert (d / "nh" / "explorer" / "yes").exists() and not (d / "nh" / "logs" / "no").exists()
    assert not (tmp_path / "config").exists()


@needs_bwrap
def test_a_check_that_fails_under_the_unit_installs_nothing(in_unit, tmp_path):
    d, env = in_unit
    broken = stub(d, 'echo "explore check: the sandbox did not run" >&2\nexit 2\n')
    r = script(env, "--check", py=broken)
    assert r.returncode == 1 and "the sandbox did not run" in r.stderr and "under the service's limits" in r.stderr
    r = script(env, py=broken)
    assert r.returncode == 1 and "not installing" in r.stderr and "--check shows" in r.stderr
    assert not (tmp_path / "config").exists()  # no unit file was written, so no timer was enabled


@needs_bwrap
def test_the_real_check_is_fenced_under_the_units_limits(in_unit):
    _, env = in_unit
    r = script(env, "--check", py=sys.executable)
    assert r.returncode == 0 and "verdict: fenced" in r.stdout, r.stdout + r.stderr
