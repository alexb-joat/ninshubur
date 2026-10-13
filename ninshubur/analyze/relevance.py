"""Relevance of an item to a profile. Phase 1 is lexical + recency + source reputation.

When embeddings are unavailable the weights renormalise so a strong lexical match can still
clear the threshold: lexical 0.70, recency 0.20, reputation 0.10. With embeddings (Phase 2):
0.35 / 0.35 / 0.20 / 0.10 as in the plan.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..profiles import Profile

HALF_LIFE_DAYS = {"service_request": 7, "social": 7, "thread": 7, "pain": 14, "news": 14, "hiring": 21,
                  "tooling": 30, "discovery": 30, "event": 45, "incident": 1, "status_component": 1}


@dataclass(frozen=True)
class Relevance:
    score: float
    lexical: float
    recency: float
    reputation: float
    embedding: float | None
    matched: tuple[str, ...]
    negatives: tuple[str, ...]

    def parts(self) -> dict:
        return {"lexical": round(self.lexical, 3), "recency": round(self.recency, 3), "reputation": round(self.reputation, 3),
                "embedding": None if self.embedding is None else round(self.embedding, 3),
                "matched": list(self.matched), "negatives": list(self.negatives)}


def _term_re(term: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)")


def lexical(text: str, profile: Profile) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    low = (text or "").lower()
    if not low:
        return 0.0, (), ()
    matched = tuple(t for t in dict.fromkeys(profile.all_terms()) if _term_re(t).search(low))
    negatives = tuple(t for t in profile.negative_keywords if _term_re(t).search(low))
    if not matched:
        score = 0.0
    else:
        score = min(1.0, 0.45 + 0.2 * (len(matched) - 1))
    score = max(0.0, score - 0.3 * len(negatives))
    return score, matched, negatives


def recency(published_at: float | None, kind: str, now: float) -> float:
    if published_at is None:
        return 0.5
    age_days = max(0.0, (now - published_at) / 86400)
    hl = HALF_LIFE_DAYS.get(kind, 14)
    return math.pow(0.5, age_days / hl)


def score(text: str, kind: str, published_at: float | None, reputation: float, profile: Profile, now: float,
          embedding: float | None = None) -> Relevance:
    lex, matched, negatives = lexical(text, profile)
    rec = recency(published_at, kind, now)
    rep = max(0.0, min(1.0, reputation))
    if embedding is None:
        s = 0.70 * lex + 0.20 * rec + 0.10 * rep
    else:
        s = 0.35 * lex + 0.35 * embedding + 0.20 * rec + 0.10 * rep
    return Relevance(round(min(1.0, s), 4), lex, rec, rep, embedding, matched, negatives)
