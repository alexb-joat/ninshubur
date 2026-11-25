"""Reach adapter 2: public search APIs queried with profile topics and keywords.

Queries are templates over a profile's ``topics`` then ``keywords`` (never text from a page), capped per API per run
and rotated: terms never asked come first, then the ones asked longest ago. Candidate URLs come only from one declared
JSON field per API.

Limits and access, checked 18 Nov 2026:
- HN Algolia: "We are limiting the number of API requests from a single IP to 10,000 per hour" (the text of
  https://hn.algolia.com/api, read from the page's script bundle). No key.
- GitHub repository search: "For unauthenticated requests, the rate limit allows you to make up to 10 requests per
  minute" (https://docs.github.com/en/rest/search/search). No key for repository search (code search needs one).
  Spaced 6 s apart here, so even an uncapped run stays at 10 a minute.
- Mastodon hashtag timeline: "Public. Requires app token + read:statuses if the instance has disabled public preview"
  (https://docs.joinmastodon.org/methods/timelines/); mastodon.social answered 200 without a token on 18 Nov 2026.
  Per IP: "All endpoints and methods can be called 300 times within 5 minutes"
  (https://docs.joinmastodon.org/api/rate-limits/). mastodon.social's robots.txt allows /api/v1/timelines for *.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import quote

from ...adapters.base import Fetched, FetchPlan, run_projection
from ...policy import host_of
from ...profiles import Profile
from ..store import Candidate
from . import schemas as _schemas  # noqa: F401  (registers the reach schemas)
from .net import reach_spec

TERM_RE = re.compile(r"^[\w .+#-]{1,60}$")
SKIP_TERMS = {"general"}  # profile bookkeeping topics, not subjects

HN = reach_spec("explorer-hn", {"hn.algolia.com"}, "hn_story.v1")
GITHUB = reach_spec("explorer-github", {"api.github.com"}, "github_repos.v1", min_delay_s=6.0)
MASTODON = reach_spec("explorer-mastodon", {"mastodon.social"}, "mastodon_statuses.v1")
SPECS = {"hn": HN, "github": GITHUB, "mastodon": MASTODON}
OWN_HOSTS = {"hn": {"news.ycombinator.com", "hn.algolia.com"}, "github": {"github.com", "api.github.com"},
             "mastodon": {"mastodon.social"}}


@dataclass(frozen=True)
class Query:
    api: str
    profile: str
    term: str
    plan: FetchPlan


def terms_of(profile: Profile) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in [*profile.topics, *profile.keywords]:
        t = str(t).strip()
        k = t.lower()
        if k in seen or k in SKIP_TERMS or not TERM_RE.match(t):
            continue
        seen.add(k)
        out.append(t)
    return out


def plan_for(api: str, term: str) -> FetchPlan | None:
    if api == "hn":
        return FetchPlan(url=f"https://hn.algolia.com/api/v1/search?tags=story&query={quote(term)}&hitsPerPage=30",
                         key=f"explorer:hn:{term.lower()}", max_bytes=1_000_000, purpose=f"HN stories about {term}")
    if api == "github":
        slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")[:50]
        if not slug:
            return None
        return FetchPlan(url=f"https://api.github.com/search/repositories?q=topic:{slug}&sort=stars&order=desc&per_page=30",
                         key=f"explorer:github:{slug}", max_bytes=2_000_000,
                         headers=(("Accept", "application/vnd.github+json"),), purpose=f"GitHub repositories on {slug}")
    if api == "mastodon":
        tag = re.sub(r"[^A-Za-z0-9_]+", "", term)[:30]
        if not tag or tag.isdigit():
            return None
        return FetchPlan(url=f"https://mastodon.social/api/v1/timelines/tag/{tag.lower()}?limit=40",
                         key=f"explorer:mastodon:{tag.lower()}", max_bytes=2_000_000, purpose=f"Mastodon posts tagged #{tag}")
    raise ValueError(f"unknown search API {api!r}; one of {', '.join(SPECS)}")


def plan_queries(profiles: dict[str, Profile], api: str, *, cap: int, conn: sqlite3.Connection | None = None) -> list[Query]:
    """At most ``cap`` queries for ``api``, across profiles, least recently asked first (one term asked by two
    profiles is one request, credited to the first)."""
    cands: dict[str, Query] = {}
    for name in sorted(profiles):
        for term in terms_of(profiles[name]):
            p = plan_for(api, term)
            if p is not None and p.key not in cands:
                cands[p.key] = Query(api, name, term, p)
    last: dict[str, float] = {}
    if conn is not None and cands:
        marks = ",".join("?" * len(cands))
        last = {r["key"]: r["last_fetched_at"] or 0.0 for r in
                conn.execute(f"SELECT key, last_fetched_at FROM fetch_state WHERE key IN ({marks})", list(cands))}  # noqa: S608 — fragments are code constants and '?' lists; values go as parameters
    order = sorted(cands.values(), key=lambda q: last.get(q.plan.key, -1.0))  # stable: profile, then term order
    return order[:max(0, cap)]


def parse(api: str, q: Query, fetched: Fetched, now: float) -> tuple[list[Candidate], int]:
    """(candidates, rows skipped as tainted). Only one declared URL field per API is read."""
    spec = SPECS[api]
    proj = run_projection(fetched, spec.schema, now, spec=spec)
    out: list[Candidate] = []
    tainted = 0
    for row in proj.rows:
        if row.get("_taint"):
            tainted += 1
            continue
        if api == "hn":
            url, reason = row.get("url"), f"HN story matching {q.term!r}"
        elif api == "github":
            if row.get("fork") or row.get("archived"):
                continue
            url, reason = row.get("homepage"), f"homepage of GitHub repository {row['full_name']} (topic {q.term!r})"
        else:
            url, reason = row.get("card_url"), f"link card in a Mastodon post tagged {q.term!r}"
        if not url or host_of(url) in OWN_HOSTS[api]:
            continue
        out.append(Candidate(url=url, kind=api, via=spec.name, profile=q.profile, query=q.plan.url, reason=reason))
    return out, tainted
