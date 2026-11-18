"""One reach run: gather candidates from the chosen adapters into the explorer database, or (dry run) list what
would be asked without a single request."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ...config import Config
from ...policy import Policy
from ...profiles import Profile
from .. import store
from ..store import Added
from . import logged, search
from .net import explorer_fetcher, reach_source

ADAPTERS = ("logged", "hn", "github", "mastodon", "boards")
DEFAULT_MAX_QUERIES = 4  # per search API per run


@dataclass
class ReachReport:
    added: dict[str, Added] = field(default_factory=dict)  # candidate kind -> counts
    planned: list[tuple[str, str]] = field(default_factory=list)  # (adapter, what would be asked)
    notes: list[str] = field(default_factory=list)
    requests: int = 0


@dataclass
class Ctx:
    home: Path
    policy: Policy
    cfg: Config
    profiles: dict[str, Profile]
    now: Callable[[], float]
    dry_run: bool
    max_queries: int = DEFAULT_MAX_QUERIES
    transport: Any = None
    resolve: Any = None
    sleep: Any = None


def _merge(report: ReachReport, kind: str, a: Added) -> None:
    cur = report.added.setdefault(kind, Added())
    cur.new, cur.seen_again, cur.refused = cur.new + a.new, cur.seen_again + a.seen_again, cur.refused + a.refused


def run_logged(ctx: Ctx, conn: sqlite3.Connection | None, report: ReachReport) -> None:
    main = logged.open_main_readonly(ctx.home)
    try:
        cands = logged.logged_links(main)
    finally:
        main.close()
    if ctx.dry_run:
        report.planned.append(("logged", f"logged hosts in the main database: {len(cands)} (no request)"))
        return
    _merge(report, "logged_link", store.add_candidates(conn, cands, ctx.now()))


async def run_search(ctx: Ctx, conn: sqlite3.Connection | None, report: ReachReport, api: str) -> None:
    queries = search.plan_queries(ctx.profiles, api, cap=ctx.max_queries, conn=conn)
    if not queries:
        report.notes.append(f"{api}: no usable topic or keyword in the profiles (profiles.yaml: topics, keywords)")
        return
    if ctx.dry_run:
        report.planned += [(api, f"GET {q.plan.url}  [{q.profile}]") for q in queries]
        return
    spec = search.SPECS[api]
    src = reach_source(spec)
    fetcher = explorer_fetcher(conn, ctx.policy, ctx.cfg, ctx.home, transport=ctx.transport, resolve=ctx.resolve,
                               now=ctx.now, sleep=ctx.sleep)
    try:
        for q in queries:
            f = await fetcher.fetch(q.plan, src, spec)
            report.requests += 1
            if f.status == 304:
                _merge(report, api, Added())
                continue
            if not f.ok:
                why = f.error or ",".join(x.code for x in f.findings) or f"HTTP {f.status}"
                report.notes.append(f"{api}: {q.plan.url} gave no answer to use ({why}); see explorer/logs/fetch.jsonl")
                continue
            cands, tainted = search.parse(api, q, f, ctx.now())
            if tainted:
                report.notes.append(f"{api}: {tainted} rows skipped as tainted for {q.term!r}")
            _merge(report, api, store.add_candidates(conn, cands, ctx.now()))
    finally:
        await fetcher.aclose()


def _readonly_explorer(home: Path) -> sqlite3.Connection | None:
    """A dry run still plans in rotation order when the explorer has run before, without opening it for writing."""
    path = home / "explorer" / store.DB_NAME
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


async def _reach(ctx: Ctx, only: str | None, report: ReachReport) -> None:
    conn = _readonly_explorer(ctx.home) if ctx.dry_run else store.connect(ctx.home)
    try:
        if only in (None, "logged"):
            run_logged(ctx, conn, report)
        for api in ("hn", "github", "mastodon"):
            if only in (None, api):
                await run_search(ctx, conn, report, api)
    finally:
        if conn is not None:
            conn.close()


def reach(ctx: Ctx, only: str | None = None) -> ReachReport:
    """A dry run opens nothing for writing: no explorer database is created and no request is made."""
    if only is not None and only not in ADAPTERS:
        raise ValueError(f"unknown reach adapter {only!r}; one of {', '.join(ADAPTERS)}")
    report = ReachReport()
    asyncio.run(_reach(ctx, only, report))
    return report
