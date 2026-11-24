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
from .config import Config, ConfigError, SourceConfig, load_config
from .db import one, rows, uj, j
from .fetch import Fetcher, reachable
from .ingest import Ingestor, IngestReport
from .policy import Policy, host_of

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
                 ingestor: Ingestor, now: Callable[[], float] = time.time, rng: random.Random | None = None,
                 reachable: Callable[[str], bool] = reachable):
        self.conn = conn
        self.policy = policy
        self.cfg = cfg
        self.home = home
        self.fetcher = fetcher
        self.ingestor = ingestor
        self._inbox = None  # the explorer hand-off's reader, made at the first tick (read_explorer_inbox)
        self.now = now
        self.rng = rng or random.Random()
        self.reachable = reachable
        self.offline_since: float | None = None
        self.jitter = float(cfg.get("scheduler", "jitter", default=0.10))
        self.max_concurrent = int(cfg.get("scheduler", "max_concurrent_sources", default=4))
        self.adapters: dict[str, Adapter] = {}
        self.after_tick = None  # optional callable run after each successful tick (digests, sinks)
        self._config_stamp = self._stamp()

    def adapter(self, name: str) -> Adapter:
        if name not in self.adapters:
            self.adapters[name] = get_adapter(name)
        return self.adapters[name]

    # -- registry sync --------------------------------------------------------------------------
    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self.cfg.path.stat() if self.cfg.path else None
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size) if st else None

    def reload_sources(self) -> bool:
        """Follow edits to config.yaml made while the daemon runs (`sources enable/disable`, `sources add`, a hand
        edit), which it used to undo on its next tick. Only the sources are reloaded; a change to any other setting is
        logged as needing a restart. A file that no longer loads keeps the running sources and is logged too."""
        stamp = self._stamp()
        if stamp is None or stamp == self._config_stamp:
            return False
        self._config_stamp = stamp
        try:
            new = load_config(path=self.cfg.path, policy=self.policy)
        except ConfigError as e:
            self.ingestor.policy_log.write({"event": "config_reload_failed", "detail": str(e)[:200]})
            return False
        changed = sorted(n for n in set(new.sources) | set(self.cfg.sources) if new.sources.get(n) != self.cfg.sources.get(n))
        restart = sorted(k for k in set(new.data) | set(self.cfg.data) if k != "sources" and new.data.get(k) != self.cfg.data.get(k))
        self.cfg.sources = new.sources
        self.cfg.data["sources"] = new.data.get("sources") or {}
        self.ingestor.policy_log.write({"event": "config_reloaded", "sources": changed,
                                        **({"restart_needed_for": restart} if restart else {})})
        return True

    def sync_sources(self) -> list[str]:
        names = []
        for src in self.cfg.sources.values():
            self.ingestor.ensure_source(src, self.adapter(src.adapter).spec)
            names.append(src.name)
        for row in rows(self.conn, "SELECT name, adapter, health FROM sources"):
            if row["adapter"] == "explorer":  # the explorer's inbox source: never fetched, not in config by design
                continue
            if row["name"] not in self.cfg.sources:
                self.ingestor.set_enabled(row["name"], False, "removed from config")
                if row["health"] in ("ok", "suspect"):  # a quarantine or a lapsed review stays on record
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
        extra_hosts: set[str] = set()
        if "events" in spec.needs:
            state["events"] = self._events_for(spec)
        if spec.dynamic_hosts:
            extra_hosts = {host_of(ev["url"]) for ev in state.get("events", []) if ev.get("url")}
        queue: list[FetchPlan] = list(adapter.plan(src, state, now))
        state.pop("events", None)  # not persisted
        if not queue:
            report.note = "adapter produced no plans (check params)"
        integrity, broken_hits = await self._crawl(src, adapter, spec, state, queue, extra_hosts, report, boosted, now)
        self._reschedule(src, spec, report, broken_hits, integrity)
        self.conn.execute("UPDATE sources SET state=? WHERE name=?", (j(state), name))
        cur = self.ingestor.source(name)
        report.health = cur["health"] if cur else "ok"
        report.next_due_at = float(cur["next_due_at"]) if cur else 0.0
        return report

    async def _crawl(self, src: SourceConfig, adapter, spec, state: dict, queue: list[FetchPlan], extra_hosts: set[str],
                     report: RunReport, boosted: bool, now: float, *, dry_run: bool = False, collect: list | None = None) -> tuple[bool, int]:
        """Fetch a queue of plans with follow-ups (<= MAX_FOLLOWUP_ROUNDS), under every policy gate.
        ``dry_run`` parses but never ingests; parsed results go to ``collect``."""
        name = src.name
        integrity = False
        broken_hits = 0
        seen_keys = {p.key for p in queue}
        for round_no in range(MAX_FOLLOWUP_ROUNDS + 1):
            nxt: list[FetchPlan] = []
            for plan in queue:
                row = self.ingestor.source(name)
                health = row["health"] if row else "ok"
                if plan.derived_from:
                    r = self.policy.allow_derived_link(plan.url, base_url=plan.derived_from, adapter=spec.name, anchor_text=plan.anchor_text)
                    if r:
                        self.fetcher.refuse(plan, name, r)
                        report.refused += 1
                        continue
                f = await self.fetcher.fetch(plan, src, spec, health=health, boosted=boosted, review_due_at=src.review_due_at(),
                                             extra_hosts=extra_hosts)
                report.fetched += 1
                codes = tuple(x.code for x in f.findings)
                if f.error and f.error.startswith("refused:"):
                    report.refused += 1
                    self._note(adapter, state, plan, ok=False, codes=(f.error,), now=now)
                    continue
                if any(x.severity == "integrity" for x in f.findings):
                    if spec.dynamic_hosts:
                        report.errors += 1
                        self.ingestor.policy_log.write({"event": "event_flagged", "source": name, "url": plan.log_url or plan.url,
                                                        "codes": list(codes), "detail": [x.detail for x in f.findings][:3]})
                        for x in f.findings:
                            if x.code == "off_host_redirect" and x.detail and "." in x.detail and not self.policy.host_denied(x.detail):
                                self.conn.execute(
                                    """INSERT INTO candidates(host, example_url, found_in_source, reason, count, first_seen, last_seen)
                                       VALUES (?,?,?,?,1,?,?) ON CONFLICT(host, found_in_source) DO UPDATE SET count=count+1, last_seen=excluded.last_seen""",
                                    (x.detail.lower(), plan.url, name, "event site moved to another host", now, now))
                        if not dry_run:
                            report.ingests.append(self.ingestor.ingest(src, spec, _flagged(f), f))
                        self._note(adapter, state, plan, ok=False, codes=codes, now=now)
                        continue
                    if not dry_run:
                        report.ingests.append(self.ingestor.integrity_event(src, f))
                    integrity = True
                    break
                if f.findings:
                    report.errors += 1
                    if not dry_run:
                        report.ingests.append(self.ingestor.ingest(src, spec, _flagged(f), f))
                    self._note(adapter, state, plan, ok=False, codes=codes, now=now)
                    continue
                if f.error or f.status is None:
                    report.errors += 1
                    self._note(adapter, state, plan, ok=False, codes=(f.error or "no_status",), now=now)
                    continue
                if f.status == 304 or (f.status == 200 and not f.changed and not dry_run):
                    report.unchanged += 1
                    self._note(adapter, state, plan, ok=True, codes=(), now=now)
                    continue
                if f.status != 200:
                    report.errors += 1
                    if f.status in BROKEN_STATUSES:
                        broken_hits += 1
                    self._note(adapter, state, plan, ok=False, codes=(f"http_{f.status}",), now=now)
                    continue
                report.changed += 1
                parsed = adapter.parse(src, f, now)
                if collect is not None:
                    collect.append((plan, f, parsed))
                if dry_run:
                    codes_after: tuple = tuple(x.code for x in parsed.findings)
                else:
                    ing = self.ingestor.ingest(src, spec, parsed, f)
                    report.ingests.append(ing)
                    codes_after = tuple(ing.codes)
                self._note(adapter, state, plan, ok=True, codes=codes_after, now=now)
                if round_no < MAX_FOLLOWUP_ROUNDS:
                    for fp in parsed.followups:
                        if fp.key not in seen_keys:
                            seen_keys.add(fp.key)
                            nxt.append(fp)
            if integrity or not nxt:
                break
            queue = nxt
        return integrity, broken_hits

    async def crawl_event(self, name: str, event: dict, *, dry_run: bool = False) -> tuple[RunReport, list]:
        """`lookup`: crawl one event now, outside the daily cap but under every other rule."""
        src = self.cfg.sources[name]
        adapter = self.adapter(src.adapter)
        spec = adapter.spec
        row = self.ingestor.ensure_source(src, spec)
        report = RunReport(source=name, health=row["health"])
        state = uj(row["state"])
        if row["health"] not in ("ok", "suspect"):
            report.note = f"not fetchable: health={row['health']}"
            return report, []
        plan = FetchPlan(url=event["url"], key=f"event_sites:{event['uid']}:home", max_bytes=2_000_000,
                         expected_content_type="text/html", purpose=f"event homepage: {event.get('title', '')[:60]}")
        collected: list = []
        now = self.now()
        # "crawl now" means re-read the pages: forget conditional-request state for this event first
        self.conn.execute("DELETE FROM fetch_state WHERE key LIKE ?", (f"event_sites:{event['uid']}:%",))
        if dry_run:
            # a dry run leaves no trace beyond the fetch log and byte ledger
            saved = dict(state)
            await self._crawl(src, adapter, spec, state, [plan], {host_of(event["url"])}, report, False, now, dry_run=True, collect=collected)
            self.conn.execute("DELETE FROM fetch_state WHERE key LIKE ?", (f"event_sites:{event['uid']}:%",))
            state = saved
        else:
            await self._crawl(src, adapter, spec, state, [plan], {host_of(event["url"])}, report, False, now, collect=collected)
            self.conn.execute("UPDATE sources SET state=? WHERE name=?", (j(state), name))
        cur = self.ingestor.source(name)
        report.health = cur["health"] if cur else "ok"
        return report, collected

    @staticmethod
    def _note(adapter, state: dict, plan: FetchPlan, *, ok: bool, codes: tuple, now: float) -> None:
        hook = getattr(adapter, "update_state", None)
        if hook:
            hook(state, plan, ok=ok, codes=tuple(codes), now=now)

    def _events_for(self, spec) -> list[dict]:
        """Events from the items table for a dynamic-host adapter: uid, url, title, dates, place."""
        out = []
        for r in rows(self.conn, """SELECT i.uid, i.url, i.title, i.attrs,
                                           (SELECT at FROM item_dates d WHERE d.item_id=i.id AND d.kind='event_start') AS start,
                                           (SELECT end_at FROM item_dates d WHERE d.item_id=i.id AND d.kind='event_start') AS end
                                    FROM items i WHERE i.kind='event' AND i.url IS NOT NULL AND i.url<>'' AND i.held=0 AND i.tainted=0"""):
            a = uj(r["attrs"])
            out.append({"uid": r["uid"], "url": r["url"], "title": r["title"], "start": r["start"], "end": r["end"],
                        "city": a.get("city"), "country": a.get("country"), "online": a.get("online")})
        return out

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

        # Sources whose adapter needs data from other sources (event_sites needs the events) run after the rest,
        # so a first run already has something to plan from.
        dependent = [n for n in names if self.adapter(self.cfg.sources[n].adapter).spec.needs]
        primary = [n for n in names if n not in dependent]
        reports = list(await asyncio.gather(*(one_source(n) for n in primary)))
        reports += list(await asyncio.gather(*(one_source(n) for n in dependent)))
        return reports

    async def network_ready(self) -> bool:
        """Whether a fixed host of any enabled source is reachable. A laptop that wakes from sleep runs the daemon
        before Wi-Fi is back; fetching then would fail every due source and back each one off for days."""
        hosts = sorted({h for s in self.cfg.sources.values() if s.enabled for h in self.adapter(s.adapter).spec.hosts})
        if not hosts:
            return True
        for host in hosts:
            if await asyncio.to_thread(self.reachable, host):
                if self.offline_since is not None:
                    self.ingestor.policy_log.write({"event": "online", "waited_s": int(self.now() - self.offline_since)})
                    self.offline_since = None
                return True
        if self.offline_since is None:
            self.offline_since = self.now()
            self.ingestor.policy_log.write({"event": "offline", "detail": f"none of {len(hosts)} source hosts is reachable; "
                                                                          "due sources wait for the network"})
        return False

    def read_explorer_inbox(self):
        """Store what the explorer handed off since the last tick (capped JSONL files only; held, Tier 3). A broken
        or missing explorer never stops the tick: the reader skips what it can't use, and a failure is logged."""
        from .explorer.handoff import Inbox
        try:
            if self._inbox is None:  # one reader for the daemon's life: a tick with nothing new opens no file
                self._inbox = Inbox(self.conn, self.policy, self.home, ingestor=self.ingestor, now=self.now)
            return self._inbox.read(self.cfg)
        except Exception as e:  # noqa: BLE001 - the explorer's output must never take main down
            self.ingestor.policy_log.write({"event": "explorer_inbox_error", "error": type(e).__name__, "detail": str(e)[:200]})
            return None

    def retention_sweep(self) -> dict | None:
        """Policy ``retention``, enforced once per ``sweep_every_s`` (an hour): see erase.sweep. None when not due."""
        from .erase import sweep_if_due
        return sweep_if_due(self.conn, self.policy, self.now(), self.ingestor.policy_log)

    async def daemon(self, *, stop: asyncio.Event | None = None, tick_s: float | None = None) -> None:
        tick = float(tick_s or self.cfg.get("scheduler", "tick_s", default=30))
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                self.reload_sources()
                self.sync_sources()
                if not self.due() or await self.network_ready():
                    await self.run_once()
                self.ingestor.decay()
                self.read_explorer_inbox()
                self.retention_sweep()
                if self.after_tick:
                    self.after_tick()
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


def health_label(row: dict) -> str:
    """Health as shown to a person: a paused source keeps its health, so "quarantined (paused)" stays visible."""
    if row["enabled"] or row["health"] == "disabled":
        return row["health"]
    return "paused" if row["health"] == "ok" else f"{row['health']} (paused)"


def status_summary(conn: sqlite3.Connection, now: float) -> dict:
    srcs = rows(conn, "SELECT name, adapter, tier, reputation, enabled, health, health_reason, interval_s, next_due_at, last_run_at, last_change_at, fail_count, reviewed_at, review_every_days FROM sources ORDER BY name")
    by_health: dict[str, int] = {}
    for s in srcs:
        by_health[health_label(s)] = by_health.get(health_label(s), 0) + 1
    held = one(conn, "SELECT COUNT(*) AS n FROM items WHERE held=1")
    tainted = one(conn, "SELECT COUNT(*) AS n FROM items WHERE tainted=1")
    quarantine = one(conn, "SELECT COUNT(*) AS n FROM quarantine WHERE ts>=?", (now - 7 * 86400,))
    cands = one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE status='new'")
    sugg = rows(conn, "SELECT status, COUNT(*) AS n FROM suggestions GROUP BY status")
    return {"sources": srcs, "by_health": by_health, "items_held": held["n"] if held else 0,
            "items_tainted": tainted["n"] if tainted else 0, "quarantine_7d": quarantine["n"] if quarantine else 0,
            "candidates_new": cands["n"] if cands else 0, "suggestions": {r["status"]: r["n"] for r in sugg}}
