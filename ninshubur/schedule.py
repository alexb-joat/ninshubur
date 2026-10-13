"""Scheduler: which admitted sources are due, run one source end to end, reschedule.

Phase 1 uses growth/backoff only (no bandit): changed -> base interval; unchanged -> x1.5 up to
max; error -> base x 2^fail up to max; +-jitter. Follow-ups are limited to two rounds and are
always templated from identifiers by the adapter.
"""
from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adapters.base import Adapter, FetchPlan, get_adapter
from .config import Config, SourceConfig
from .db import one, rows, uj, j
from .fetch import Fetcher
from .ingest import Ingestor, IngestReport
from .policy import Policy

BROKEN_STATUSES = {403, 404, 410}
BROKEN_AFTER = 5
MAX_FOLLOWUP_ROUNDS = 2


@dataclass
class RunReport:
    source: str
    fetched: int = 0
    changed: int = 0
    unchanged: int = 0
    refused: int = 0
    errors: int = 0
    ingests: list[IngestReport] = field(default_factory=list)
    health: str = "ok"
    next_due_at: float = 0.0
    note: str = ""

    @property
    def stored(self) -> int:
        return sum(i.stored for i in self.ingests)

    @property
    def dropped(self) -> int:
        return sum(i.dropped for i in self.ingests)

    @property
    def held(self) -> int:
        return sum(i.held for i in self.ingests)


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, cfg: Config, home: Path, fetcher: Fetcher,
                 ingestor: Ingestor, now: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.conn = conn
        self.policy = policy
        self.cfg = cfg
        self.home = home
        self.fetcher = fetcher
        self.ingestor = ingestor
        self.now = now
        self.rng = rng or random.Random()
        self.jitter = float(cfg.get("scheduler", "jitter", default=0.10))
        self.max_concurrent = int(cfg.get("scheduler", "max_concurrent_sources", default=4))
        self.adapters: dict[str, Adapter] = {}

    def adapter(self, name: str) -> Adapter:
        if name not in self.adapters:
            self.adapters[name] = get_adapter(name)
        return self.adapters[name]

    # -- registry sync --------------------------------------------------------------------------
    def sync_sources(self) -> list[str]:
        names = []
        for src in self.cfg.sources.values():
            self.ingestor.ensure_source(src, self.adapter(src.adapter).spec)
            names.append(src.name)
        for row in rows(self.conn, "SELECT name, health FROM sources"):
            if row["name"] not in self.cfg.sources and row["health"] != "disabled":
                self.ingestor.set_health(row["name"], "disabled", "removed from config")
        return names

    def due(self, now: float | None = None, force: bool = False) -> list[dict]:
        now = self.now() if now is None else now
        out = []
        for row in rows(self.conn, "SELECT * FROM sources WHERE enabled=1 AND health IN ('ok','suspect') ORDER BY next_due_at"):
            src = self.cfg.sources.get(row["name"])
            if src is None:
                continue
            due_at = src.review_due_at()
            if due_at is not None and due_at <= now:
                self.ingestor.set_health(row["name"], "review_overdue", "trust review overdue (H8)")
                continue
            if force or row["next_due_at"] <= now:
                out.append(row)
        return out

    # -- one source -----------------------------------------------------------------------------
    async def run_source(self, name: str, *, force: bool = False, boosted: bool = False) -> RunReport:
        src = self.cfg.sources[name]
        adapter = self.adapter(src.adapter)
        spec = adapter.spec
        row = self.ingestor.ensure_source(src, spec)
        report = RunReport(source=name, health=row["health"])
        now = self.now()
        if row["health"] not in ("ok", "suspect"):
            report.note = f"not fetchable: health={row['health']}"
            return report
        state = uj(row["state"])
        queue: list[FetchPlan] = list(adapter.plan(src, state, now))
        if not queue:
            report.note = "adapter produced no plans (check params)"
        integrity = False
        broken_hits = 0
        for round_no in range(MAX_FOLLOWUP_ROUNDS + 1):
            nxt: list[FetchPlan] = []
            for plan in queue:
                health = (self.ingestor.source(name) or row)["health"]
                f = await self.fetcher.fetch(plan, src, spec, health=health, boosted=boosted, review_due_at=src.review_due_at())
                report.fetched += 1
                if f.error and f.error.startswith("refused:"):
                    report.refused += 1
                    continue
                if any(x.severity == "integrity" for x in f.findings):
                    report.ingests.append(self.ingestor.integrity_event(src, f))
                    integrity = True
                    break
                if f.findings:  # binary magic etc. at transport level but not integrity
                    report.errors += 1
                    report.ingests.append(self.ingestor.ingest(src, spec, _flagged(f), f))
                    continue
                if f.error or f.status is None:
                    report.errors += 1
                    continue
                if f.status == 304 or (f.status == 200 and not f.changed):
                    report.unchanged += 1
                    continue
                if f.status != 200:
                    report.errors += 1
                    if f.status in BROKEN_STATUSES:
                        broken_hits += 1
                    continue
                report.changed += 1
                parsed = adapter.parse(src, f, now)
                report.ingests.append(self.ingestor.ingest(src, spec, parsed, f))
                if round_no < MAX_FOLLOWUP_ROUNDS:
                    nxt.extend(p for p in parsed.followups if p.key != plan.key)
            if integrity or not nxt:
                break
            queue = nxt
        self._reschedule(src, spec, report, broken_hits, integrity)
        cur = self.ingestor.source(name)
        report.health = cur["health"] if cur else "ok"
        report.next_due_at = float(cur["next_due_at"]) if cur else 0.0
        return report

    def _reschedule(self, src: SourceConfig, spec, report: RunReport, broken_hits: int, integrity: bool) -> None:
        row = self.ingestor.source(src.name)
        if row is None:
            return
        base = int(row["base_interval_s"])
        mx = int(row["max_interval_s"])
        interval = int(row["interval_s"]) or base
        fail = int(row["fail_count"])
        now = self.now()
        if report.changed > 0 and report.errors == 0:
            interval, fail = base, 0
        elif report.errors > 0 or report.refused == report.fetched and report.fetched > 0:
            fail += 1
            interval = min(mx, base * (2 ** min(fail, 8)))
        elif report.unchanged > 0:
            interval, fail = min(mx, int(interval * 1.5)), 0
        else:
            interval = min(mx, max(base, interval))
        jittered = interval * (1 + self.rng.uniform(-self.jitter, self.jitter))
        last_change = now if report.changed else row["last_change_at"]
        self.conn.execute("UPDATE sources SET interval_s=?, next_due_at=?, last_run_at=?, last_change_at=?, fail_count=?, updated_at=? WHERE name=?",
                          (interval, now + jittered, now, last_change, fail, now, src.name))
        if broken_hits and fail >= BROKEN_AFTER:
            self.ingestor.set_health(src.name, "broken", f"{fail} consecutive 403/404/410 responses")

    # -- ticks ----------------------------------------------------------------------------------
    async def run_once(self, *, force: bool = False, only: str | None = None) -> list[RunReport]:
        self.sync_sources()
        names = [only] if only else [r["name"] for r in self.due(force=force)]
        if only and only not in self.cfg.sources:
            raise KeyError(f"unknown source {only!r}")
        sem = asyncio.Semaphore(max(1, self.max_concurrent))

        async def one_source(n: str) -> RunReport:
            async with sem:
                return await self.run_source(n, force=force)

        return list(await asyncio.gather(*(one_source(n) for n in names)))

    async def daemon(self, *, stop: asyncio.Event | None = None, tick_s: float | None = None) -> None:
        tick = float(tick_s or self.cfg.get("scheduler", "tick_s", default=30))
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.run_once()
                self.ingestor.decay()
            except Exception as e:  # noqa: BLE001 - a daemon must survive a bad tick
                self.ingestor.policy_log.write({"event": "tick_error", "error": type(e).__name__, "detail": str(e)[:200]})
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass


def _flagged(f):
    """Wrap transport-level non-integrity findings (e.g. binary magic) as a document finding."""
    from .adapters.base import ParseResult
    from .schemas import Projection
    p = Projection()
    p.findings.extend(f.findings)
    return ParseResult(projection=p)


def status_summary(conn: sqlite3.Connection, now: float) -> dict:
    srcs = rows(conn, "SELECT name, adapter, tier, reputation, health, health_reason, interval_s, next_due_at, last_run_at, last_change_at, fail_count, reviewed_at, review_every_days FROM sources ORDER BY name")
    by_health: dict[str, int] = {}
    for s in srcs:
        by_health[s["health"]] = by_health.get(s["health"], 0) + 1
    held = one(conn, "SELECT COUNT(*) AS n FROM items WHERE held=1")
    tainted = one(conn, "SELECT COUNT(*) AS n FROM items WHERE tainted=1")
    quarantine = one(conn, "SELECT COUNT(*) AS n FROM quarantine WHERE ts>=?", (now - 7 * 86400,))
    cands = one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE status='new'")
    sugg = rows(conn, "SELECT status, COUNT(*) AS n FROM suggestions GROUP BY status")
    return {"sources": srcs, "by_health": by_health, "items_held": held["n"] if held else 0,
            "items_tainted": tainted["n"] if tainted else 0, "quarantine_7d": quarantine["n"] if quarantine else 0,
            "candidates_new": cands["n"] if cands else 0, "suggestions": {r["status"]: r["n"] for r in sugg}}
