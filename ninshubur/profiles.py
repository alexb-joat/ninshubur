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
    target_companies: list[dict] = field(default_factory=list)
    tools_in_use: list[dict] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)

    def all_terms(self) -> list[str]:
        return [t for t in (self.keywords + self.pains + self.industries + self.topics) if t]

    def matches_city(self, city: str | None) -> bool:
        return bool(city) and city.strip().lower() in {c.lower() for c in self.cities}

    def matches_country(self, country: str | None) -> bool:
        return bool(country) and country.strip().lower() in {c.lower() for c in self.countries}

    def company_by_board(self, board: str) -> dict | None:
        for c in self.target_companies:
            if str(c.get("greenhouse") or c.get("lever") or "").lower() == board.lower():
                return c
        return None

    def company_by_key(self, key: str) -> dict | None:
        for c in self.target_companies:
            if key in (f"company:{c.get('domain')}", f"company:{c.get('greenhouse')}", f"company:{c.get('lever')}"):
                return c
        return None

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
        home_base=p.get("home_base"), target_companies=[dict(c) for c in (p.get("target_companies") or [])],
        tools_in_use=[dict(t) for t in (p.get("tools_in_use") or [])],
        weights={str(k): float(v) for k, v in (p.get("weights") or {}).items()}, topics=_terms(p.get("topics"), "topics", name),
    )
