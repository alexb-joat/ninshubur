"""Redaction: contact details are taken out of text before it leaves the store for a model or a sheet.

Replaced, each by a fixed placeholder: e-mail addresses (also the spelt-out "name at host dot com"),
links, @handles and phone numbers. The placeholders match none of the patterns, so redacting twice
changes nothing.

Not replaced: names of people in running text. No pattern finds them reliably, so this module does not
claim to; rows marked as person data never reach it (the callers read ``person_data=0`` rows only).
Bare host names ("example.org") stay too: they name an organisation, not a person.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

PLACEHOLDERS = {"email": "[email]", "link": "[link]", "handle": "[handle]", "phone": "[phone]"}

# Local parts are capped at 64 (RFC 5321 4.5.3.1.1) and labels are possessive: without both, a long run
# of address characters costs quadratic time. A longer local part still loses its last 64 characters.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]++(?:\.[A-Za-z0-9-]++)+")
_AT = r"(?:\s*[\[({]\s*at\s*[\])}]\s*|\s+at\s+)"
_DOT = r"(?:\s*[\[({]\s*dot\s*[\])}]\s*|\s+dot\s+)"
_EMAIL_SPELT = re.compile(rf"\b[A-Za-z0-9._%+-]{{1,64}}{_AT}[A-Za-z0-9-]++(?:{_DOT}[A-Za-z0-9-]++)+", re.IGNORECASE)
_LINK = re.compile(r"(?:\b[a-z][a-z0-9+.-]{1,15}://|\bwww\.)[^\s<>\"'\])]+", re.IGNORECASE)
_HANDLE = re.compile(r"(?<![\w@/.\]])@[A-Za-z0-9_][A-Za-z0-9_.-]{1,38}")
_PHONE = re.compile(r"(?<![\w.,$€£+-])(?:\+\s?)?\(?\d[\d\s().-]{6,22}\d(?![\w%])")
_GROUPS = re.compile(r"\d+")
_DATE = re.compile(r"(?<!\d)(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\d)")
_TRAILING = ".,;:!?"


@dataclass(frozen=True)
class Redacted:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _is_phone(s: str) -> bool:
    """A run of digits and separators counts as a phone number only when it looks dialled: 9 to 15
    digits, and either an international '+' or at least three groups ("555 010 0199"). A range such as
    "120000 - 150000" or a year span has two groups and stays, and so does anything holding a date."""
    if _DATE.search(s):
        return False
    groups = _GROUPS.findall(s)
    digits = sum(len(g) for g in groups)
    if not 9 <= digits <= 15:
        return False
    return s.lstrip().startswith("+") or len(groups) >= 3


def redact(text: str | None) -> Redacted:
    """Return ``text`` with contact details replaced, and how many of each kind were replaced."""
    if not text:
        return Redacted("", {})
    counts: Counter[str] = Counter()

    def sub(kind: str, pattern: re.Pattern, s: str, keep=None) -> str:
        def repl(m: re.Match) -> str:
            if keep is not None and not keep(m.group(0)):
                return m.group(0)
            counts[kind] += 1
            found = m.group(0)
            # Sentence punctuation after a link belongs to the sentence, not to the address.
            tail = found[len(found.rstrip(_TRAILING)):] if kind == "link" else ""
            return PLACEHOLDERS[kind] + tail
        return pattern.sub(repl, s)

    out = sub("email", _EMAIL, text)
    out = sub("link", _LINK, out)
    out = sub("email", _EMAIL_SPELT, out)
    out = sub("handle", _HANDLE, out)
    out = sub("phone", _PHONE, out, keep=_is_phone)
    return Redacted(out, dict(counts))
