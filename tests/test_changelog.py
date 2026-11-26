"""CHANGELOG.md in its released form: Keep a Changelog layout, one `## Unreleased` section on top, then one dated heading per
release (`## 0.2.0 - 2026-11-26`), newest first, the newest being the version the package declares. Each section uses the
four sub-headings in order, each once and none empty. Every `ninshubur <command>` a bullet names is a command the parser
has, so the file cannot describe a command ahead of its code. Commit hashes and branch names stay out of the file."""
from __future__ import annotations

import datetime
import re
import tomllib
from pathlib import Path

import ninshubur
from ninshubur.__main__ import build_parser

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
ALLOWED = ["Added", "Changed", "Fixed", "Security"]
RELEASE = re.compile(r"^## (\d+)\.(\d+)\.(\d+) - (\d{4}-\d{2}-\d{2})$")
DATE_LIKE = [
    r"\b\d{4}-\d{2}-\d{2}\b", r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}\b",
    r"\b\d{1,2}(?:st|nd|rd|th)? (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{4}\b",
]


def sections() -> list[tuple[str, str]]:
    """(heading line, body) for each `## ` section, in file order."""
    parts = re.split(r"^(## .*)$", TEXT, flags=re.M)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]


def released() -> list[tuple[str, str]]:
    return sections()[1:]


def bullets(body: str) -> list[str]:
    return [x for x in body.splitlines() if x.startswith("- ")]


def test_it_starts_with_the_title_and_has_exactly_one_unreleased_section_on_top():
    assert TEXT.startswith("# Changelog\n") and TEXT.count("# Changelog") == 1
    heads = [h for h, _ in sections()]
    assert heads[0] == "## Unreleased" and heads.count("## Unreleased") == 1
    assert len(released()) >= 1
    assert not re.search(r"^## \[", TEXT, re.M)


def test_every_other_heading_is_a_version_and_a_real_date_newest_first():
    found = []
    for heading, _ in released():
        m = RELEASE.match(heading)
        assert m, f"not `## X.Y.Z - YYYY-MM-DD`: {heading}"
        found.append(((int(m[1]), int(m[2]), int(m[3])), datetime.date.fromisoformat(m[4])))
    versions, dates = [v for v, _ in found], [d for _, d in found]
    assert versions == sorted(set(versions), reverse=True)        # newest first, no version twice
    assert dates == sorted(dates, reverse=True)                   # a later version is never dated earlier


def test_the_newest_heading_is_the_version_the_package_declares():
    m = RELEASE.match(released()[0][0])
    top = ".".join(m.group(i) for i in (1, 2, 3))
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert top == meta["version"] == ninshubur.__version__


def test_each_section_uses_the_sub_headings_in_order_once_and_none_empty():
    assert not re.search(r"^#### ", TEXT, re.M)
    for heading, body in sections():
        heads = re.findall(r"^### (.*)$", body, re.M)
        assert heads == [h for h in ALLOWED if h in heads], f"{heading}: {heads}"
        if heading != "## Unreleased":
            assert heads == ALLOWED, heading
        for name in heads:
            part = re.split(r"^### ", body.split(f"### {name}\n", 1)[1], maxsplit=1, flags=re.M)[0]
            assert bullets(part), f"{heading}: {name} has no bullet"


def test_no_date_and_no_version_number_under_unreleased():
    body = sections()[0][1]
    for pattern in DATE_LIKE:
        assert not re.search(pattern, body), pattern
    assert not re.search(r"\b\d+\.\d+\.\d+\b", body)          # a version number such as 0.3.0 belongs to a release heading


def test_each_bullet_is_one_plain_line_and_commit_hashes_stay_out():
    found = [b for _, body in sections() for b in bullets(body)]
    assert len(found) >= 20
    for b in found:
        assert len(b) > 20 and b.rstrip().endswith((".", ")")), b[:60]
        for word in re.findall(r"\b[0-9a-f]{7,40}\b", b):
            assert not (re.search(r"\d", word) and re.search(r"[a-f]", word)), f"looks like a commit hash: {word}"
    for _, body in sections():
        assert [x for x in body.splitlines() if x.strip() and not x.startswith(("- ", "#"))] == []


def test_every_command_a_bullet_names_exists_and_no_branch_name_is_in_the_file():
    top = next(a for a in build_parser()._actions if getattr(a, "choices", None))
    named = set(re.findall(r"`ninshubur ([a-z][a-z-]*)", TEXT))
    assert "forget" in named and named <= set(top.choices), sorted(named - set(top.choices))
    assert "promises" not in TEXT.lower()
