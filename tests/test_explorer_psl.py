"""Registrable domains: cases from the Public Suffix List's own test vectors (publicsuffix.org/list/), plus the
trap shape the explorer budgets against."""
from __future__ import annotations

import re

import pytest

from ninshubur.explorer import psl


@pytest.mark.parametrize("host,expected", [
    ("example.com", "example.com"),
    ("b.example.com", "example.com"),
    ("a.b.example.com", "example.com"),
    ("www.example.co.uk", "example.co.uk"),
    ("a.b.example.uk.com", "example.uk.com"),       # private-section suffix
    ("co.uk", "co.uk"),                             # a bare public suffix stands for itself
    ("b.test.ck", "b.test.ck"),                     # wildcard rule *.ck
    ("a.b.test.ck", "b.test.ck"),
    ("www.ck", "www.ck"),                           # exception rule !www.ck
    ("www.www.ck", "www.ck"),
    ("a.b.kawasaki.jp", "a.b.kawasaki.jp"),
    ("x.city.kawasaki.jp", "city.kawasaki.jp"),     # exception under a wildcard
    ("a.alice.github.io", "alice.github.io"),       # each Pages user is a separate site
    ("xn--85x722f.xn--55qx5d.cn", "xn--85x722f.xn--55qx5d.cn"),  # IDN rule matched in its ASCII form
    ("WWW.Example.COM.", "example.com"),
    ("example.unknowntld", "example.unknowntld"),   # no rule: the default "*" makes the TLD the suffix
])
def test_registrable(host, expected):
    assert psl.registrable(host) == expected


def test_endless_subdomains_share_one_site():
    assert {psl.registrable(f"p{i}.q{i}.trap.example.net") for i in range(200)} == {"example.net"}


def test_snapshot_is_dated_and_whole():
    d = psl.default()
    assert re.match(r"\d{4}-\d{2}-\d{2}_", d.version)
    assert len(d.rules) > 9000 and d.wildcards and d.exceptions
    text = psl.PSL_PATH.read_text(encoding="utf-8")
    assert "Mozilla Public" in text and "===BEGIN PRIVATE DOMAINS===" in text  # licence header and both sections kept


def test_empty_list_fails_loudly():
    with pytest.raises(ValueError, match="restore"):
        psl.parse("// nothing here\n")
