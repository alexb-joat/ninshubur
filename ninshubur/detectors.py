"""Detectors: pure functions that look at transport facts, document structure and text and
return findings. They never raise on bad input and never store anything.

Severity ladder: ``medium`` (hold) < ``high`` (drop + source suspect) < ``integrity``
(source quarantined; transport-level). Thresholds come from policy ``detectors``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .policy import check_url, host_of

SEVERITIES = ("medium", "high", "integrity")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    detail: str = ""


def worst(findings: Iterable[Finding]) -> str | None:
    best: str | None = None
    for f in findings:
        if best is None or _RANK[f.severity] > _RANK[best]:
            best = f.severity
    return best


def codes(findings: Iterable[Finding]) -> tuple[str, ...]:
    seen: list[str] = []
    for f in findings:
        if f.code not in seen:
            seen.append(f.code)
    return tuple(seen)


# --- content -----------------------------------------------------------------------------
# Invisible characters that can carry instructions or data a reader never sees. Found in a field: a finding, and the
# character is removed.
HIDDEN_RANGES = (
    (0x0000, 0x0008), (0x000B, 0x000C), (0x000E, 0x001F), (0x007F, 0x009F),
    (0x061C, 0x061C),  # Arabic letter mark (a bidi control)
    (0x180E, 0x180E),  # Mongolian vowel separator (zero width since Unicode 6.3)
    (0x200B, 0x200B), (0x200E, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),  # interlinear annotation controls
    (0xE0000, 0xE007F),  # tag characters
    (0xE0100, 0xE01EF),  # variation selectors 17-256: carry one byte each, the core of "emoji smuggling"
)
# Invisible but common in honest text, so removed without a finding: the soft hyphen (German and Russian pages are
# full of them), the zero-width non-joiner and joiner (Persian and Indic spelling; the joiner builds emoji such as
# the woman technologist, U+1F469 U+200D U+1F4BB) and variation selectors 1-16 (U+FE0F follows most emoji).
STRIP_ONLY_RANGES = ((0x00AD, 0x00AD), (0x200C, 0x200D), (0xFE00, 0xFE0F))


def _char_class(ranges) -> str:
    esc = lambda o: f"\\U{o:08x}"  # noqa: E731
    return "".join(esc(lo) if lo == hi else f"{esc(lo)}-{esc(hi)}" for lo, hi in ranges)


_HIDDEN = re.compile(f"[{_char_class(HIDDEN_RANGES)}]")
_INVISIBLE = re.compile(f"[{_char_class(HIDDEN_RANGES + STRIP_ONLY_RANGES)}]")
# One selector after a character is ordinary; two or more in a row select nothing and are how data is smuggled.
_SELECTOR_RUN = re.compile(f"[{_char_class(((0xFE00, 0xFE0F), (0xE0100, 0xE01EF)))}]{{2,}}")
# Likewise one joiner between two emoji is ordinary; a run of zero-width characters is a channel.
_ZERO_WIDTH_RUN = re.compile(f"[{_char_class(((0x200B, 0x200D), (0x2060, 0x2060), (0xFEFF, 0xFEFF)))}]{{2,}}")


def has_hidden_unicode(text: str) -> bool:
    return bool(_HIDDEN.search(text) or _SELECTOR_RUN.search(text) or _ZERO_WIDTH_RUN.search(text))


def strip_invisible(text: str) -> str:
    """``text`` without any hidden or strip-only character."""
    return _INVISIBLE.sub("", text)


_INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)"
    r"|disregard\s+(the\s+|all\s+|any\s+)?(previous|prior|above)\s+(instructions|prompts)"
    r"|you\s+are\s+now\s+(a|an|the|in)\b"
    r"|\bsystem\s+prompt\b"
    r"|^[^\S\n]*(assistant|system|user|human)\s*:"  # same lines as ^\s*, without rescanning blank runs
    r"|<\|im_(start|end)\|>"
    r"|\[/?INST\]"
    r"|\bBEGIN\s+INSTRUCTIONS\b"
    r"|\btool_calls?\b|\bfunction_call\b)",
    re.IGNORECASE | re.MULTILINE,
)
_HTML_ACTIVE = re.compile(
    r"(<\s*script\b|javascript\s*:|\bdata\s*:\s*[a-z]+/[a-z0-9.+-]+\s*[;,]|<\s*iframe\b|<\s*object\b|<\s*embed\b"
    r"|\bon(load|error|click|mouseover|focus|submit|input|change)\s*=)",
    re.IGNORECASE,
)
# PowerShell only in command shape: the .exe, a launcher before it (cmd /c, Start-Process), or after it a flag
# (hyphen, en dash or em dash, which PowerShell also accepts), a /c-style switch, iex or iwr, a script block or
# a .ps1 file. The bare word is ordinary prose in job posts ("Scripting (Bash, Python, PowerShell)").
_PWSH = (r"\bpowershell\.exe\b"
         r"|\b(cmd(\.exe)?\s+/c|start-process)\s+[\"']?(powershell|pwsh)\b"
         r"|\b(powershell|pwsh)(\.exe)?\s+([-\u2013\u2014]{1,2}[a-z]+\b(?!-)"
         r"|/(c|command|e|ec|enc|encodedcommand|nop|noprofile|noni|w|windowstyle|ep|executionpolicy|f|file)\b(?![#+])"
         r"|[\"'(;]*\s*(iex|iwr)\b|[\"'(]*&\s*\{|\S+\.ps1\b)")
_SHELL = re.compile(
    r"(\bcurl\s+-|\bcurl\s+https?://|\bwget\s+https?://|\brm\s+-rf\b|" + _PWSH + r"|\b(ba)?sh\s+-c\b"
    r"|\|\s*(ba)?sh\b|\bchmod\s+\+x\b|\bInvoke-(WebRequest|Expression)\b|\bbase64\s+(-d|--decode)\b)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_API_KEY = re.compile(
    r"(\bsk-(proj-|ant-)?[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{22,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}|\bAIza[0-9A-Za-z_-]{35}\b|\bhf_[A-Za-z0-9]{30,}\b)"
)
_URL = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)
_WORD = re.compile(r"\w{2,}", re.UNICODE)


def _script_of(ch: str) -> str | None:
    if not ch.isalpha():
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for s in ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN"):
        if name.startswith(s):
            return s
    return None


def mixed_script_tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in _WORD.findall(text):
        scripts = {s for s in map(_script_of, tok) if s}
        if len(scripts) > 1:
            out.append(tok)
    return out


def edit_distance(a: str, b: str, limit: int = 3) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _registrable(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_lookalike(host: str, allowlisted: Iterable[str], max_distance: int = 2) -> str | None:
    """Return the allowlisted host ``host`` imitates, or None."""
    host = host.lower()
    if not host:
        return None
    for good in allowlisted:
        good = good.lower()
        if host == good or host.endswith("." + good):
            continue
        # evil.example that embeds a trusted name as a subdomain: confs.tech.evil.example
        if host.startswith(good + ".") or ("." + good + ".") in ("." + host):
            return good
        if edit_distance(_registrable(host), _registrable(good), max_distance) <= max_distance:
            return good
    return None


def repetition_anomaly(text: str) -> bool:
    if len(text) < 200:
        return False
    if len(set(text)) < 8:
        return True
    run = re.search(r"(.)\1{99,}", text)
    if run:
        return True
    chunk = re.search(r"(.{4,40}?)\1{30,}", text, re.DOTALL)
    return chunk is not None


def scan_text(text: str, *, allowlisted_hosts: Iterable[str] = (), base64_run_min: int = 200,
              lookalike_max_distance: int = 2) -> list[Finding]:
    """Content detectors on one text field. Run on the raw string, before sanitising."""
    if not isinstance(text, str) or not text:
        return []
    f: list[Finding] = []
    if has_hidden_unicode(text):
        f.append(Finding("high", "hidden_unicode"))
    if _INJECTION.search(text):
        f.append(Finding("high", "prompt_injection"))
    if _HTML_ACTIVE.search(text):
        f.append(Finding("high", "active_html"))
    if _PRIVATE_KEY.search(text):
        f.append(Finding("high", "private_key"))
    if _API_KEY.search(text):
        f.append(Finding("high", "api_key_like"))
    toks = mixed_script_tokens(text)
    if toks:
        f.append(Finding("high", "mixed_script", toks[0][:40]))
    if _SHELL.search(text):
        f.append(Finding("medium", "shell_fragment"))
    run_min = max(50, int(base64_run_min))
    if re.search(rf"[A-Za-z0-9+/]{{{run_min},}}={{0,2}}", text):
        f.append(Finding("medium", "base64_run"))
    if repetition_anomaly(text):
        f.append(Finding("medium", "repetition"))
    allow = tuple(allowlisted_hosts)
    for m in _URL.findall(text):
        code = check_url(m)
        if code in ("url_ip_literal", "url_punycode"):
            f.append(Finding("high", code, m[:80]))
        elif code == "url_userinfo":
            f.append(Finding("high", code, m[:80]))
        host = host_of(m)
        if allow and host:
            target = is_lookalike(host, allow, lookalike_max_distance)
            if target:
                f.append(Finding("high", "lookalike_host", f"{host} ~ {target}"))
    return f


# --- transport ----------------------------------------------------------------------------
def check_transport(*, tls_ok: bool = True, request_host: str, final_host: str | None,
                    content_type: str | None, expected_content_type: str | None,
                    size: int, max_bytes: int, decompression_ratio: float | None = None,
                    decompression_ratio_max: float = 100.0, utf8_ok: bool = True) -> list[Finding]:
    f: list[Finding] = []
    if not tls_ok:
        f.append(Finding("integrity", "cert_error"))
    if final_host and final_host.lower() != request_host.lower():
        f.append(Finding("integrity", "off_host_redirect", final_host))
    if expected_content_type and not content_type_matches(content_type, expected_content_type):
        f.append(Finding("integrity", "content_type_mismatch", str(content_type)[:60]))
    if size > max_bytes:
        f.append(Finding("integrity", "body_too_large", str(size)))
    if decompression_ratio is not None and decompression_ratio > decompression_ratio_max:
        f.append(Finding("integrity", "decompression_bomb", f"{decompression_ratio:.0f}x"))
    if not utf8_ok:
        f.append(Finding("integrity", "invalid_utf8"))
    return f


def content_type_matches(actual: str | None, expected: str) -> bool:
    """``expected`` is ``a/b|c/d`` (alternatives). Parameters like charset are ignored."""
    if not actual:
        return False
    main = actual.split(";", 1)[0].strip().lower()
    for alt in expected.split("|"):
        alt = alt.strip().lower()
        if not alt:
            continue
        if alt.endswith("/*") and main.startswith(alt[:-1]):
            return True
        if main == alt:
            return True
        if alt == "application/json" and main.endswith("+json"):
            return True
        if alt in ("application/atom+xml", "application/rss+xml") and main in ("application/xml", "text/xml"):
            return True
    return False


# --- structure ----------------------------------------------------------------------------
MAGIC = (b"MZ", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xce", b"PK\x03\x04", b"%PDF", b"\x1f\x8b",
         b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"#!")


def check_bytes(body: bytes) -> list[Finding]:
    head = body[:8]
    for m in MAGIC:
        if head.startswith(m):
            return [Finding("high", "binary_magic", m.hex())]
    return []


JSON_MAX_BYTES = 8_000_000  # the fetcher's hard body cap: nothing larger is ever parsed


def load_json(data: bytes | str, *, max_bytes: int = JSON_MAX_BYTES, max_depth: int = 64) -> Any:
    """``json.loads`` for hostile input: every failure is a ValueError, so a caller's ``except ValueError`` holds.

    Too large or nested past ``max_depth`` raises ValueError with the reason. Bytes must be UTF-8
    (UnicodeDecodeError is a ValueError). Nesting is counted on the text before anything is parsed, because the
    parser's own guard can't be relied on: up to Python 3.13 it raises RecursionError after a fixed number of
    levels, from 3.14 on it stops only when the C stack runs low. Measured on 3.14.4, 24 Nov: 8 MB of brackets
    under ``ulimit -s unlimited`` parsed in 1.1 s at 934 MB peak before the depth check below could refuse it."""
    if len(data) > max_bytes:
        raise ValueError(f"JSON of {len(data)} bytes is over the {max_bytes}-byte limit")
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if nesting_over(data, max_depth):
        raise ValueError(f"JSON nested deeper than {max_depth} levels")
    try:
        obj = json.loads(data)
    except RecursionError:  # unreachable while max_depth stays far below the interpreter's limit; kept as a backstop
        raise ValueError("JSON nested too deeply to parse") from None
    if depth_of(obj, limit=max_depth) > max_depth:
        raise ValueError(f"JSON nested deeper than {max_depth} levels")
    return obj


_JSON_MARKS = re.compile(r'["\[\]{}]')


def nesting_over(text: str, limit: int) -> bool:
    """True when the brackets of a JSON text open more than ``limit`` levels at once. One pass, no recursion.

    Brackets inside strings don't count; a quote ends a string unless an odd number of backslashes precedes it.
    On a valid text the count is exact. On a broken one it is exact up to the first error, which is as far as a
    parser gets."""
    if text.count("[") + text.count("{") <= limit:
        return False
    depth = 0
    in_string = False
    for m in _JSON_MARKS.finditer(text):
        ch = m.group()
        if ch == '"':
            if in_string:
                i = m.start() - 1
                while i >= 0 and text[i] == "\\":
                    i -= 1
                if (m.start() - 1 - i) % 2:
                    continue  # an escaped quote: still inside the string
            in_string = not in_string
        elif in_string:
            continue
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return True
        else:
            depth -= 1
    return False


def depth_of(obj: Any, limit: int = 64) -> int:
    """Iterative depth measurement that cannot blow the stack."""
    best = 0
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        cur, d = stack.pop()
        if d > best:
            best = d
        if d > limit:
            return d
        if isinstance(cur, dict):
            stack.extend((v, d + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, d + 1) for v in cur)
    return best


def check_structure(data: Any, *, max_depth: int = 32) -> list[Finding]:
    f: list[Finding] = []
    d = depth_of(data, limit=max_depth)
    if d > max_depth:
        f.append(Finding("high", "depth_exceeded", str(d)))
    return f


# --- behaviour ----------------------------------------------------------------------------
def item_explosion(count: int, rolling_median: float | None, factor: float = 5.0) -> Finding | None:
    if rolling_median and rolling_median >= 5 and count > factor * rolling_median:
        return Finding("medium", "item_explosion", f"{count} vs median {rolling_median:.0f}")
    return None
