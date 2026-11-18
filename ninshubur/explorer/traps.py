"""Trap rules, pure functions only: canonical URLs, URL shapes, URL-level traps, near-duplicate fingerprints, parked
and soft-404 pages. The frontier applies them; nothing here touches the network or the database.

What they catch (plan section 2A): calendars and faceted URLs that mint endless pages, session ids and tracking
parameters that make one page look like many, repeated path segments (``/a/b/a/b/...``), generated mazes and
tarpits (near-duplicate pages), soft-404 sites (any path answers 200) and parked domains. Every regex here runs in
linear time on bounded input (``tests/test_redos.py`` style: no nested quantifiers)."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset({"gclid", "dclid", "fbclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "_hsenc",
                             "_hsmi", "ref_src", "ref_url", "_ga", "_gl", "oly_enc_id", "oly_anon_id", "vero_id", "wickedid"})
SESSION_PARAMS = frozenset({"sid", "sessionid", "session_id", "session", "phpsessid", "jsessionid", "sessid",
                            "aspsessionid", "cfid", "cftoken", "zenid", "osCsid".lower()})
_PATH_SESSION = re.compile(r";(?:jsessionid|phpsessid|sid|sessionid)=[^/?#]*", re.I)
_DIGITS = re.compile(r"\d+")
# Years a calendar can plausibly show (1900-2199): wide enough to catch a calendar walked decades out, narrow enough
# that an id like /p/2345/12 is not read as a date.
_YEAR_SEG = re.compile(r"^(19\d\d|2[01]\d\d)$")
_DATE_TOKEN = re.compile(r"(?<!\d)(19\d\d|2[01]\d\d)-(0?[1-9]|1[0-2])(?:-\d{1,2})?(?!\d)")
_DATE_KEYS = frozenset({"year", "y", "yr", "date", "month", "m", "day", "d", "start", "end", "from", "to", "since",
                        "until", "cal", "calendar", "week", "tribe-bar-date", "ical", "eventdate"})


def _drop_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in TRACKING_PARAMS or k in SESSION_PARAMS


def canonical(url: str) -> str:
    """One spelling per page: lower-case scheme and host, no default port, no fragment, no session or tracking
    parameters, query sorted, ``//`` collapsed, an empty path as ``/``. Percent-encoding is left as it is."""
    p = urlsplit(url)
    host = (p.hostname or "").rstrip(".")
    port = p.port
    netloc = host if port in (None, {"https": 443, "http": 80}.get(p.scheme.lower())) else f"{host}:{port}"
    path = _PATH_SESSION.sub("", p.path)
    while "//" in path:
        path = path.replace("//", "/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _drop_param(k)))
    return urlunsplit((p.scheme.lower(), netloc, path or "/", query, ""))


def segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def shape(url: str) -> str:
    """The URL with every digit run as ``#`` and query values dropped: ``/events/2026/11/18?page=3`` and
    ``/events/2027/01/02?page=9`` share the shape ``/events/#/#/#?page``. Many URLs of one shape on one site are
    a calendar, a pagination run or an id space, and share one budget."""
    p = urlsplit(url)
    keys = sorted({_DIGITS.sub("#", k.lower()) for k, _ in parse_qsl(p.query, keep_blank_values=True)})
    return _DIGITS.sub("#", p.path.lower()) + ("?" + "&".join(keys) if keys else "")


def _years(url: str) -> list[int]:
    p = urlsplit(url)
    segs = segments(url)
    out = [int(s) for i, s in enumerate(segs) if _YEAR_SEG.match(s) and i + 1 < len(segs) and segs[i + 1].isdigit()
           and 1 <= int(segs[i + 1]) <= 12]
    out += [int(m.group(1)) for m in _DATE_TOKEN.finditer(p.path)]
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if k.lower() in _DATE_KEYS:
            out += [int(m.group(1)) for m in _DATE_TOKEN.finditer(v)]
            if _YEAR_SEG.match(v):
                out.append(int(v))
    return out


def url_trap(url: str, *, max_len: int, max_depth: int, max_query_params: int, year_min: int, year_max: int) -> str | None:
    """A refusal code for a URL whose shape alone marks it as a trap, or None."""
    if len(url) > max_len:
        return "trap_url_length"
    segs = segments(url)
    if len(segs) > max_depth:
        return "trap_depth"
    if len(parse_qsl(urlsplit(url).query, keep_blank_values=True)) > max_query_params:
        return "trap_query_params"
    low = [s.lower() for s in segs]
    if any(low.count(s) >= 3 for s in set(low)):
        return "trap_repeated_segment"
    if any(low[i:i + 2] == low[i + 2:i + 4] for i in range(len(low) - 3)):
        return "trap_repeated_segment"
    if any(y < year_min or y > year_max for y in _years(url)):
        return "trap_calendar"
    return None


# -- page fingerprints --------------------------------------------------------------------------------------------
_TAG = re.compile(rb"<[^<>]{0,2000}>")
_WORD = re.compile(r"\w+")
FINGERPRINT_BYTES = 200_000
SIMHASH_WORDS = 5_000  # enough to tell pages apart; keeps one fingerprint under ~50 ms


def _strip_blocks(body: bytes) -> bytes:
    """Remove <script> and <style> blocks with one forward scan (no regex backtracking: a page of 10,000 unclosed
    <script tags costs one pass, not 10,000 scans). An unclosed block hides the rest of the page."""
    low, out, i = body.lower(), [], 0
    while True:
        starts = [j for j in (low.find(b"<script", i), low.find(b"<style", i)) if j >= 0]
        if not starts:
            out.append(body[i:])
            return b" ".join(out)
        j = min(starts)
        out.append(body[i:j])
        close = b"</script" if low.startswith(b"<script", j) else b"</style"
        k = low.find(close, j)
        if k < 0:
            return b" ".join(out)
        i = low.find(b">", k)
        i = len(body) if i < 0 else i + 1


def visible_words(body: bytes) -> list[str]:
    """Rough visible text of a page, for fingerprints only (never stored, never parsed further): scripts, styles
    and tags removed, first 200 KB only."""
    b = _TAG.sub(b" ", _strip_blocks(body[:FINGERPRINT_BYTES]))
    return _WORD.findall(b.decode("utf-8", "replace").lower())


def simhash(body: bytes) -> int:
    """64-bit simhash over word 3-shingles. Near-identical pages land a few bits apart; a maze of generated pages
    built from one template does too."""
    words = visible_words(body)[:SIMHASH_WORDS]
    shingles = [" ".join(words[i:i + 3]) for i in range(max(1, len(words) - 2))] if words else [""]
    acc = [0] * 64
    for s in shingles:
        h = int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            acc[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if acc[bit] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# A heuristic, and labelled as one: parking and for-sale landers share a handful of phrases and vendor names.
PARKED_MARKERS = (b"this domain is for sale", b"this domain may be for sale", b"buy this domain", b"domain is parked",
                  b"parked free", b"sedoparking", b"parkingcrew", b"bodis.com", b"dan.com/buy-domain", b"afternic",
                  b"hugedomains.com", b"domain has expired", b"renew this domain", b"parklogic", b"above.com/marketplace")


def parked(body: bytes) -> str | None:
    low = body[:FINGERPRINT_BYTES].lower()
    return next((m.decode() for m in PARKED_MARKERS if m in low), None)


def soft404(probe_status: int | None, probe_location: str = "") -> bool:
    """A path nobody links to (``/<random>``) that answers 200, or redirects to the front page, means every path
    answers: page counts and yields on that site measure nothing."""
    if probe_status == 200:
        return True
    if probe_status in (301, 302, 303, 307, 308):
        return urlsplit(probe_location).path in ("", "/")
    return False
