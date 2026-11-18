"""Registrable domains from a vendored, dated Public Suffix List snapshot (``ninshubur/data/public_suffix_list.dat``,
MPL-2.0, from https://publicsuffix.org/list/public_suffix_list.dat). No dependency and no network call: refresh the
file with ``scripts/update_psl.sh`` and commit it.

Budgets and trap rules count per *registrable domain*, so ``a.trap.example``, ``b.trap.example`` and an endless
supply of other subdomains share one budget, while ``alice.github.io`` and ``bob.github.io`` (a private-section
suffix) stay separate sites."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PSL_PATH = Path(__file__).resolve().parent.parent / "data" / "public_suffix_list.dat"
_VERSION_RE = re.compile(r"^// VERSION: (\S+)")


@dataclass(frozen=True)
class SuffixList:
    rules: frozenset[str]
    wildcards: frozenset[str]   # "*.ck" is stored as "ck"
    exceptions: frozenset[str]  # "!www.ck" is stored as "www.ck"
    version: str

    def public_suffix(self, host: str) -> str:
        labels = host.lower().rstrip(".").split(".")
        # The longest matching rule wins; an exception rule beats everything; no match means the TLD alone ("*").
        for i in range(len(labels)):
            cand = ".".join(labels[i:])
            if cand in self.exceptions:
                return ".".join(labels[i + 1:])
            if cand in self.rules:
                return cand
            if i + 1 < len(labels) and ".".join(labels[i + 1:]) in self.wildcards:
                return cand
        return labels[-1]

    def registrable(self, host: str) -> str:
        """The registrable domain ("example.co.uk" for "a.b.example.co.uk"). A host that is itself a public suffix
        (``github.io``, ``co.uk``) is returned as it is: it owns no budget beyond its own name."""
        host = host.lower().rstrip(".")
        suffix = self.public_suffix(host)
        if host == suffix:
            return host
        rest = host[: -len(suffix) - 1].rsplit(".", 1)[-1]
        return f"{rest}.{suffix}"


def _ascii(rule: str) -> set[str]:
    out = {rule}
    if not rule.isascii():
        try:
            out.add(rule.encode("idna").decode("ascii"))
        except UnicodeError:
            pass
    return out


def parse(text: str) -> SuffixList:
    rules: set[str] = set()
    wild: set[str] = set()
    exc: set[str] = set()
    version = "unknown"
    for line in text.splitlines():
        line = line.strip()
        m = _VERSION_RE.match(line)
        if m:
            version = m.group(1)
        if not line or line.startswith("//"):
            continue
        rule = line.split()[0].lower()
        if rule.startswith("!"):
            exc |= _ascii(rule[1:])
        elif rule.startswith("*."):
            wild |= _ascii(rule[2:])
        else:
            rules |= _ascii(rule)
    if not rules:
        raise ValueError("public suffix list has no rules; restore ninshubur/data/public_suffix_list.dat from git")
    return SuffixList(frozenset(rules), frozenset(wild), frozenset(exc), version)


@lru_cache(maxsize=1)
def default() -> SuffixList:
    return parse(PSL_PATH.read_text(encoding="utf-8"))


def registrable(host: str) -> str:
    return default().registrable(host)
