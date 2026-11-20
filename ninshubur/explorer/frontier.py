"""The explorer's frontier: which URLs may be asked for, in what order, and when a site is stopped.

``offer`` is the only way in, for candidates, links and redirect targets alike (the CaMeL rule: text from a page
never picks an action, it can only propose a URL to this gate). A URL is refused when it fails the shared URL check,
names a denied host, or has a trap's shape; otherwise it is queued under its *site* (registrable domain), whose
budgets are counted here: hosts per site (wildcard-subdomain traps), URLs per shape (calendars, pagination runs),
pages per day. ``record`` is told what each fetch brought back; a site whose recent pages are near-duplicates
(generated mazes, tarpits), that answers 200 for any path (soft-404), that is a parked lander, or that yields
nothing useful for ``no_yield_pages`` pages is stopped, and none of its URLs is fetched again.

Every refusal and every stop goes to ``explorer/logs/explorer.jsonl``. Refusals of addresses or URL forms that only
make sense as an attack on this machine (IP literals, ports, userinfo, private addresses) are logged as
``ssrf_attempt`` with ``via`` (``link``, ``candidate``, ``redirect``); only attempts to *reach* one (redirects,
connect-time refusals) count toward the tripwire (``tripwire.py``).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from ..db import one, rows
from ..logs import JsonlLog
from ..policy import Policy, check_url, host_of
from . import psl, traps
from .store import explorer_home

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS explorer_sites (
        site TEXT PRIMARY KEY, hosts TEXT NOT NULL DEFAULT '[]', first_seen REAL NOT NULL,
        pages_total INTEGER NOT NULL DEFAULT 0, day TEXT, pages_day INTEGER NOT NULL DEFAULT 0,
        useful_total INTEGER NOT NULL DEFAULT 0, since_useful INTEGER NOT NULL DEFAULT 0,
        recent TEXT NOT NULL DEFAULT '[]', soft404 INTEGER, parked TEXT, probe_misses INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open', status_reason TEXT, status_at REAL,
        stage TEXT NOT NULL DEFAULT 'candidate', stage_at REAL, trial_started REAL,
        clean_fetches INTEGER NOT NULL DEFAULT 0, last_clean REAL, fingerprint TEXT, nets TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE IF NOT EXISTS explorer_frontier (
        url TEXT PRIMARY KEY, site TEXT NOT NULL, host TEXT NOT NULL, shape TEXT NOT NULL, depth INTEGER NOT NULL,
        found_from TEXT, candidate_id INTEGER, added_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        fetched_at REAL, outcome TEXT)""",
    "CREATE INDEX IF NOT EXISTS explorer_frontier_queue ON explorer_frontier(status, site, added_at)",
    """CREATE TABLE IF NOT EXISTS explorer_shapes (site TEXT NOT NULL, shape TEXT NOT NULL, n INTEGER NOT NULL,
        PRIMARY KEY(site, shape))""",
]

# Columns added after E1 (the ladder's, then the probe count); an older database gets them on first open.
SITE_COLUMNS = {"stage": "TEXT NOT NULL DEFAULT 'candidate'", "stage_at": "REAL", "trial_started": "REAL",
                "clean_fetches": "INTEGER NOT NULL DEFAULT 0", "last_clean": "REAL", "fingerprint": "TEXT",
                "nets": "TEXT NOT NULL DEFAULT '[]'", "probe_misses": "INTEGER NOT NULL DEFAULT 0"}

# Refusal codes that name an attempt to reach this machine or its network rather than a public page.
SSRF_CODES = frozenset({"url_ip_literal", "url_port", "url_numeric_host", "url_userinfo", "url_single_label",
                        "url_scheme", "private_address", "address_refused"})
RECENT_WINDOW = 10


@dataclass(frozen=True)
class Limits:
    max_url_len: int = 512
    max_depth: int = 6
    max_query_params: int = 4
    hosts_per_site: int = 4
    pages_per_site_day: int = 20
    pages_per_shape: int = 8
    max_body_bytes: int = 1_000_000
    daily_bytes: int = 50_000_000
    min_delay_s: float = 5.0
    near_duplicate_hamming: int = 3
    near_duplicate_share: float = 0.6
    no_yield_pages: int = 30
    year_min: int = 1995
    years_ahead: int = 2
    content_types: tuple[str, ...] = ("text/html", "application/json")
    max_hops: int = 2               # links followed from a candidate page, then from those pages; no further
    links_per_page: int = 20
    trial_pages_per_day: int = 5
    trial_days: int = 7
    trial_clean_fetches: int = 20
    trial_min_clean: int = 3
    admitted_stale_days: int = 30
    content_shift_bits: int = 24
    parser_memory_mb: int = 1024
    parser_cpu_s: int = 10
    parser_timeout_s: float = 20.0
    tripwire_ssrf: int = 3
    tripwire_window_s: int = 86400
    probe_tries: int = 3
    disk_quota_bytes: int = 500_000_000

    @classmethod
    def from_policy(cls, policy: Policy) -> "Limits":
        block = policy.get("explorer", default={}) or {}
        kw = {}
        for name, default in cls.__dataclass_fields__.items():  # noqa: B007
            if name in block:
                d = getattr(cls, name)
                kw[name] = tuple(block[name]) if isinstance(d, tuple) else type(d)(block[name])
        return cls(**kw)


@dataclass(frozen=True)
class Offer:
    queued: bool
    code: str | None = None  # why not, when not queued
    url: str = ""            # the canonical form that was (or would have been) queued
    site: str = ""


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        have = {r["name"] for r in rows(conn, "PRAGMA table_info(explorer_sites)")}
        for col, decl in SITE_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE explorer_sites ADD COLUMN {col} {decl}")


def to_https(url: str) -> str:
    """Links on the open web are often written http://; the explorer only ever asks over https (and the fetcher
    refuses anything else), so the https form is what gets offered. A site without TLS simply fails to connect."""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return url  # check_url names what is wrong with it
    if p.scheme.lower() == "http":
        netloc = p.netloc if port not in (80,) else p.netloc.rsplit(":", 1)[0]
        return urlunsplit(("https", netloc, p.path, p.query, p.fragment))
    return url


class Frontier:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, home: Path, *, now: Callable[[], float] = time.time,
                 limits: Limits | None = None):
        ensure_schema(conn)
        self.conn = conn
        self.policy = policy
        self.now = now
        self.limits = limits or Limits.from_policy(policy)
        self.log = JsonlLog(explorer_home(home) / "logs" / "explorer.jsonl")

    # -- in -------------------------------------------------------------------------------------------------------
    def offer(self, url: str, *, depth: int = 0, found_from: str | None = None, candidate_id: int | None = None,
              via: str | None = None, dry_run: bool = False) -> Offer:
        """Queue ``url`` or say why not. With ``dry_run`` the verdict is the same but nothing is written or logged."""
        refuse = (lambda u, site, code, detail="": Offer(False, code, str(u), site)) if dry_run else (  # noqa: E731
            lambda u, site, code, detail="": self._refuse(u, site, code, found_from, detail, via))
        url = to_https(url.strip()) if isinstance(url, str) else url
        code = check_url(url)
        if code:
            return refuse(url, "", code)
        url = traps.canonical(url)
        host = host_of(url)
        if self.policy.host_denied(host):
            return refuse(url, "", "host_denied")
        site = psl.registrable(host)
        if one(self.conn, "SELECT 1 AS x FROM explorer_frontier WHERE url=?", (url,)):
            return Offer(False, "already_known", url, site)
        lim = self.limits
        if depth > lim.max_hops:  # asked here as well as by the callers: a redirect target is a hop like any link
            return refuse(url, site, "max_hops", f"{depth} hops from a candidate")
        year_max = time.gmtime(self.now()).tm_year + lim.years_ahead
        code = traps.url_trap(url, max_len=lim.max_url_len, max_depth=lim.max_depth, max_query_params=lim.max_query_params,
                              year_min=lim.year_min, year_max=year_max)
        if code:
            return refuse(url, site, code)
        s = one(self.conn, "SELECT status, hosts FROM explorer_sites WHERE site=?", (site,)) or {"status": "open", "hosts": "[]"}
        if s["status"] != "open":
            return Offer(False, f"site_{s['status']}", url, site)
        hosts = json.loads(s["hosts"])
        if host not in hosts and len(hosts) >= lim.hosts_per_site:
            return refuse(url, site, "trap_subdomains", f"{len(hosts)} hosts already")
        shp = traps.shape(url)
        n = one(self.conn, "SELECT n FROM explorer_shapes WHERE site=? AND shape=?", (site, shp))
        if n and n["n"] >= lim.pages_per_shape:
            return refuse(url, site, "trap_shape", shp[:120])
        if dry_run:
            return Offer(True, None, url, site)
        now = self.now()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO explorer_sites(site, first_seen) VALUES (?,?)", (site, now))
            self.conn.execute("""INSERT INTO explorer_frontier(url, site, host, shape, depth, found_from, candidate_id, added_at)
                                 VALUES (?,?,?,?,?,?,?,?)""", (url, site, host, shp, depth, found_from, candidate_id, now))
            self.conn.execute("""INSERT INTO explorer_shapes(site, shape, n) VALUES (?,?,1)
                                 ON CONFLICT(site, shape) DO UPDATE SET n=n+1""", (site, shp))
            if host not in hosts:
                self.conn.execute("UPDATE explorer_sites SET hosts=? WHERE site=?", (json.dumps(hosts + [host]), site))
        return Offer(True, None, url, site)

    def _site(self, site: str) -> dict:
        self.conn.execute("INSERT OR IGNORE INTO explorer_sites(site, first_seen) VALUES (?,?)", (site, self.now()))
        return one(self.conn, "SELECT * FROM explorer_sites WHERE site=?", (site,)) or {}

    def _refuse(self, url: str, site: str, code: str, found_from: str | None, detail: str = "",
                via: str | None = None) -> Offer:
        event = "ssrf_attempt" if code in SSRF_CODES else "frontier_refused"
        self.log.write({"event": event, "url": str(url)[:300], "site": site, "code": code, "at": round(self.now(), 3),
                        "via": via or ("link" if found_from else "candidate"),
                        **({"found_from": found_from[:300]} if found_from else {}), **({"detail": detail} if detail else {})})
        return Offer(False, code, str(url), site)

    # -- out ------------------------------------------------------------------------------------------------------
    def day_cap(self, stage: str) -> int:
        """Pages a day for a site on this rung of the trial ladder: one until its first clean page, then the trial
        cap, then the admitted cap."""
        lim = self.limits
        return lim.pages_per_site_day if stage == "admitted" else lim.trial_pages_per_day if stage == "trial" else 1

    def next_batch(self, n: int) -> list[dict]:
        """Up to ``n`` queued URLs, at most one per site, oldest first, from open sites with day budget left (the
        budget depends on the site's rung, ``day_cap``)."""
        today = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        lim = self.limits
        return rows(self.conn, """
            SELECT * FROM (
              SELECT f.*, s.stage, ROW_NUMBER() OVER (PARTITION BY f.site ORDER BY f.added_at, f.url) AS k
              FROM explorer_frontier f JOIN explorer_sites s ON s.site = f.site
              WHERE f.status='queued' AND s.status='open'
                AND (s.day IS NOT ? OR s.pages_day < CASE s.stage WHEN 'admitted' THEN ? WHEN 'trial' THEN ? ELSE 1 END))
            WHERE k = 1 ORDER BY added_at, url LIMIT ?""",
                    (today, lim.pages_per_site_day, lim.trial_pages_per_day, max(n, 0)))

    def depth(self, url: str) -> int:
        """How many hops from a candidate ``url`` was found at (0 for a URL the frontier doesn't hold)."""
        r = one(self.conn, "SELECT depth FROM explorer_frontier WHERE url=?", (url,))
        return int(r["depth"]) if r else 0

    def record(self, url: str, *, status: int | None, body: bytes = b"", outcome: str = "",
               useful: bool | None = None, fingerprint: int | None = None) -> str | None:
        """Note what a fetch of ``url`` brought back. Returns the reason the site was stopped, if it was.
        ``fingerprint`` is the body's simhash when the caller has it already (the cycle hashes a page once).

        ``useful`` comes from the parser (session E2); until something says a page was useful it counts as not
        useful, so a site that never yields stops after ``no_yield_pages`` pages."""
        f = one(self.conn, "SELECT site FROM explorer_frontier WHERE url=?", (url,))
        if not f:
            raise KeyError(f"{url} is not in the frontier; offer() it first")
        site, now = f["site"], self.now()
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        s = self._site(site)
        recent = json.loads(s["recent"])
        stop: str | None = None
        parked = None
        if status == 200 and body:
            fp = fingerprint if fingerprint is not None else traps.simhash(body)
            recent = (recent + [format(fp, "016x")])[-RECENT_WINDOW:]
            parked = traps.parked(body)
            stop = self._dup_stop(recent) or (f"parked: {parked}" if parked else None)
        since = 0 if useful else s["since_useful"] + 1
        if not stop and since >= self.limits.no_yield_pages:
            stop = f"no_yield: nothing useful in {since} pages"
        with self.conn:
            self.conn.execute("UPDATE explorer_frontier SET status=?, fetched_at=?, outcome=? WHERE url=?",
                              ("fetched" if status == 200 else "failed", now, (outcome or str(status))[:120], url))
            self.conn.execute("""UPDATE explorer_sites SET pages_total=pages_total+1,
                                   pages_day=CASE WHEN day IS ? THEN pages_day+1 ELSE 1 END, day=?,
                                   useful_total=useful_total+?, since_useful=?, recent=?, parked=COALESCE(?, parked)
                                 WHERE site=?""",
                              (today, today, 1 if useful else 0, since, json.dumps(recent), parked, site))
        if stop:
            self.stop_site(site, stop)
        return stop

    def _dup_stop(self, recent: list[str]) -> str | None:
        if len(recent) < RECENT_WINDOW:
            return None
        hs = [int(h, 16) for h in recent]
        near = sum(1 for i, h in enumerate(hs) if any(traps.hamming(h, o) <= self.limits.near_duplicate_hamming
                                                      for j, o in enumerate(hs) if j != i))
        share = near / len(hs)
        if share > self.limits.near_duplicate_share:
            return f"near_duplicates: {near} of the last {len(hs)} pages"
        return None

    def note_soft404(self, site: str, status: int | None, location: str = "") -> bool:
        """Record the random-path probe for ``site``; a soft-404 site is stopped."""
        soft = traps.soft404(status, location)
        self._site(site)
        with self.conn:
            self.conn.execute("UPDATE explorer_sites SET soft404=? WHERE site=?", (int(soft), site))
        if soft:
            self.stop_site(site, f"soft_404: a random path answered {status}")
        return soft

    def note_probe_miss(self, site: str, why: str = "") -> str | None:
        """The random-path probe for ``site`` got no answer: the site is down, or its robots.txt refuses the path.

        The try uses up the site's page for today, so the next batch goes on to other sites instead of asking this
        one again every run, and after ``probe_tries`` tries without an answer the site is stopped. Returns the
        stop reason, if it was."""
        today = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        n = int(self._site(site)["probe_misses"]) + 1
        with self.conn:
            self.conn.execute("""UPDATE explorer_sites SET probe_misses=?,
                                   pages_day=CASE WHEN day IS ? THEN pages_day+1 ELSE 1 END, day=? WHERE site=?""",
                              (n, today, today, site))
        self.log.write({"event": "probe_no_answer", "site": site, "why": why[:120], "tries": n})
        if n < self.limits.probe_tries:
            return None
        reason = f"probe_no_answer: {n} probes without an answer (last: {why[:80] or 'nothing came back'})"
        self.stop_site(site, reason)
        return reason

    def stop_site(self, site: str, reason: str) -> None:
        """Stop ``site`` (created if it has no row yet, so a stop is never lost) and drop its queued URLs."""
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO explorer_sites(site, first_seen) VALUES (?,?)", (site, self.now()))
            self.conn.execute("UPDATE explorer_sites SET status='stopped', status_reason=?, status_at=? WHERE site=?",
                              (reason[:200], self.now(), site))
            self.conn.execute("UPDATE explorer_frontier SET status='dropped', outcome=? WHERE site=? AND status='queued'",
                              (f"site stopped: {reason}"[:120], site))
        self.log.write({"event": "site_stopped", "site": site, "reason": reason[:200]})
