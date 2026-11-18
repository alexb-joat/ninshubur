"""The explorer's fetch gate: the shared hardened ``Fetcher`` (DNS pinned to the checked address, 443 only, total
deadline, streamed byte cap, bounded gzip steps, MIME check before the body, robots.txt with every fail-open shut,
per-host pacing and breaker) bound to the explorer's own database, logs and caps, plus what only the explorer needs:

- a ``STOP`` file (``~/.ninshubur/explorer/STOP``) checked before *every* request the fetcher sends, robots.txt and
  redirect hops included, so the kill switch works whatever else is running;
- the frontier's verdict before anything is asked for, and its bookkeeping after;
- a redirect to another site is never followed: its target is offered to the frontier like any other link.

Network calls: only ``GET https://<host>/...`` for URLs the frontier queued, ``/robots.txt`` on those hosts, and one
random-path probe per site (``probe_soft404``). Nothing is parsed here and no body is stored; bytes go back to the
caller (the bwrap parser, session E2).
"""
from __future__ import annotations

import asyncio
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin

import httpx

from ..adapters.base import AdapterSpec, FetchPlan
from ..config import Config, SourceConfig
from ..fetch import Fetcher
from ..policy import Policy, host_of
from . import psl
from .frontier import SSRF_CODES, Frontier
from .store import explorer_home

SOURCE = SourceConfig(name="explorer", adapter="explorer", trust={"tier": 3})


class Stopped(httpx.TransportError):
    """The explorer's STOP file exists: no request is sent."""


class StopTransport(httpx.AsyncBaseTransport):
    """Refuses every request while the STOP file exists. It sits under the pinning transport, so it is asked right
    before each connection, not once per run."""

    def __init__(self, inner: httpx.AsyncBaseTransport, stop_path: Path):
        self.inner = inner
        self.stop_path = stop_path

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.stop_path.exists():
            raise Stopped(f"{self.stop_path} exists: the explorer is stopped (remove the file to resume)", request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class Outcome:
    url: str
    status: int | None
    code: str          # "ok", a refusal code, an error name, or a finding code
    body: bytes = b""
    bytes: int = 0
    stopped_site: str | None = None
    offered: list[str] = field(default_factory=list)  # redirect targets handed to the frontier


class ExplorerGate:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, cfg: Config, home: Path, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 resolve: Callable[[str], list[str]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], float] = time.time):
        self.home = explorer_home(home)
        self.stop_path = self.home / "STOP"
        self.frontier = Frontier(conn, policy, home, now=now)
        lim = self.frontier.limits
        inner = transport or httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0,
                                                      limits=httpx.Limits(max_keepalive_connections=0))
        # Logs go to explorer/logs/, the ledger and robots cache to explorer.db: nothing here touches the main crawler's.
        self.fetcher = Fetcher(conn, policy, cfg, self.home, transport=StopTransport(inner, self.stop_path),
                               sleep=sleep, now=now, resolve=resolve)
        self.fetcher.daily_bytes = min(self.fetcher.daily_bytes or lim.daily_bytes, lim.daily_bytes)
        self.max_bytes = min(lim.max_body_bytes, self.fetcher.hard_cap)
        self.expected = "|".join(lim.content_types)
        self.spec = AdapterSpec(name="explorer", kind="discovery", hosts=frozenset(), schema="", expected_content_type=self.expected,
                                base_interval_s=86400, max_interval_s=86400 * 7, default_tier=3,
                                legal_basis="public_business_info", min_delay_s=lim.min_delay_s, dynamic_hosts=True)

    @property
    def stopped(self) -> bool:
        return self.stop_path.exists()

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    async def fetch(self, url: str) -> Outcome:
        """Fetch one URL the frontier queued and record what came back."""
        if self.stopped:  # asked here too, so a stopped run doesn't count toward any host's error breaker
            return Outcome(url=url, status=None, code="stopped")
        host = host_of(url)
        plan = FetchPlan(url=url, key=f"explore:{url}", max_bytes=self.max_bytes, expected_content_type=self.expected,
                         purpose="explore")
        f = await self.fetcher.fetch(plan, SOURCE, self.spec, extra_hosts=(host,))
        out = Outcome(url=url, status=f.status, code="ok", bytes=len(f.body))
        if f.error:
            out.code = "address_refused" if f.error == "AddressRefused" else f.error.removeprefix("refused:")
            if f.error == "Stopped" or self.stopped:
                out.code = "stopped"
                return out  # left queued: nothing was asked, the next run picks it up
        elif f.findings:
            out.code = ",".join(x.code for x in f.findings)
            if any(x.code == "off_host_redirect" for x in f.findings) and f.headers.get("location"):
                target = urljoin(f.final_url or url, f.headers["location"])
                o = self.frontier.offer(target, depth=1, found_from=url)
                out.offered.append(target)
                out.code = "redirect_offered" if o.queued else f"redirect_refused:{o.code}"
        elif f.status == 200:
            out.body = f.body
        else:
            out.code = f"http_{f.status}"
        if out.code in SSRF_CODES:
            self.frontier.log.write({"event": "ssrf_attempt", "url": url[:300], "code": out.code,
                                     "detail": (f.error_detail or "")[:200]})
        out.stopped_site = self.frontier.record(url, status=f.status, body=out.body, outcome=out.code)
        return out

    async def probe_soft404(self, host: str) -> bool:
        """Ask for a random path nobody links to; 200 (or a redirect to the front page) stops the site."""
        url = f"https://{host}/ninshubur-probe-{secrets.token_hex(8)}"
        plan = FetchPlan(url=url, key=f"explore-probe:{host}", max_bytes=65_536, expected_content_type=self.expected,
                         purpose="soft-404 probe")
        f = await self.fetcher.fetch(plan, SOURCE, self.spec, extra_hosts=(host,))
        if f.error == "Stopped" or self.stopped:
            return False
        return self.frontier.note_soft404(psl.registrable(host), f.status, f.headers.get("location", ""))

    async def step(self, n: int) -> list[Outcome]:
        """Fetch up to ``n`` queued URLs, one per site, in order. Stops early when the STOP file appears."""
        out: list[Outcome] = []
        for row in self.frontier.next_batch(n):
            if self.stopped:
                break
            out.append(await self.fetch(row["url"]))
        return out
