"""E2: the explorer's systemd unit. ``--print`` shows it without installing; the unit's limits and fences are the
ones verified with a transient ``systemd-run --user`` (bwrap starts inside, ``explore check`` fenced, only
``explorer/`` written)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
