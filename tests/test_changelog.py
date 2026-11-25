"""CHANGELOG.md is a draft: Keep a Changelog layout, one `## Unreleased` section, the four sub-headings and nothing
that belongs to the release session (a date, a version number) or to PR #2 (forget, the sweep). Commit hashes stay out
of the file; the review note carries the trace from each bullet to its commits."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
ALLOWED = ["Added", "Changed", "Fixed", "Security"]
DATE_LIKE = [
    r"\b\d{4}-\d{2}-\d{2}\b", r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}\b",
    r"\b\d{1,2}(?:st|nd|rd|th)? (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]* \d{4}\b",
]


def unreleased() -> str:
    start = TEXT.index("## Unreleased")
    nxt = re.search(r"^## ", TEXT[start + 3:], re.M)
    return TEXT[start:start + 3 + nxt.start()] if nxt else TEXT[start:]


def bullets() -> list[str]:
    return [x for x in unreleased().splitlines() if x.startswith("- ")]


def test_it_starts_with_the_title_and_has_exactly_one_unreleased_section():
    assert TEXT.startswith("# Changelog\n")
    assert len(re.findall(r"^## ", TEXT, re.M)) == 1 and len(re.findall(r"^## Unreleased$", TEXT, re.M)) == 1
    assert TEXT.count("# Changelog") == 1


def test_only_the_four_sub_headings_each_once_in_order_and_none_empty():
    heads = re.findall(r"^### (.*)$", unreleased(), re.M)
    assert heads == ALLOWED
    assert not re.search(r"^#### ", TEXT, re.M)
    for name in ALLOWED:
        body = unreleased().split(f"### {name}\n", 1)[1]
        body = re.split(r"^### ", body, maxsplit=1, flags=re.M)[0]
        assert [x for x in body.splitlines() if x.startswith("- ")], f"{name} has no bullet"


def test_no_date_and_no_version_number_under_unreleased():
    body = unreleased()
    for pattern in DATE_LIKE:
        assert not re.search(pattern, body), pattern
    assert not re.search(r"\b\d+\.\d+\.\d+\b", body)          # a version number such as 0.2.0
    assert not re.search(r"^## .*\d", TEXT, re.M)               # no "## 0.2.0 - date" heading yet
    assert not re.search(r"^## \[", TEXT, re.M)


def test_each_bullet_is_one_plain_line_and_commit_hashes_stay_out():
    found = bullets()
    assert len(found) >= 20
    for b in found:
        assert len(b) > 20 and b.rstrip().endswith((".", ")")), b[:60]
        for word in re.findall(r"\b[0-9a-f]{7,40}\b", b):
            assert not (re.search(r"\d", word) and re.search(r"[a-f]", word)), f"looks like a commit hash: {word}"
    non_bullet_lines = [x for x in unreleased().splitlines() if x.strip() and not x.startswith(("- ", "#"))]
    assert non_bullet_lines == []


def test_nothing_from_pr2_and_nothing_private():
    low = TEXT.lower()
    for word in ("forget", "sweep", "erase", "promises"):
        assert word not in low, word
