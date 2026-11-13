"""Redaction: contact details go, the rest of the line stays readable, and a second pass changes nothing."""
from __future__ import annotations

import pytest

from ninshubur.redact import PLACEHOLDERS, redact

CASES = [
    ("Apply to jobs@acme.example or ask Sam", "Apply to [email] or ask Sam", {"email": 1}),
    ("mail first.last+hiring@mail.acme.example.", "mail [email].", {"email": 1}),
    ("write to jobs at acme dot example today", "write to [email] today", {"email": 1}),
    ("careers [at] acme [dot] co [dot] uk", "[email]", {"email": 1}),
    ("founders(at)acme(dot)example", "[email]", {"email": 1}),
    ("Details: https://acme.example/careers?id=7#top and more", "Details: [link] and more", {"link": 1}),
    ("see www.acme.example/jobs (remote)", "see [link] (remote)", {"link": 1}),
    ("ftp://files.acme.example/x.pdf", "[link]", {"link": 1}),
    ("Read https://acme.example/jobs. Then apply!", "Read [link]. Then apply!", {"link": 1}),
    ("ping @sam_dev or @acme-hq on the usual place", "ping [handle] or [handle] on the usual place", {"handle": 2}),
    ("call +381 21 555 0100 after ten", "call [phone] after ten", {"phone": 1}),
    ("tel (555) 010-0199.", "tel [phone].", {"phone": 1}),
    ("tel 555.010.0199", "tel [phone]", {"phone": 1}),
    ("a@b.example, https://x.example, @sam, +44 20 7946 0000", "[email], [link], [handle], [phone]",
     {"email": 1, "link": 1, "handle": 1, "phone": 1}),
]


@pytest.mark.parametrize("raw, want, counts", CASES)
def test_contact_details_are_replaced_and_counted(raw: str, want: str, counts: dict):
    r = redact(raw)
    assert r.text == want and r.counts == counts and r.total == sum(counts.values())


@pytest.mark.parametrize("raw", [
    "Senior Engineer, Platform (Remote, EMEA)",
    "Salary 120000 - 150000 EUR, or $120,000-$150,000",
    "Founded 2019, Series B 2024-2026, 250 people",
    "Python 3.11.4 and PostgreSQL 16.2 on 10.0.0.1",
    "Great at scale; a dot plot is fine; look at the team page",
    "acme.example builds C++ tooling; rating 4.5/5",
    "Order #1234567890 shipped; ISO 8601 date 2026-11-13 23:10",
    "Office hours 9-17, 40 h/week, 100% remote",
    "We pay 1,500-2,500 a month",
    "Posted 13.11.2026 23:10, closes 01/12/2026 18:00",
])
def test_ordinary_text_is_left_alone(raw: str):
    r = redact(raw)
    assert r.text == raw and r.counts == {}


def test_a_second_pass_changes_nothing():
    for raw, _, _ in CASES:
        once = redact(raw)
        twice = redact(once.text)
        assert twice.text == once.text and twice.counts == {}
    assert all(redact(p).counts == {} for p in PLACEHOLDERS.values())


def test_empty_and_none():
    assert redact(None).text == "" and redact("").counts == {} and redact("").total == 0


def test_no_address_survives_in_a_mixed_post():
    post = ("Acme | Remote (EU) | Full-time | https://acme.example/jobs\n"
            "We build data tools. Email sam.lee@acme.example or DM @samlee; phone +1 (555) 010-0199.\n"
            "Alternatively: careers at acme dot example.")
    out = redact(post).text
    for leak in ("acme.example/jobs", "sam.lee@", "@samlee", "010-0199", "acme dot example"):
        assert leak not in out
    assert out.startswith("Acme | Remote (EU) | Full-time | [link]")
