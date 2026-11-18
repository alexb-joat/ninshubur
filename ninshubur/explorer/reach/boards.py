"""Reach adapter 3: job-board tokens seen in links the main database already holds, each probed alone on the
platform's own API host.

A token is an identifier, templated into a fixed API URL; the link it was read from is never fetched. Links come from
the main crawler's logged candidates and from items that are neither tainted nor held. Tokens already in config or in
a profile's ``target_companies`` are skipped. One request per token, so a dead token can only reject itself.

A board that answers with open jobs becomes a ``trial`` candidate; 404 or no jobs is ``rejected``; anything else
(a 5xx, a refusal, the network) leaves it a candidate for the next run. Config is never written: the run prints the
YAML lines to add (applying them belongs to the settings engine).

A probe uses the trust block of the configured source that reads the same platform, when there is one (see
``probe_source``).

Network: ``boards-api.greenhouse.io``, ``api.ashbyhq.com``, ``api.lever.co``, one GET per probed token.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass

from ...adapters.ashby import BOARD_RE as ASHBY_RE
from ...adapters.base import FetchPlan, Fetched, run_projection
from ...adapters.greenhouse import BOARD_RE as GREENHOUSE_RE
from ...config import Config, SourceConfig
from ...profiles import BOARD_ADAPTERS, Profile
from . import schemas as _schemas  # noqa: F401  (registers lever_postings.v1)
from .net import reach_source, reach_spec

LINK_RE = re.compile(
    r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?(?P<greenhouse>[A-Za-z0-9_-]{1,64})"
    r"|https?://jobs\.ashbyhq\.com/(?P<ashby>[A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_.%-])"
    r"|https?://jobs\.lever\.co/(?P<lever>[A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_.%-])")
NOT_TOKENS = {"embed", "v1", "jobs", "api"}
TOKEN_RE = {"greenhouse": GREENHOUSE_RE, "ashby": ASHBY_RE, "lever": re.compile(r"^[A-Za-z0-9_-]{1,64}$")}

SPECS = {
    "greenhouse": reach_spec("explorer-board-greenhouse", {"boards-api.greenhouse.io"}, "greenhouse_jobs.v1"),
    "ashby": reach_spec("explorer-board-ashby", {"api.ashbyhq.com"}, "ashby_jobs.v1"),
    "lever": reach_spec("explorer-board-lever", {"api.lever.co"}, "lever_postings.v1"),
}


@dataclass(frozen=True)
class Token:
    platform: str
    token: str
    seen: int
    example: str


def api_url(platform: str, token: str) -> str:
    if platform == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    if platform == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"
    if platform == "lever":
        return f"https://api.lever.co/v0/postings/{token}?mode=json&limit=100"
    raise ValueError(f"unknown board platform {platform!r}; one of {', '.join(SPECS)}")


def probe_source(cfg: Config, platform: str) -> SourceConfig:
    """The probe's source record. When config already reads this platform, its reviewed trust block is used (same API
    host, already reviewed by a person: e.g. ``robots_reviewed_status: 401`` for api.ashbyhq.com, whose robots.txt
    answers 401); otherwise the explorer's own Tier 3 record, under which a 401/403 robots.txt refuses the probe."""
    spec = SPECS[platform]
    reviewed = next((s for _, s in sorted(cfg.sources.items()) if s.adapter == platform and s.trust), None)
    if reviewed is None:
        return reach_source(spec)
    return SourceConfig(name=spec.name, adapter=spec.name, trust=dict(reviewed.trust))


def probe_plan(t: Token) -> FetchPlan:
    return FetchPlan(url=api_url(t.platform, t.token), key=f"explorer:board:{t.platform}:{t.token.lower()}",
                     max_bytes=4_000_000, purpose=f"probe {t.platform} board {t.token}")


def tokens_in(text: str) -> list[tuple[str, str, str]]:
    """(platform, token, link) for every board link in ``text``."""
    out = []
    for m in LINK_RE.finditer(text or ""):
        platform = next(k for k in ("greenhouse", "ashby", "lever") if m.group(k))
        tok = m.group(platform)
        if platform == "greenhouse":  # Greenhouse tokens are lowercase; its links are not always
            tok = tok.lower()
        if tok.lower() in NOT_TOKENS or not TOKEN_RE[platform].match(tok):
            continue
        out.append((platform, tok, m.group(0)))
    return out


def known_tokens(cfg: Config, profiles: dict[str, Profile]) -> set[tuple[str, str]]:
    known: set[tuple[str, str]] = set()
    for src in cfg.sources.values():
        if src.adapter in BOARD_ADAPTERS:
            known |= {(src.adapter, str(b).lower()) for b in src.params.get("boards", []) or []}
    for p in profiles.values():
        for c in p.target_companies:
            known |= {(a, str(c[a]).lower()) for a in BOARD_ADAPTERS if c.get(a)}
    return known


def find_tokens(main: sqlite3.Connection, known: set[tuple[str, str]]) -> list[Token]:
    """Tokens seen in the main database's links, most often seen first; known ones left out. Ashby and Lever tokens
    differ in case across links ("PostHog", "posthog"); they are one board, kept in the spelling seen most."""
    counts: Counter = Counter()
    spelling: dict[tuple[str, str], Counter] = {}
    example: dict[tuple[str, str], str] = {}
    texts = [r[0] for r in main.execute("SELECT example_url FROM candidates")]
    for url, text in main.execute("SELECT url, text FROM items WHERE tainted=0 AND held=0"):
        texts += [url, text]
    for t in texts:
        for platform, tok, link in tokens_in(t or ""):
            k = (platform, tok.lower())
            counts[k] += 1
            spelling.setdefault(k, Counter())[tok] += 1
            example.setdefault(k, link)
    return [Token(p, spelling[(p, low)].most_common(1)[0][0], n, example[(p, low)])
            for (p, low), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if (p, low) not in known]


def judge(platform: str, f: Fetched, now: float) -> tuple[str | None, str]:
    """(new status or None to leave it a candidate, reason)."""
    if f.status == 404:
        return "rejected", "no such board (404)"
    if not f.ok:
        return None, f.error or ",".join(x.code for x in f.findings) or f"HTTP {f.status}"
    proj = run_projection(f, SPECS[platform].schema, now, spec=SPECS[platform])
    if proj.findings:
        return None, "answer is not the expected JSON: " + ",".join(x.code for x in proj.findings)
    n = len(proj.rows)
    return ("trial", f"{n} open job" + ("s" if n != 1 else "")) if n else ("rejected", "board answers but lists no open jobs")


def yaml_preview(cfg: Config, trials: list[tuple[Token, str]], today: str) -> list[str]:
    """The lines Alex would add to config.yaml, grouped by the source that reads each platform."""
    lines: list[str] = []
    for platform in ("greenhouse", "ashby", "lever"):
        group = [(t, why) for t, why in trials if t.platform == platform]
        if not group:
            continue
        names = sorted(n for n, s in cfg.sources.items() if s.adapter == platform)
        if platform == "lever" and not names:
            lines.append(f"# {len(group)} Lever board(s) answer, but Ninshubur has no Lever adapter yet: "
                         + ", ".join(t.token for t, _ in group))
            continue
        where = f"sources.{names[0]}.params.boards" if names else f"a new source with `adapter: {platform}`, params.boards"
        lines.append(f"# add under {where} in config.yaml:")
        lines += [f"      - {t.token}   # {why}, probed {today}, seen in {t.seen} link(s)" for t, why in group]
    return lines
