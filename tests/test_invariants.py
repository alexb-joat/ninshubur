"""Crawl-safety invariants are locked here.

``policy.yaml`` documents a ``never`` list of behaviours Ninshubur does not
implement. These tests make that claim verifiable: the list is pinned, and the
package imports none of the tooling such behaviours would require.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "ninshubur"

EXPECTED_NEVER = {"login", "captcha_bypass", "paywall_bypass", "proxies", "fingerprint_evasion"}

# Patterns that would only appear in order to defeat an invariant above.
FORBIDDEN = [
    r"\b(?:import|from)\s+(?:selenium|seleniumwire|playwright|undetected_chromedriver|fake_useragent)\b",
    r"\bimport\s+\w*stealth\b",
    r"\b(?:2captcha|anti[- ]?captcha|capmonster|deathbycaptcha)\b",
    r"\bproxies\s*=",
]


def _find_never(node):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "never":
                found.append(value)
            found += _find_never(value)
    elif isinstance(node, list):
        for item in node:
            found += _find_never(item)
    return found


def test_never_list_is_pinned():
    data = yaml.safe_load((PKG / "policy.yaml").read_text(encoding="utf-8"))
    nevers = _find_never(data)
    assert len(nevers) == 1, f"expected exactly one `never:` list, found {len(nevers)}"
    assert set(nevers[0]) == EXPECTED_NEVER


def test_no_bypass_tooling_in_package():
    offenders = []
    for py in PKG.rglob("*.py"):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if any(re.search(pat, line) for pat in FORBIDDEN):
                offenders.append(f"{py.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "bypass/evasion tooling found:\n" + "\n".join(offenders)
