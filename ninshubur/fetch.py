"""The only module that opens a network connection.

Per FetchPlan: policy gate -> robots.txt -> per-host pacing -> conditional request with
``follow_redirects=False`` -> streamed read under a byte cap with a decompression guard ->
transport detectors -> fetch_state / ledger / fetches row / fetch.jsonl. Nothing here ever
follows a URL that came out of content; adapters template URLs from identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from . import detectors
from .adapters.base import AdapterSpec, FetchPlan, Fetched
from .config import Config, SourceConfig
from .db import ledger_add, ledger_sum
from .logs import JsonlLog
from .policy import Policy, Refusal, check_url, host_of

HARD_CAP = 8_000_000
ROBOTS_CAP = 512_000
REDIRECT_CODES = {301, 302, 303, 307, 308}
BREAKER_ERRORS = 5
BREAKER_WINDOW_S = 600
BREAKER_PAUSE_S = 3600


@dataclass
class HostState:
    last_request_at: float = 0.0
    retry_after_until: float = 0.0
    errors: list[float] = field(default_factory=list)
    paused_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class HostGate:
    """Serialises requests per host and enforces the slowest of every delay rule."""

    def __init__(self, per_host_rps: float, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], float] = time.time):
        self.min_gap = 1.0 / max(0.01, per_host_rps)
        self.sleep = sleep
        self.now = now
        self.hosts: dict[str, HostState] = {}

    def state(self, host: str) -> HostState:
        return self.hosts.setdefault(host, HostState())

    def paused(self, host: str) -> float:
        st = self.state(host)
        return max(0.0, st.paused_until - self.now())

    async def wait(self, host: str, min_delay: float, crawl_delay: float | None) -> float:
        st = self.state(host)
        gap = max(self.min_gap, min_delay, crawl_delay or 0.0)
        now = self.now()
        due = max(st.last_request_at + gap, st.retry_after_until)
        delay = max(0.0, due - now)
        if delay > 0:
            await self.sleep(delay)
        st.last_request_at = self.now()
        return delay

    def note_error(self, host: str) -> bool:
        st = self.state(host)
        now = self.now()
        st.errors = [t for t in st.errors if t > now - BREAKER_WINDOW_S] + [now]
        if len(st.errors) >= BREAKER_ERRORS:
            st.paused_until = now + BREAKER_PAUSE_S
            st.errors.clear()
            return True
        return False

    def note_retry_after(self, host: str, seconds: float) -> None:
        self.state(host).retry_after_until = self.now() + min(seconds, 3600.0)


class RobotsCache:
    def __init__(self, conn: sqlite3.Connection, ttl_s: float, now: Callable[[], float] = time.time):
        self.conn = conn
        self.ttl = ttl_s
        self.now = now

    def cached(self, host: str) -> dict | None:
        r = self.conn.execute("SELECT body, fetched_at, status, crawl_delay FROM robots WHERE host=?", (host,)).fetchone()
        if r and r["fetched_at"] > self.now() - self.ttl:
            return dict(r)
        return None

    def store(self, host: str, body: str, status: int | None, crawl_delay: float | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO robots(host, body, fetched_at, status, crawl_delay) VALUES (?,?,?,?,?)",
                          (host, body, self.now(), status, crawl_delay))

    @staticmethod
    def evaluate(body: str, status: int | None, url: str, ua: str) -> tuple[bool, float | None]:
        if status in (401, 403):
            return False, None  # robots says the whole host is off limits to robots
        if status is None or status >= 400 or not body.strip():
            return True, None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        token = ua.split("/")[0]
        allowed = rp.can_fetch(token, url) and rp.can_fetch("*", url) if not rp.can_fetch(token, url) else rp.can_fetch(token, url)
        delay = rp.crawl_delay(token) or rp.crawl_delay("*")
        try:
            delay_f = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            delay_f = None
        return allowed, delay_f


class Fetcher:
    def __init__(self, conn: sqlite3.Connection, policy: Policy, cfg: Config, home: Path, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], float] = time.time):
        self.conn = conn
        self.policy = policy
        self.cfg = cfg
        self.home = home
        self.now = now
        self.gate = HostGate(min(float(cfg.get("scheduler", "per_host_rps", default=1.0)),
                                 float(policy.get("fetch", "per_host_rps_max", default=1.0))), sleep, now)
        self.robots = RobotsCache(conn, float(policy.get("fetch", "robots", "cache_s", default=86400)), now)
        self.fetch_log = JsonlLog(home / "logs" / "fetch.jsonl")
        self.policy_log = JsonlLog(home / "logs" / "policy.jsonl")
        self.hard_cap = min(HARD_CAP, int(policy.get("fetch", "max_body_bytes_hard", default=HARD_CAP)))
        self.ratio_max = float(policy.get("fetch", "decompression_ratio_max", default=100))
        self.max_hops = int(policy.get("fetch", "max_hops", default=2))
        self.daily_bytes = int(cfg.get("budgets", "daily_bytes", default=200_000_000))
        self._client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, verify=True,
            timeout=httpx.Timeout(30.0, connect=10.0), headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- public ---------------------------------------------------------------------------------
    def bytes_today(self) -> float:
        return ledger_sum(self.conn, "bytes", self.now() - 86400)

    def refuse(self, plan: FetchPlan, src_name: str, r: Refusal) -> Fetched:
        self.policy_log.write({"event": "refused_fetch", "source": src_name, "url": plan.log_url or plan.url,
                               "code": r.code, "reason": r.reason})
        return Fetched(plan=plan, status=None, error=f"refused:{r.code}", fetched_at=self.now())

    async def fetch(self, plan: FetchPlan, src: SourceConfig, spec: AdapterSpec, *, health: str = "ok",
                    boosted: bool = False, review_due_at: float | None = None) -> Fetched:
        now = self.now()
        refusal = self.policy.allow_fetch(plan.url, spec_hosts=spec.hosts, method=plan.method, allowed_methods=spec.methods,
                                          source_health=health, review_due_at=review_due_at, now=now)
        if refusal:
            return self.refuse(plan, src.name, refusal)
        if plan.body is not None and plan.method == "GET":
            return self.refuse(plan, src.name, Refusal("body_on_get", "GET requests carry no body"))
        if spec.needs_key_env is None and any(k.lower() in ("authorization", "x-api-key", "cookie") for k, _ in plan.headers):
            return self.refuse(plan, src.name, Refusal("credential_header", "credentials only for licensed APIs (H5)"))
        host = host_of(plan.url)
        if self.daily_bytes and self.bytes_today() >= self.daily_bytes:
            return self.refuse(plan, src.name, Refusal("byte_budget", "daily byte budget exhausted"))
        paused = self.gate.paused(host)
        if paused > 0:
            return self.refuse(plan, src.name, Refusal("circuit_open", f"{host} paused for {int(paused)}s after repeated errors"))

        crawl_delay: float | None = None
        if self.policy.get("fetch", "robots", "honour", default=True):
            allowed, crawl_delay = await self._robots_allow(host, plan.url, spec)
            if not allowed:
                return self.refuse(plan, src.name, Refusal("robots_disallow", f"robots.txt disallows {urlsplit(plan.url).path}"))

        max_bytes = min(plan.max_bytes, self.hard_cap)
        state = self.conn.execute("SELECT etag, last_modified, content_hash FROM fetch_state WHERE key=?", (plan.key,)).fetchone()
        headers = dict(plan.headers)
        conditional = False
        if state and plan.method == "GET":
            if state["etag"]:
                headers["If-None-Match"] = state["etag"]
                conditional = True
            if state["last_modified"]:
                headers["If-Modified-Since"] = state["last_modified"]
                conditional = True

        url = plan.url
        hops = 0
        t0 = self.now()
        result = Fetched(plan=plan, status=None, fetched_at=now, final_url=url)
        while True:
            await self.gate.wait(host, spec.min_delay_s, crawl_delay)
            try:
                resp, body, raw_bytes, err = await self._request(plan.method, url, headers, plan.body, max_bytes)
            except (httpx.HTTPError, OSError) as e:  # network/TLS errors
                result.error = type(e).__name__
                if isinstance(e, httpx.ConnectError) and "certificate" in str(e).lower():
                    result.findings.append(detectors.Finding("integrity", "cert_error", str(e)[:80]))
                self.gate.note_error(host)
                break
            if err:
                result.findings.append(err)
                result.status = resp.status_code if resp is not None else None
                break
            result.status = resp.status_code
            result.headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code in REDIRECT_CODES:
                hops += 1
                loc = resp.headers.get("location", "")
                target = urljoin(url, loc)
                if hops > self.max_hops or check_url(target) or host_of(target) != host:
                    result.findings.append(detectors.Finding("integrity", "off_host_redirect", host_of(target) or loc[:60]))
                    break
                url = target
                result.final_url = url
                continue
            if resp.status_code == 429 or resp.status_code == 503:
                ra = resp.headers.get("retry-after")
                try:
                    self.gate.note_retry_after(host, float(ra) if ra else 60.0)
                except ValueError:
                    self.gate.note_retry_after(host, 60.0)
            if resp.status_code >= 500:
                self.gate.note_error(host)
            result.body = body
            ratio = (len(body) / raw_bytes) if raw_bytes else None
            ct = resp.headers.get("content-type")
            expected = plan.expected_content_type or src.expected_content_type or spec.expected_content_type
            utf8_ok = True
            if resp.status_code == 200:
                try:
                    body.decode("utf-8")
                except UnicodeDecodeError:
                    utf8_ok = False
                result.findings.extend(detectors.check_transport(
                    request_host=host, final_host=host_of(str(resp.url)) or host, content_type=ct,
                    expected_content_type=expected, size=len(body), max_bytes=max_bytes,
                    decompression_ratio=ratio, decompression_ratio_max=self.ratio_max, utf8_ok=utf8_ok))
                result.findings.extend(detectors.check_bytes(body))
            break

        result.duration_ms = int((self.now() - t0) * 1000)
        self._record(result, src, state, conditional, boosted)
        return result

    # -- internals ------------------------------------------------------------------------------
    async def _request(self, method: str, url: str, headers: dict, body: bytes | None, max_bytes: int):
        """Stream the response under the cap. Returns (response, body, raw_bytes, finding|None)."""
        req = self._client.build_request(method, url, headers=headers, content=body)
        resp = await self._client.send(req, stream=True)
        chunks: list[bytes] = []
        total = 0
        finding: detectors.Finding | None = None
        try:
            if resp.status_code in REDIRECT_CODES or resp.status_code == 304:
                return resp, b"", 0, None
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return resp, b"", 0, detectors.Finding("integrity", "body_too_large", declared)
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    finding = detectors.Finding("integrity", "body_too_large", f">{max_bytes}")
                    break
                raw = resp.num_bytes_downloaded
                if raw and total / max(raw, 1) > self.ratio_max and total > 65536:
                    finding = detectors.Finding("integrity", "decompression_bomb", f"{total / raw:.0f}x")
                    break
                chunks.append(chunk)
        finally:
            await resp.aclose()
        return resp, b"".join(chunks), resp.num_bytes_downloaded, finding

    async def _robots_allow(self, host: str, url: str, spec: AdapterSpec) -> tuple[bool, float | None]:
        ua = self.cfg.user_agent
        rec = self.robots.cached(host)
        if rec is None:
            robots_url = f"https://{host}/robots.txt"
            body, status = "", None
            try:
                await self.gate.wait(host, spec.min_delay_s, None)
                resp, raw, _, finding = await self._request("GET", robots_url, {}, None, ROBOTS_CAP)
                status = resp.status_code
                if status == 200 and finding is None:
                    body = raw.decode("utf-8", errors="replace")
            except (httpx.HTTPError, OSError):
                status = None
            _, delay = RobotsCache.evaluate(body, status, url, ua)
            self.robots.store(host, body, status, delay)
            rec = {"body": body, "status": status, "crawl_delay": delay}
        allowed, delay = RobotsCache.evaluate(rec["body"], rec["status"], url, ua)
        return allowed, delay

    def _record(self, r: Fetched, src: SourceConfig, state, conditional: bool, boosted: bool) -> None:
        plan = r.plan
        changed = False
        if r.status == 200 and not r.findings and r.error is None:
            digest = hashlib.sha256(r.body).hexdigest()
            changed = not state or state["content_hash"] != digest
            self.conn.execute(
                """INSERT INTO fetch_state(key, url, etag, last_modified, content_hash, last_fetched_at, last_changed_at, size)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET url=excluded.url, etag=excluded.etag, last_modified=excluded.last_modified,
                     content_hash=excluded.content_hash, last_fetched_at=excluded.last_fetched_at,
                     last_changed_at=CASE WHEN excluded.content_hash IS NOT fetch_state.content_hash THEN excluded.last_changed_at
                                          ELSE fetch_state.last_changed_at END, size=excluded.size""",
                (plan.key, plan.log_url or plan.url, r.headers.get("etag"), r.headers.get("last-modified"), digest,
                 r.fetched_at, r.fetched_at, len(r.body)))
        elif r.status == 304 and state:
            self.conn.execute("UPDATE fetch_state SET last_fetched_at=? WHERE key=?", (r.fetched_at, plan.key))
        r.changed = changed
        nbytes = len(r.body)
        if nbytes:
            ledger_add(self.conn, "bytes", nbytes, src.name, r.fetched_at)
        error = r.error or (",".join(detectors.codes(r.findings)) if r.findings else None)
        self.conn.execute(
            "INSERT INTO fetches(ts, source, url, status, bytes, changed, duration_ms, conditional, boosted, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.fetched_at, src.name, plan.log_url or plan.url, r.status, nbytes, int(changed), r.duration_ms, int(conditional),
             int(boosted), error))
        self.fetch_log.write({"source": src.name, "url": plan.log_url or plan.url, "status": r.status, "bytes": nbytes,
                              "changed": changed, "duration_ms": r.duration_ms, "conditional": conditional,
                              "boosted": boosted, "error": error})
