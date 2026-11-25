"""README's "Known limits": few, short, and each one ends with a reference that exists. A reference is a path, or
`path:line`; the file must exist, the line must be inside it and not blank. A limit nobody can point at is not allowed."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ninshubur.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
MAX_LIMITS = 8
REF = re.compile(r"^(?P<path>[A-Za-z0-9_./-]+?)(?::(?P<line>\d+))?$")


def section() -> list[str]:
    lines = README.splitlines()
    start = next(i for i, x in enumerate(lines) if x.strip() == "## Known limits")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    return lines[start + 1:end]


def limits() -> list[list[str]]:
    """Each bullet as its source lines (a bullet may wrap onto continuation lines)."""
    out: list[list[str]] = []
    for line in section():
        if line.startswith("- "):
            out.append([line])
        elif out and line.startswith("  ") and line.strip():
            out[-1].append(line)
    return out


def refs(bullet: list[str]) -> list[str]:
    text = " ".join(x.strip() for x in bullet)
    m = re.search(r"\(([^()]*)\)\s*$", text)
    assert m, f"a limit must end with its reference in parentheses: {text[:60]!r}"
    return [r.strip() for r in m.group(1).split(";") if r.strip()]


def tracked() -> set[str]:
    return set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines())


def test_the_section_sits_before_layout_and_has_a_few_limits():
    heads = [x for x in README.splitlines() if x.startswith("## ")]
    assert heads.index("## Known limits") == heads.index("## Layout") - 1
    assert README.count("## Known limits") == 1
    found = limits()
    assert 1 <= len(found) <= MAX_LIMITS


def test_each_limit_is_one_or_two_lines_and_bold_led():
    for bullet in limits():
        assert len(bullet) <= 2, bullet[0][:60]
        assert re.match(r"- \*\*[^*]+\*\*", bullet[0]), bullet[0][:60]


def problem(ref: str, files: set[str]) -> str | None:
    """Why a reference does not hold, or None when it does."""
    m = REF.match(ref)
    if not m:
        return f"not a path or path:line: {ref!r}"
    if m["path"] not in files:
        return f"{ref}: no such tracked file"
    if m["line"]:
        text = (ROOT / m["path"]).read_text(encoding="utf-8").splitlines()
        n = int(m["line"])
        if not 1 <= n <= len(text):
            return f"{ref}: the file has {len(text)} lines"
        if not text[n - 1].strip():
            return f"{ref}: that line is blank"
    return None


def test_every_reference_exists_and_every_line_number_is_inside_its_file():
    files = tracked()
    for bullet in limits():
        found = refs(bullet)
        assert found, bullet[0][:60]
        assert [problem(r, files) for r in found] == [None] * len(found)


def test_no_number_is_typed_into_the_prose_and_a_command_it_names_exists():
    prose = " ".join(re.sub(r"\([^()]*\)\s*$", "", " ".join(x.strip() for x in b)) for b in limits())
    assert set(re.findall(r"\d+", prose)) <= {"401", "403"}  # HTTP status codes the code itself names, no statistics
    top = next(a for a in build_parser()._actions if getattr(a, "choices", None))
    named = set(re.findall(r"`ninshubur ([a-z][a-z-]*)", prose)) | ({"forget"} if "`forget`" in prose else set())
    assert named <= set(top.choices), sorted(named - set(top.choices))  # a limit never describes a command ahead of its code
    assert "promises" not in prose.lower()  # a branch name is not something a reader can use


def test_the_checker_catches_a_bad_reference():
    files = tracked()
    n = len((ROOT / "README.md").read_text(encoding="utf-8").splitlines())
    assert problem("README.md", files) is None and problem(f"README.md:{n}", files) is None
    assert "no such tracked file" in problem("nope/missing.py", files)
    assert "the file has" in problem(f"README.md:{n + 1}", files)
    assert "the file has" in problem("README.md:0", files)
    assert "no such tracked file" in problem("logs/run-report-live-2026-11-25.md", files)  # untracked scratch is not evidence
    assert "not a path" in problem("two words", files)
    blank = next(i for i, x in enumerate((ROOT / "README.md").read_text(encoding="utf-8").splitlines(), 1) if not x.strip())
    assert "blank" in problem(f"README.md:{blank}", files)
    with pytest.raises(AssertionError):
        refs(["- **A limit with no reference.** It just stops."])
