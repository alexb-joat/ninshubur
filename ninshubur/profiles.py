"""Profiles: one per client / ideal customer profile. Drives relevance and which builders fire."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .home import data_home


class ProfileError(Exception):
    pass


# Job-board adapters whose board token a `target_companies` entry may carry: {name: X, ashby: x}.
BOARD_ADAPTERS = ("greenhouse", "lever", "ashby")

# Every key a profile may carry. Anything else is kept in ``Profile.unknown_keys`` and named by
# `ninshubur profiles check`, so a misspelt or retired block is seen instead of silently doing nothing.
PROFILE_KEYS = ("industries", "titles", "geos", "pains", "keywords", "negative_keywords", "example_texts", "thresholds",
                "tiers", "home_base", "home_country", "target_companies", "tools_in_use", "weights", "topics", "qualify")
QUALIFY_KEYS = ("question", "subject", "must", "must_not", "labels")
DEFAULT_LABELS = ("yes", "no", "unsure")
MAX_RULES = 12


@dataclass(frozen=True)
class Qualify:
    """What `ninshubur qualify` asks about one subject, in the operator's own words.

    ``question`` is the one thing to decide; ``must`` and ``must_not`` are the rules it is decided by;
    ``labels`` are the only answers allowed, and the first one is the positive answer (the one precision
    and recall are counted for); ``subject`` is the noun for what one row is."""
    question: str
    labels: tuple[str, ...] = DEFAULT_LABELS
    subject: str = "company"
    must: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()

    @property
    def positive(self) -> str:
        return self.labels[0]


@dataclass
class Profile:
    name: str
    industries: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    pains: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    example_texts: list[str] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=lambda: {"relevance": 0.55, "tier_b": 0.70})
    tiers: dict[str, bool] = field(default_factory=lambda: {"b": False, "c": False})
    home_base: str | None = None
    home_country: str | None = None
    target_companies: list[dict] = field(default_factory=list)
    tools_in_use: list[dict] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    qualify: Qualify | None = None
    unknown_keys: tuple[str, ...] = ()

    def all_terms(self) -> list[str]:
        return [t for t in (self.keywords + self.pains + self.industries + self.topics) if t]

    def matches_city(self, city: str | None) -> bool:
        return bool(city) and city.strip().lower() in {c.lower() for c in self.cities}

    def matches_country(self, country: str | None) -> bool:
        return bool(country) and country.strip().lower() in {c.lower() for c in self.countries}

    def company_by_board(self, board: str) -> dict | None:
        for c in self.target_companies:
            if board.lower() in {str(c[a]).lower() for a in BOARD_ADAPTERS if c.get(a)}:
                return c
        return None

    def company_by_key(self, key: str) -> dict | None:
        for c in self.target_companies:
            if key in {f"company:{c.get('domain')}", *(f"company:{c.get(a)}" for a in BOARD_ADAPTERS)}:
                return c
        return None

    def company_by_name(self, name: str | None) -> dict | None:
        if not name:
            return None
        n = name.strip().lower()
        for c in self.target_companies:
            cn = str(c.get("name") or "").strip().lower()
            if cn and (cn == n or cn in n or n in cn):
                return c
        return None

    def builders_available(self) -> dict[str, str]:
        """Which suggestion kinds this profile can feed, and why not otherwise (for `profiles check`)."""
        out = {}
        out["conference_prospecting"] = ("ok" if (self.cities or self.countries or self.topics)
                                         else "needs geos.cities/countries or topics (crawled events outside them still rank at 0.5)")
        out["cfp_submit"] = "ok" if (self.topics or self.cities or self.countries or self.keywords) else "needs topics, geos or keywords"
        out["service_request_lead"] = "ok" if (self.keywords or self.pains) else "needs keywords or pains"
        if self.target_companies:
            out["hiring_signal_outreach"] = "ok"
        elif self.titles or self.keywords:
            out["hiring_signal_outreach"] = "title/keyword match only (add target_companies for company matches)"
        else:
            out["hiring_signal_outreach"] = "needs target_companies, titles or keywords"
        out["tool_outage"] = "ok" if self.tools_in_use else "needs tools_in_use"
        return out

    def weight(self, kind: str) -> float:
        return float(self.weights.get(kind, 1.0))

    def tool_ids(self) -> set[str]:
        return {str(t.get("id") or t.get("repo") or t.get("provider") or "").lower() for t in self.tools_in_use}


_TERM_RE = re.compile(r"^[\w .+#/&'-]{1,60}$")


def profiles_path(home: Path | None = None) -> Path | None:
    env = os.environ.get("NINSHUBUR_PROFILES")
    for c in ([Path(env).expanduser()] if env else []) + [(home or data_home()) / "profiles.yaml", Path("profiles.yaml")]:
        if c.is_file():
            return c
    return None


def load_qualify_file(path: Path, profile: str) -> Qualify:
    """A ``qualify:`` block kept in its own YAML file (a second prompt to compare against the profile's), validated
    exactly like the profile's own. The file holds either the block's keys or one ``qualify:`` key around them."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ProfileError(f"cannot read {path}: {e.strerror or e}") from None
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} is not valid YAML ({e})") from None
    if isinstance(raw, dict) and set(raw) == {"qualify"}:
        raw = raw["qualify"]
    q = _qualify(profile, raw)
    if q is None:
        raise ProfileError(f"{path} is empty; it needs a qualify: block with a question")
    return q


def load_profiles(home: Path | None = None, path: Path | None = None) -> dict[str, Profile]:
    path = path or profiles_path(home)
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ProfileError(f"cannot load {path}: {e}") from e
    out: dict[str, Profile] = {}
    for name, p in (raw.get("profiles") or {}).items():
        out[str(name)] = _profile(str(name), p or {})
    return out


def _terms(v: Any, what: str, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ProfileError(f"profile {name}: {what} must be a list")
    out = []
    for t in v:
        t = str(t).strip()
        if not _TERM_RE.match(t):
            raise ProfileError(f"profile {name}: bad {what} entry {t!r}")
        out.append(t)
    return out


_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
_SUBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z -]{0,39}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sentence(v: Any, what: str, name: str, *, lo: int, hi: int) -> str:
    if not isinstance(v, str):
        raise ProfileError(f"profile {name}: qualify.{what} must be text")
    if _CONTROL_RE.search(v):
        raise ProfileError(f"profile {name}: qualify.{what} holds a control character")
    text = " ".join(v.split())
    if not lo <= len(text) <= hi:
        raise ProfileError(f"profile {name}: qualify.{what} must be {lo} to {hi} characters, got {len(text)}")
    return text


def _rules(v: Any, what: str, name: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if not isinstance(v, list):
        raise ProfileError(f"profile {name}: qualify.{what} must be a list of sentences")
    if len(v) > MAX_RULES:
        raise ProfileError(f"profile {name}: qualify.{what} has {len(v)} entries, at most {MAX_RULES}")
    return tuple(_sentence(r, f"{what}[{i}]", name, lo=3, hi=300) for i, r in enumerate(v))


def _qualify(name: str, q: Any) -> Qualify | None:
    """Validate a profile's ``qualify:`` block. Unknown keys are refused, not ignored: a misspelt rule
    that quietly drops out would change every answer."""
    if q is None:
        return None
    if not isinstance(q, dict):
        raise ProfileError(f"profile {name}: qualify must be a mapping with {', '.join(QUALIFY_KEYS)}")
    extra = sorted(str(k) for k in q if k not in QUALIFY_KEYS)
    if extra:
        raise ProfileError(f"profile {name}: qualify has unknown key(s) {', '.join(extra)}; allowed: {', '.join(QUALIFY_KEYS)}")
    if "question" not in q:
        raise ProfileError(f"profile {name}: qualify needs a question (one sentence: what is to be decided)")
    labels_raw = q.get("labels", list(DEFAULT_LABELS))
    if not isinstance(labels_raw, list) or not 2 <= len(labels_raw) <= 5:
        raise ProfileError(f"profile {name}: qualify.labels must list 2 to 5 answers, the positive one first")
    # YAML reads a bare yes/no as a boolean; they are the obvious labels, so they are read back as words.
    labels = tuple({True: "yes", False: "no"}.get(v, v) if isinstance(v, bool) else str(v).strip() for v in labels_raw)
    for lab in labels:
        if not _LABEL_RE.match(lab):
            raise ProfileError(f"profile {name}: qualify label {lab!r} must be lower-case letters, digits, - or _ (24 at most)")
    if len(set(labels)) != len(labels):
        raise ProfileError(f"profile {name}: qualify.labels repeats an answer")
    subject = str(q.get("subject", "company")).strip()
    if not _SUBJECT_RE.match(subject):
        raise ProfileError(f"profile {name}: qualify.subject must be a short noun (letters, spaces, hyphens), got {subject!r}")
    return Qualify(question=_sentence(q["question"], "question", name, lo=10, hi=400), labels=labels, subject=subject,
                   must=_rules(q.get("must"), "must", name), must_not=_rules(q.get("must_not"), "must_not", name))


def _profile(name: str, p: dict) -> Profile:
    if not isinstance(p, dict):
        raise ProfileError(f"profile {name}: must be a mapping")
    geos = p.get("geos") or {}
    thresholds = {"relevance": 0.55, "tier_b": 0.70} | {k: float(v) for k, v in (p.get("thresholds") or {}).items()}
    for k, v in thresholds.items():
        if not 0.0 <= v <= 1.0:
            raise ProfileError(f"profile {name}: threshold {k} out of [0,1]")
    return Profile(
        name=name, industries=_terms(p.get("industries"), "industries", name), titles=_terms(p.get("titles"), "titles", name),
        cities=_terms(geos.get("cities"), "cities", name), countries=_terms(geos.get("countries"), "countries", name),
        pains=_terms(p.get("pains"), "pains", name), keywords=_terms(p.get("keywords"), "keywords", name),
        negative_keywords=_terms(p.get("negative_keywords"), "negative_keywords", name),
        example_texts=[str(t) for t in (p.get("example_texts") or [])], thresholds=thresholds,
        tiers={"b": bool((p.get("tiers") or {}).get("b", False)), "c": bool((p.get("tiers") or {}).get("c", False))},
        home_base=p.get("home_base"), home_country=p.get("home_country"), target_companies=[dict(c) for c in (p.get("target_companies") or [])],
        tools_in_use=[dict(t) for t in (p.get("tools_in_use") or [])],
        weights={str(k): float(v) for k, v in (p.get("weights") or {}).items()}, topics=_terms(p.get("topics"), "topics", name),
        qualify=_qualify(name, p.get("qualify")), unknown_keys=tuple(sorted(str(k) for k in p if k not in PROFILE_KEYS)),
    )
