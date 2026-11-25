"""scripts/fresh_clone.py: the plan runs nothing in --dry-run, its install commands are the ones the README, CONTRIBUTING
and CI write (so the rehearsal cannot drift from the instructions), and it rehearses from a clone of the committed HEAD,
never the working folder. No network here: the venv and pip steps are stubbed."""
from __future__ import annotations

import importlib.util
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fresh_clone", ROOT / "scripts" / "fresh_clone.py")
fc = importlib.util.module_from_spec(_spec)
sys.modules["fresh_clone"] = fc  # a dataclass in a module loaded by path needs this on Python 3.14
_spec.loader.exec_module(fc)


def doc_commands(path: str, start: str) -> list[str]:
    """Lines of a document's code blocks that begin with ``start`` (a shell prompt prefix and a comment tail removed)."""
    text = (ROOT / path).read_text(encoding="utf-8")
    out = []
    for block in re.findall(r"```(?:bash|sh|console)?\n(.*?)```", text, re.S):
        for line in block.splitlines():
            line = re.sub(r"\s+#.*$", "", line.strip())
            if line.startswith(start):
                out.append(line)
    return out


def test_dry_run_prints_the_plan_and_runs_nothing(monkeypatch, capsys, tmp_path):
    def boom(*a, **k):
        raise AssertionError("--dry-run must run nothing")
    monkeypatch.setattr(fc.subprocess, "run", boom)
    monkeypatch.setattr(fc.tempfile, "mkdtemp", boom)
    monkeypatch.chdir(tmp_path)
    assert fc.main(["--dry-run", "--keep", "--ref", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("fresh_clone plan (nothing is run)")
    for p in fc.PATHS:
        assert p.install in out and f"[{p.key}]" in out
    assert "git clone --no-hardlinks" in out and "not rehearsed" in out and "ninshubur sources test" in out
    assert list(tmp_path.iterdir()) == []


def test_the_install_commands_are_the_ones_the_documents_write():
    readme, contributing, ci = fc.PATHS[0], fc.PATHS[1], (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert doc_commands("README.md", "pip install") == [readme.install]
    assert doc_commands("CONTRIBUTING.md", "pip install") == [contributing.install]
    assert re.findall(r"run: (pip install .*)$", ci, re.M) == [contributing.install]
    assert shlex.split(contributing.install) == ["pip", "install", "-e", ".[dev]"]


def test_the_checks_after_the_install_are_the_ones_contributing_and_ci_name():
    pytest_check, smoke, check = fc.PATHS[1].checks
    assert doc_commands("CONTRIBUTING.md", "python -m pytest") == [pytest_check.command]
    assert doc_commands("CONTRIBUTING.md", "python scripts/smoke.py") == [smoke.command]
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(rf"run: {re.escape(pytest_check.command)}( -rs)?$", ci, re.M)
    assert re.search(rf"run: {re.escape(smoke.command)}$", ci, re.M)
    assert re.search(rf"run: {re.escape(check.command)}$", ci, re.M)
    assert dict(check.env) == {"NINSHUBUR_CONFIG": "config.example.yaml"} and f"NINSHUBUR_CONFIG: {dict(check.env)['NINSHUBUR_CONFIG']}" in ci


def test_only_offline_commands_run_after_the_install():
    banned = ("sources test", " run", "qualify", "explore", "lookup")
    for p in fc.PATHS:
        for c in p.checks:
            assert not any(b in c.command for b in banned), c.command
    assert not any("--replay" in c.command for p in fc.PATHS for c in p.checks)  # smoke replays inside the test, not here


def test_the_requirements_file_the_plan_installs_stands_alone_and_matches_the_packaging_metadata():
    import tomllib
    name = shlex.split(fc.PATHS[2].install)[-1]
    assert (ROOT / name).is_file()

    def names(path: str) -> set[str]:
        out: set[str] = set()
        for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            m = re.match(r"-[rc]\s+(\S+)", line)
            if m:  # every file it points at is in the repository, so the install can start from a clean clone
                assert (ROOT / m.group(1)).is_file(), f"{path} names {m.group(1)}, which is not in the repository"
                if line.startswith("-r"):
                    out |= names(m.group(1))
            elif line:
                out.add(re.split(r"[<>=!~\[; ]", line)[0].lower())
        return out
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = {re.split(r"[<>=!~\[; ]", d)[0].lower() for d in meta["dependencies"] if "sys_platform" not in d}  # tzdata is Windows only
    dev = {re.split(r"[<>=!~\[; ]", d)[0].lower() for d in meta["optional-dependencies"]["dev"]}
    assert names(name) == runtime | dev and runtime == names("requirements.in")


# --- what it does, with the network steps stubbed -----------------------------------------------
class Recorder:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple[list[str], Path, dict]] = []
        self.fail_on = fail_on

    def __call__(self, args, cwd, env, timeout):
        self.calls.append((list(args), Path(cwd), dict(env)))
        joined = " ".join(map(str, args))
        if self.fail_on and self.fail_on in joined:
            return 1, "ERROR: something broke\nline two"
        if args[0] == "git":
            return fc_real_sh(args, cwd, env, timeout)
        if "pytest" in joined:
            return 0, "....\n1019 passed in 12.3s\n"
        if "-m" in args and "venv" in args:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return 0, ""


fc_real_sh = fc.sh


@pytest.fixture
def synthetic_repo(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (["init", "-q"], ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True)
    (src / "committed.txt").write_text("in the commit\n")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(src), *ident, "commit", "-q", "-m", "one"], check=True, capture_output=True)
    (src / "uncommitted.txt").write_text("never committed\n")
    (src / "committed.txt").write_text("edited after the commit\n")
    monkeypatch.setattr(fc, "ROOT", src)
    return src


def test_it_clones_the_committed_head_so_nothing_uncommitted_gets_in(synthetic_repo, tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fc, "sh", rec)
    commit = fc.resolve_commit(None)
    work = tmp_path / "work"
    work.mkdir()
    results = fc.rehearse(fc.PATHS[0], commit, work)
    repo = work / "readme" / "repo"
    assert results == [("install", True, "[readme] pip install -e .")]
    assert (repo / "committed.txt").read_text() == "in the commit\n"     # the committed text, not the edit
    assert not (repo / "uncommitted.txt").exists()
    clone = next(c for c in rec.calls if c[0][:2] == ["git", "clone"])
    assert "--no-hardlinks" in clone[0] and str(synthetic_repo) in clone[0]
    assert any(c[0][:3] == ["git", "-C", str(repo)] and "--detach" in c[0] and commit in c[0] for c in rec.calls)


def test_each_step_runs_in_the_clone_with_a_clean_environment_and_a_throwaway_home(synthetic_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("NINSHUBUR_HOME", "/somewhere/real")
    monkeypatch.setenv("NINSHUBUR_ROUTER_KEY", "secret-value")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    rec = Recorder()
    monkeypatch.setattr(fc, "sh", rec)
    work = tmp_path / "work"
    work.mkdir()
    results = fc.rehearse(fc.PATHS[1], fc.resolve_commit(None), work)
    assert [(r[0], r[1]) for r in results] == [("install", True), ("pytest", True), ("smoke", True), ("check", True)]
    assert "1019 passed" in results[1][2]
    repo, venv = work / "contributing" / "repo", work / "contributing" / "venv"
    pip = next(c for c in rec.calls if "pip" in c[0])
    assert pip[0][1:] == ["-m", "pip", "install", "-e", ".[dev]"] and pip[1] == repo
    after = [c for c in rec.calls if c[1] == repo and c[0][0] != "git" and "pip" not in c[0]]
    assert [c[0][1:] for c in after] == [["-m", "pytest", "-q"], ["scripts/smoke.py"], ["-m", "ninshubur", "check"]]
    for _, _, env in rec.calls:
        assert "NINSHUBUR_ROUTER_KEY" not in env and "PYTHONPATH" not in env and env.get("NINSHUBUR_HOME") != "/somewhere/real"
    assert all(Path(env["NINSHUBUR_HOME"]).is_relative_to(work) for _, _, env in after)
    assert after[2][2]["NINSHUBUR_CONFIG"] == "config.example.yaml"
    assert all(str(c[0][0]).startswith(str(venv)) for c in after)  # the venv's python, not the caller's


def test_a_failing_install_is_reported_with_its_tail_and_the_other_paths_still_run(synthetic_repo, tmp_path, monkeypatch, capsys):
    rec = Recorder(fail_on="requirements-dev.in")
    monkeypatch.setattr(fc, "sh", rec)

    def mkdtemp(prefix=""):
        (tmp_path / "w").mkdir()
        return str(tmp_path / "w")
    monkeypatch.setattr(fc.tempfile, "mkdtemp", mkdtemp)
    code = fc.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("install: PASS") == 2 and out.count("install: FAIL") == 1
    assert "install: FAIL  [requirements-dev] pip install -r requirements-dev.in\n    ERROR: something broke\n    line two" in out
    assert "1 failed" in out and "ninshubur sources test, ninshubur run" in out
    assert not (tmp_path / "w").exists()                       # removed afterwards


def test_a_failing_check_stops_that_path_and_keep_leaves_the_folder(synthetic_repo, tmp_path, monkeypatch, capsys):
    rec = Recorder(fail_on="smoke.py")
    monkeypatch.setattr(fc, "sh", rec)
    work = tmp_path / "kept"
    code = fc.main(["--keep", "--workdir", str(work)])
    out = capsys.readouterr().out
    assert code == 1 and "smoke: FAIL" in out and "check: " not in out and f"kept: {work}" in out
    assert (work / "contributing").is_dir()
    assert fc.main(["--workdir", str(work)]) == 2               # not empty: refused


def test_the_ref_option_clones_that_commit(synthetic_repo, tmp_path, monkeypatch):
    first = fc.resolve_commit(None)
    (synthetic_repo / "committed.txt").write_text("second\n")
    subprocess.run(["git", "-C", str(synthetic_repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qam", "two"],
                   check=True, capture_output=True)
    assert fc.resolve_commit(None) != first and fc.resolve_commit(first[:10]) == first
    with pytest.raises(SystemExit):
        fc.resolve_commit("no-such-ref")
