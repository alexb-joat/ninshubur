"""Detectors: pure functions that look at transport facts, document structure and text and
return findings. They never raise on bad input and never store anything.

Severity ladder: ``medium`` (hold) < ``high`` (drop + source suspect) < ``integrity``
(source quarantined; transport-level). Thresholds come from policy ``detectors``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

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
HIDDEN_RANGES = (
    (0x0000, 0x0008), (0x000B, 0x000C), (0x000E, 0x001F), (0x007F, 0x009F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x2069),
    (0xFEFF, 0xFEFF), (0xE0000, 0xE007F),
)


def has_hidden_unicode(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        for lo, hi in HIDDEN_RANGES:
            if lo <= o <= hi:
                return True
    return False


_INJECTION = re.compile(
    r"(ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)"
    r"|disregard\s+(the\s+|all\s+|any\s+)?(previous|prior|above)\s+(instructions|prompts)"
    r"|you\s+are\s+now\s+(a|an|the|in)\b"
    r"|\bsystem\s+prompt\b"
    r"|^\s*(assistant|system|user|human)\s*:"
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
_SHELL = re.compile(
    r"(\bcurl\s+-|\bcurl\s+https?://|\bwget\s+https?://|\brm\s+-rf\b|\bpowershell(\.exe)?\b|\b(ba)?sh\s+-c\b"
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
    if re.search(r"[A-Za-z0-9+/]{%d,}={0,2}" % max(50, int(base64_run_min)), text):
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
