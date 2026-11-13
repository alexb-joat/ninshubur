"""Company identity: one canonical key for every hiring item, a rollup into ``signals``, and a
list of near-miss names for a person to judge.

Merge order, strictly, first match wins:

1. **domain**: the item itself names a domain (``attrs.domain``, a company written as a domain such
   as ``Nordash.io``, or a trailing parenthetical such as ``Chronoleaf (chronoleaf.pe)``);
2. **config / profile**: a domain known from a source's ``params.companies[board].domain`` or from a
   profile's ``target_companies``, matched by the board token of the item's own adapter, or by the
   exact normalised name;
3. **manual**: the aliases file ``company_aliases.yaml`` in the data home (alias -> name or domain);
4. **name**: the normalised name.

The key is ``company:<registrable domain>`` when a domain is known, else ``company:<name slug>``.
Nothing is ever merged by similarity: :func:`dupes` lists near misses and the exact aliases-file
line that would merge them, and writes nothing.
"""
from __future__ import annotations

import re
import sqlite3
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from .analyze import timing, value
from .config import Config, ConfigError
from .db import rows, uj
from .detectors import edit_distance
from .home import data_home
from .policy import Policy
from .profiles import Profile

ALIASES_FILE = "company_aliases.yaml"
ORIGINS = ("domain", "config", "profile", "manual", "name")  # merge order; also the precedence in company_aliases
SIGNAL_KIND = "hiring"

# Dropped only as trailing tokens, never to an empty name: unambiguous words, and dotted letter forms
# ("S.A.", "B.V.", "A.S."). Bare short tokens ("AG", "SA", "Co") may be part of a name: dupes() lists them.
LEGAL_FORMS = frozenset({"inc", "incorporated", "llc", "ltd", "limited", "gmbh", "corp", "corporation", "plc", "pty"})
AMBIGUOUS_FORMS = frozenset({"ag", "sa", "as", "se", "ab", "oy", "nv", "bv", "co", "kg", "srl", "spa", "sas", "llp", "lp"})
_DOTTED_FORM = re.compile(r"^(?:[^\W\d_]\.){2,}$|^[^\W\d_]\.[^\W\d_]$")
# "Acme" and "Acme Labs" are different companies; dupes() lists the pair when the extra word is one of these.
GENERIC_WORDS = frozenset({
    "labs", "lab", "technologies", "technology", "tech", "software", "systems", "group", "holdings", "hq",
    "ai", "io", "app", "apps", "solutions", "studio", "studios", "digital", "global", "international",
    "company", "health", "data", "cloud", "security", "robotics", "analytics", "ventures", "media",
})
# Second-level public suffixes under which the registrable domain has three labels. A short fixed list,
# not the Public Suffix List: a suffix missing here (e.g. "com.pe") keeps the last two labels.
TWO_LEVEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au", "co.nz",
    "org.nz", "co.jp", "ne.jp", "or.jp", "co.kr", "com.br", "com.cn", "com.hk", "com.sg", "com.tw", "co.in",
    "co.za", "com.mx", "com.ar", "com.tr", "co.il", "com.ua", "co.id", "com.my", "com.ph", "com.vn",
})
# Hosts that give every customer a subdomain: the full host is the company ("acme.github.io").
SHARED_HOSTS = frozenset({
    "github.io", "gitlab.io", "notion.site", "herokuapp.com", "vercel.app", "netlify.app", "pages.dev", "web.app",
    "firebaseapp.com", "webflow.io", "wixsite.com", "wordpress.com", "substack.com", "carrd.co", "blogspot.com",
    "framer.website", "framer.app", "squarespace.com", "myshopify.com", "glitch.me", "fly.dev", "onrender.com",
    "azurewebsites.net", "cloudfront.net", "appspot.com", "readthedocs.io",
})
# Platforms whose host never names the company (it is in the path, or not there at all): not a company domain.
PLATFORMS = frozenset({
    "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com", "teamtailor.com", "linkedin.com", "github.com",
    "gitlab.com", "notion.so", "google.com", "t.me", "linktr.ee", "bit.ly", "x.com", "twitter.com", "facebook.com",
    "youtube.com", "medium.com", "instagram.com", "wellfound.com", "angel.co", "ycombinator.com", "smartrecruiters.com",
    "bamboohr.com", "recruitee.com", "personio.de", "breezy.hr", "jobvite.com", "myworkdayjobs.com", "docs.google.com",
    "forms.gle", "calendly.com", "discord.gg", "discord.com", "slack.com", "tinyurl.com", "goo.gl",
})
GENERIC_SECOND_LEVEL = frozenset({"com", "co", "org", "net", "gov", "edu", "ac"})
NOT_TLDS = frozenset({"js", "ts", "py", "rb"})  # "Vue.js" is a project name, not a domain
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_PAREN_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)$")
_DROP_CHARS = frozenset(".'’&")
LINE_HINT = 'write one alias per line, as  "Alias Name": canonical-name-or-domain'


class AliasesError(ConfigError):
    """The aliases file is malformed; the message names the file, the line and the fix."""


# --- normalisation -------------------------------------------------------------------------------
def normalise_domain(v: object) -> str | None:
    """Lowercase host of a domain or URL: no scheme, path, port, userinfo or leading ``www.``; None if
    it is not a plausible domain."""
    s = str(v or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0].rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    if s.startswith("www."):
        s = s[4:]
    if not s.isascii():
        try:
            s = s.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if not _DOMAIN_RE.match(s) or s.rsplit(".", 1)[-1] in NOT_TLDS:
        return None
    return s


def _suffix_len(parts: list[str]) -> int:
    if len(parts) >= 2 and (".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES
                            or (parts[-2] in GENERIC_SECOND_LEVEL and len(parts[-1]) == 2)):
        return 2
    return 1


def registrable(host: str | None) -> str | None:
    """The part of a host that names one company, or None when it names none.

    The last two labels; three under a second-level suffix (``co.uk``, ``com.pe``: a known list plus
    com/co/org/net/gov/edu/ac before a two-letter country code); the full customer host on
    :data:`SHARED_HOSTS` (``acme.github.io``); None on :data:`PLATFORMS` (``jobs.lever.co``) and for a bare
    suffix. Not the Public Suffix List."""
    if not host:
        return None
    parts = host.split(".")
    for shared in SHARED_HOSTS:
        n = shared.count(".") + 1
        if ".".join(parts[-n:]) == shared:
            return ".".join(parts[-n - 1:]) if len(parts) > n else None
    keep = _suffix_len(parts) + 1
    if len(parts) < keep:
        return None
    out = ".".join(parts[-keep:])
    if any(out == p or out.endswith("." + p) or host == p or host.endswith("." + p) for p in PLATFORMS):
        return None
    return out


def company_domain(v: object) -> str | None:
    """normalise_domain then registrable: the company domain a value names, or None."""
    return registrable(normalise_domain(v))


def _bare_domain(s: str) -> str | None:
    """A name that is itself a domain ("Nordash.io"): one token, no scheme or path."""
    s = s.strip()
    if not s or any(c.isspace() or c in "/:@" for c in s):
        return None
    return normalise_domain(s)


def split_name(raw: object) -> tuple[str, str | None]:
    """(display name, domain the name carries). A trailing parenthetical is dropped from the display
    name; when it holds a domain ("HiQ (hiq.ai)") that domain is returned. A name that is a domain
    ("Nordash.io") keeps its spelling and returns the domain."""
    s = " ".join(unicodedata.normalize("NFKC", str(raw or "")).split())
    domain = None
    m = _PAREN_RE.match(s)
    if m and m.group(1).strip():
        s, inner = m.group(1).strip(), m.group(2).strip()
        domain = normalise_domain(inner) if inner and not any(c.isspace() for c in inner) else None
    if domain is None:
        domain = _bare_domain(s)
    return s, domain


def normalise_name(raw: object) -> str:
    """Casefold + NFKC, trailing parenthetical dropped, ``. ' & `` removed, other punctuation and
    symbols turned into spaces, whitespace collapsed, trailing legal forms dropped (never to empty)."""
    s = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", split_name(raw)[0]).casefold())
    raw_tokens = [t for t in (x.strip(",;") for x in s.split()) if t]
    while len(raw_tokens) > 1:
        t = raw_tokens[-1]
        if _clean(t) in LEGAL_FORMS or _DOTTED_FORM.match(t):
            raw_tokens.pop()
        else:
            break
    return " ".join(" ".join(_clean(t) for t in raw_tokens).split())


def _clean(token: str) -> str:
    out = []
    for ch in token:
        if ch in _DROP_CHARS:
            continue
        out.append(" " if unicodedata.category(ch)[0] in "PSZC" else ch)
    return " ".join("".join(out).split())


def slug(norm: str) -> str:
    return norm.replace(" ", "-")


def name_key(norm: str) -> str:
    return f"company:{slug(norm)}"


def domain_key(domain: str) -> str:
    """``domain`` is already registrable (see :func:`company_domain`)."""
    return f"company:{domain}"


# --- aliases file --------------------------------------------------------------------------------
def aliases_path(home: Path | None = None) -> Path:
    return (home or data_home()) / ALIASES_FILE


def load_aliases(path: Path) -> dict[str, tuple[str, int]]:
    """``{normalised alias: (target, line)}``. An absent file is empty; a malformed one raises
    :class:`AliasesError` naming the file, the line and the fix."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AliasesError(f"{path}: cannot read it ({e}); make it a readable UTF-8 file, or delete it") from e
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"{path}:{mark.line + 1}" if mark is not None else str(path)
        problem = getattr(e, "problem", None) or "syntax error"
        raise AliasesError(f"{where}: not valid YAML ({problem}); {LINE_HINT}") from e
    if node is None:
        return {}
    if not isinstance(node, yaml.MappingNode):
        raise AliasesError(f"{path}:{node.start_mark.line + 1}: the file must be a mapping of alias to company, "
                           f"not a {node.id}; {LINE_HINT}")
    out: dict[str, tuple[str, int]] = {}
    for k, v in node.value:
        line = k.start_mark.line + 1
        if not isinstance(k, yaml.ScalarNode) or not str(k.value).strip():
            raise AliasesError(f"{path}:{line}: an alias must be a company name; {LINE_HINT}")
        alias = str(k.value).strip()
        if not isinstance(v, yaml.ScalarNode) or not str(v.value).strip():
            got = "nothing" if isinstance(v, yaml.ScalarNode) else f"a {v.id}"
            raise AliasesError(f"{path}:{line}: alias {alias!r} maps to {got}; give one company name or domain, "
                               f"as  {yaml.safe_dump({alias: 'acme.example'}, allow_unicode=True).strip()}")
        target = str(v.value).strip()
        norm = normalise_name(alias)
        if not norm:
            raise AliasesError(f"{path}:{line}: alias {alias!r} is empty once punctuation and legal forms are "
                               "removed; write the company's name")
        if not (_bare_domain(target) or normalise_name(target)):
            raise AliasesError(f"{path}:{line}: alias {alias!r} maps to {target!r}, which is neither a domain nor a "
                               "name; give one company name or domain")
        if norm in out and out[norm][0] != target:
            raise AliasesError(f"{path}:{line}: alias {alias!r} is the same name as line {out[norm][1]} once normalised, "
                               f"with a different target ({out[norm][0]!r} vs {target!r}); keep one of the two lines")
        out[norm] = (target, line)
    return out


# --- resolver ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Resolution:
    key: str
    name: str            # display name this item suggests
    domain: str | None
    origin: str          # one of ORIGINS
    norm: str            # normalised name ("" for a domain-only item)
    authoritative: bool  # the name comes from config, a profile or the aliases file, not the item's spelling
    trusted: bool = False  # identity from config/profile board tokens or the aliases file, never from crawled text


@dataclass(frozen=True)
class _Known:
    domain: str | None
    name: str
    origin: str


class Resolver:
    """Canonical company keys from config, profiles and the aliases file. Pure: reads no database."""

    def __init__(self, cfg: Config | None = None, profiles: dict[str, Profile] | None = None,
                 aliases: dict[str, tuple[str, int]] | None = None, aliases_file: Path | None = None):
        self.adapters = {name: s.adapter for name, s in (cfg.sources.items() if cfg else ())}
        self.aliases = aliases or {}
        self.aliases_file = aliases_file
        self.warnings: list[str] = []
        self._board: dict[tuple[str, str], list[_Known]] = {}
        self._name: dict[str, list[_Known]] = {}
        self._domain_name: dict[str, str] = {}
        self._boards: set[tuple[str, str]] = set()  # board tokens a source is configured to fetch
        for src in (cfg.sources.values() if cfg else ()):
            raw_boards = src.params.get("boards") or ([src.params["board"]] if src.params.get("board") else [])
            if isinstance(raw_boards, list):
                self._boards |= {(src.adapter, str(b).lower()) for b in raw_boards}
            companies = src.params.get("companies")
            if not isinstance(companies, dict):
                continue
            for board, meta in companies.items():
                if isinstance(meta, dict):
                    self._learn(src.adapter, str(board), str(meta.get("name") or board), meta.get("domain"), "config",
                                f"source {src.name}")
        for p in (profiles or {}).values():
            for t in p.target_companies:
                name = str(t.get("name") or "").strip()
                for adapter in ("greenhouse", "lever"):
                    if t.get(adapter):
                        self._learn(adapter, str(t[adapter]), name or str(t[adapter]), t.get("domain"), "profile",
                                    f"profile {p.name}")
                if not (t.get("greenhouse") or t.get("lever")) and name:
                    self._learn(None, None, name, t.get("domain"), "profile", f"profile {p.name}")
        self._name = {n: ks for n, ks in self._name.items() if self._unambiguous(n, ks)}

    @classmethod
    def load(cls, cfg: Config | None, profiles: dict[str, Profile] | None, home: Path | None = None) -> "Resolver":
        path = aliases_path(home)
        return cls(cfg, profiles, load_aliases(path), aliases_file=path)

    def _learn(self, adapter: str | None, board: str | None, name: str, domain: object, origin: str, where: str) -> None:
        d = company_domain(domain)
        k = _Known(d, name, origin)
        if adapter and board:
            known = self._board.setdefault((adapter, board.lower()), [])
            first = next((x for x in known if x.domain), None)
            if d and first and first.domain != d:
                self.warnings.append(f"{adapter} board {board!r}: {where} says {d}, {first.origin} said {first.domain}; "
                                     f"{first.domain} is used")
            known.append(k)
        if d:
            self._domain_name.setdefault(d, name)
            norm = normalise_name(name)
            if norm:
                self._name.setdefault(norm, []).append(k)

    def _unambiguous(self, norm: str, known: list[_Known]) -> bool:
        for origin in ("config", "profile"):
            ds = sorted({k.domain for k in known if k.origin == origin and k.domain})
            if len(ds) > 1:
                self.warnings.append(f"name {norm!r} has {len(ds)} domains in {origin} ({', '.join(ds)}); "
                                     "it is not merged by name until one is removed")
                return False
        return True

    def _by_name(self, norm: str) -> _Known | None:
        known = self._name.get(norm) or []
        return next((k for k in known if k.origin == "config"), None) or (known[0] if known else None)

    def _domain_res(self, d: str, fallback: str, origin: str, norm: str, trusted: bool) -> Resolution:
        known = self._domain_name.get(d)
        return Resolution(f"company:{d}", known or fallback, d, origin, norm, known is not None, trusted)

    def resolve(self, item: dict, adapter: str | None = None) -> Resolution | None:
        """The canonical company of one hiring item (a row of ``items``), or None without a usable name."""
        attrs = item.get("attrs")
        attrs = uj(attrs) if isinstance(attrs, str) or attrs is None else attrs
        adapter = adapter or self.adapters.get(str(item.get("source")))
        board = str(attrs.get("board") or "").strip().lower()
        raw = str(attrs.get("company") or "").strip() or board
        display, own = split_name(raw)
        own = company_domain(attrs.get("domain")) or registrable(own)
        norm = normalise_name(display)
        # 1. the item's own domain (read from content: groups the item, never makes it trusted)
        if own:
            return self._domain_res(own, display or own, "domain", norm, False)
        # 2. a domain from config or a profile: board token first, then exact name
        known = self._board.get((adapter, board), []) if adapter and board else []
        board_trusted = bool(known) or (adapter, board) in self._boards
        hit = next((k for k in known if k.domain), None)
        if hit:
            return Resolution(f"company:{hit.domain}", hit.name, hit.domain, hit.origin, normalise_name(hit.name), True, True)
        authoritative = bool(known)
        if known:
            display = known[0].name
            norm = normalise_name(display)
        if not norm:
            return None
        hit = self._by_name(norm)
        if hit and hit.domain:
            return Resolution(f"company:{hit.domain}", hit.name, hit.domain, hit.origin, norm, True, board_trusted)
        # 3. the aliases file
        if norm in self.aliases:
            return self._alias(norm, display)
        # 4. the normalised name
        return Resolution(name_key(norm), display, None, "name", norm, authoritative, board_trusted)

    def _alias(self, norm: str, display: str) -> Resolution:
        target = self.aliases[norm][0]
        d = registrable(_bare_domain(target))
        if d:
            return self._domain_res(d, display, "manual", norm, True)
        tnorm = normalise_name(target)
        hit = self._by_name(tnorm)
        if hit and hit.domain:
            return Resolution(f"company:{hit.domain}", hit.name, hit.domain, "manual", norm, True, True)
        return Resolution(name_key(tnorm), split_name(target)[0], None, "manual", norm, True, True)

    def key_for_target(self, target: dict) -> str | None:
        """The canonical key of a profile's ``target_companies`` entry."""
        d = company_domain(target.get("domain"))
        if d:
            return domain_key(d)
        name = str(target.get("name") or "").strip()
        for adapter in ("greenhouse", "lever"):
            if target.get(adapter):
                r = self.resolve({"attrs": {"board": str(target[adapter]), "company": name or str(target[adapter])}}, adapter)
                return r.key if r else None
        r = self.resolve({"attrs": {"company": name}}, "")
        return r.key if r else None


# --- reading -------------------------------------------------------------------------------------
@dataclass
class CompanySummary:
    key: str
    name: str
    domain: str | None
    origin: str
    postings: int = 0
    held: int = 0
    sources: list[str] = field(default_factory=list)
    first_at: float | None = None
    last_at: float | None = None
    spellings: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "domain": self.domain, "origin": self.origin, "postings": self.postings,
                "held": self.held, "sources": self.sources, "first_at": self.first_at, "last_at": self.last_at,
                "spellings": self.spellings}


def display_name(seen: list[tuple[Resolution, float, int]]) -> str:
    """Deterministic display name for one company: a config/profile/aliases name first, else the most
    frequent spelling; ties go to the earliest seen (first_seen_at, then item id)."""
    pool = ([s for s in seen if s[0].trusted and s[0].authoritative] or [s for s in seen if s[0].trusted]
            or [s for s in seen if s[0].authoritative] or seen)
    counts = Counter(r.name for r, _, _ in pool)
    first: dict[str, tuple[float, int]] = {}
    for r, at, iid in pool:
        first[r.name] = min(first.get(r.name, (at, iid)), (at, iid))
    return min(counts, key=lambda n: (-counts[n], first[n], n))


def _hiring_rows(conn: sqlite3.Connection, *, include_held: bool) -> list[dict]:
    # Never a person row: hiring items only, person_data=0; tainted content never names a company.
    return rows(conn, f"""SELECT id, source, attrs, published_at, first_seen_at, held FROM items
                          WHERE kind='hiring' AND tainted=0 AND person_data=0 {'' if include_held else 'AND held=0'}
                          ORDER BY first_seen_at, id""")


def companies(conn: sqlite3.Connection, resolver: Resolver, *, include_held: bool = True) -> list[CompanySummary]:
    """Every company with an untainted hiring item, most postings first. Reads only."""
    groups: dict[str, list[tuple[Resolution, dict]]] = {}
    for it in _hiring_rows(conn, include_held=include_held):
        r = resolver.resolve(it)
        if r is not None:
            groups.setdefault(r.key, []).append((r, it))
    out = []
    for key, g in groups.items():
        seen = [(r, float(it["first_seen_at"]), int(it["id"])) for r, it in g]
        ats = [float(it["published_at"] or it["first_seen_at"]) for _, it in g]
        best = min((r for r, _ in g), key=lambda r: ORIGINS.index(r.origin))
        out.append(CompanySummary(key=key, name=display_name(seen), domain=next((r.domain for r, _ in g if r.domain), None),
                                  origin=best.origin, postings=len(g), held=sum(int(it["held"]) for _, it in g),
                                  sources=sorted({it["source"] for _, it in g}), first_at=min(ats), last_at=max(ats),
                                  spellings=dict(sorted(Counter(r.name for r, _ in g).items()))))
    out.sort(key=lambda c: (-c.postings, c.key))
    return out


@dataclass(frozen=True)
class Dupe:
    a: CompanySummary
    b: CompanySummary
    distance: int
    why: str
    alias_line: str | None  # the exact line for company_aliases.yaml, or None when both sides have a domain
    hint: str


def _limit(n: int) -> int:
    """Edit distance allowed for names of length n: none up to 4 letters, 1 up to 8, else 2."""
    return 0 if n <= 4 else (1 if n <= 8 else 2)


def _names(c: CompanySummary) -> set[str]:
    names = {normalise_name(s) for s in c.spellings} | {normalise_name(c.name)}
    if c.domain:
        names.add(c.domain.split(".", 1)[0])  # "nordash.io" -> "nordash"
    return {n for n in names if n}


def _extra_word(short: str, long_: str) -> str | None:
    """The trailing word ``long_`` adds to ``short`` if it is generic or an ambiguous legal form."""
    if len(short) >= 3 and long_.startswith(short + " "):
        w = long_[len(short) + 1:]
        if w in GENERIC_WORDS or w in AMBIGUOUS_FORMS:
            return w
    return None


def dupes(found: list[CompanySummary]) -> list[Dupe]:
    """Near-miss pairs of different keys. Merges nothing and writes nothing.

    Same name, a generic word or short legal form added ("Acme" / "Acme Labs", "Acme" / "Acme AG"), or a
    small edit distance (:func:`_limit`). Names are bucketed by length, so only pairs whose length differs
    by at most the largest limit are compared; the result is the same as comparing every pair."""
    owners: dict[str, set[int]] = {}
    for i, c in enumerate(found):
        for n in _names(c):
            owners.setdefault(n, set()).add(i)
    best: dict[tuple[int, int], tuple[int, str]] = {}

    def offer(i: int, j: int, hit: tuple[int, str]) -> None:
        if i == j:
            return
        k = (min(i, j), max(i, j))
        if k not in best or hit[0] < best[k][0]:
            best[k] = hit

    for n, idx in owners.items():
        for i in idx:
            for j in idx:
                if i < j:
                    offer(i, j, (0, "same name"))
        for w in GENERIC_WORDS | AMBIGUOUS_FORMS:
            longer = owners.get(f"{n} {w}")
            if longer and _extra_word(n, f"{n} {w}"):
                for i in idx:
                    for j in longer:
                        offer(i, j, (1, f"'{w}' added"))
    by_len: dict[int, list[str]] = {}
    for n in owners:
        by_len.setdefault(len(n), []).append(n)
    for la in sorted(by_len):
        if _limit(la) == 0:
            continue
        for lb in range(la, la + 3):
            for x in by_len[la]:
                for y in by_len.get(lb, []):
                    if lb == la and y <= x:
                        continue
                    lim = _limit(la)
                    if lb - la > lim:
                        continue
                    d = edit_distance(x, y, lim)
                    if 0 < d <= lim:
                        for i in owners[x]:
                            for j in owners[y]:
                                offer(i, j, (d, f"edit distance {d}"))
    out = [_dupe(found[i], found[j], *hit) for (i, j), hit in best.items()]
    out.sort(key=lambda d: (d.distance, d.a.key, d.b.key))
    return out


def _dupe(a: CompanySummary, b: CompanySummary, distance: int, why: str) -> Dupe:
    if a.domain and b.domain:
        return Dupe(a, b, distance, why, None, "both have a domain: if they are one company, correct the domain in "
                                                "config.yaml or profiles.yaml")
    # The side without a domain is redirected; between two names, the one with fewer postings.
    if a.domain or (not b.domain and (a.postings, b.key) > (b.postings, a.key)):
        a, b = b, a
    target = b.domain or b.name
    line = yaml.safe_dump({a.name: target}, allow_unicode=True, default_flow_style=False, width=1000).strip()
    return Dupe(a, b, distance, why, line, f"add to {ALIASES_FILE} if they are one company")


# --- rollup --------------------------------------------------------------------------------------
@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass
class RollupReport:
    signals: int = 0
    written: int = 0
    removed: int = 0
    aliases: int = 0


def rollup(conn: sqlite3.Connection, resolver: Resolver, policy: Policy, now: float | None = None) -> RollupReport:
    """Fill ``signals`` (kind ``hiring``) and ``company_aliases`` from the hiring items. Idempotent:
    rows already right are left alone, rows whose item is gone, tainted or held are removed.

    One signal per untainted, not-held hiring item without person data. ``signals`` is
    UNIQUE(kind, entity_key, at, source), so postings of one company published in the same second by
    the same source share one row (the lowest item id)."""
    now = time.time() if now is None else now
    srcs = {r["name"]: r for r in rows(conn, "SELECT name, tier, reputation FROM sources")}
    want: dict[tuple[str, float, str], tuple] = {}
    aliases: dict[str, tuple[str, str]] = {}
    alias_rows: list[tuple[str, str, str]] = []
    for it in _hiring_rows(conn, include_held=False):
        r = resolver.resolve(it)
        if r is None:
            continue
        attrs = uj(it["attrs"])
        s = srcs.get(it["source"])
        tier = int(attrs.get("tier", s["tier"] if s else 3))
        conf = value.confidence(float(s["reputation"]) if s else 0.3, 1.0, 1, tier, policy)
        at = float(it["published_at"] or it["first_seen_at"])
        w = timing.hiring_post(at)
        k = (r.key, at, it["source"])
        if k not in want or it["id"] < want[k][0]:
            want[k] = (int(it["id"]), w.start, w.end, conf)
        if r.norm:
            alias_rows.append((f"name:{slug(r.norm)}", r.key, r.origin))
        board = str(attrs.get("board") or "").strip().lower()
        adapter = resolver.adapters.get(it["source"])
        if board and adapter and r.origin in ("config", "profile"):
            alias_rows.append((f"board:{adapter}:{board}", r.key, r.origin))
    for norm in resolver.aliases:
        res = resolver._alias(norm, norm)
        alias_rows.append((f"name:{slug(norm)}", res.key, "manual"))
    for alias, key, origin in sorted(alias_rows, key=lambda x: (ORIGINS.index(x[2]), x[0], x[1])):
        aliases.setdefault(alias, (key, origin))  # the earlier step of the merge order wins

    rep = RollupReport(signals=len(want), aliases=len(aliases))
    with _tx(conn):
        have = {(r["entity_key"], r["at"], r["source"]): r for r in
                rows(conn, "SELECT * FROM signals WHERE kind=?", (SIGNAL_KIND,))}
        for k, r in have.items():
            if k not in want:
                conn.execute("DELETE FROM signals WHERE id=?", (r["id"],))
                rep.removed += 1
        for (key, at, source), (iid, ws, we, conf) in want.items():
            old = have.get((key, at, source))
            if old and (old["item_id"], old["window_start"], old["window_end"], old["confidence"]) == (iid, ws, we, conf):
                continue
            conn.execute("""INSERT INTO signals(kind, entity_key, at, window_start, window_end, item_id, source, confidence)
                            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(kind, entity_key, at, source) DO UPDATE SET
                            window_start=excluded.window_start, window_end=excluded.window_end, item_id=excluded.item_id,
                            confidence=excluded.confidence""", (SIGNAL_KIND, key, at, ws, we, iid, source, conf))
            rep.written += 1
        cur = {r["alias"]: (r["company_key"], r["origin"]) for r in rows(conn, "SELECT alias, company_key, origin FROM company_aliases")}
        for alias in cur.keys() - aliases.keys():
            conn.execute("DELETE FROM company_aliases WHERE alias=?", (alias,))
        for alias, (key, origin) in aliases.items():
            if cur.get(alias) != (key, origin):
                conn.execute("""INSERT INTO company_aliases(alias, company_key, origin, created_at) VALUES (?,?,?,?)
                                ON CONFLICT(alias) DO UPDATE SET company_key=excluded.company_key, origin=excluded.origin""",
                             (alias, key, origin, now))
    return rep
