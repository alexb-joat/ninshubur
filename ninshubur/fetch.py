"""The only module that opens a network connection.

Per FetchPlan: policy gate -> robots.txt -> per-host pacing -> conditional request with
``follow_redirects=False`` -> streamed read under a byte cap with a decompression guard ->
transport detectors -> fetch_state / ledger / fetches row / fetch.jsonl. Nothing here ever
follows a URL that came out of content; adapters template URLs from identifiers.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import sqlite3
import time
import urllib.robotparser
import zlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from . import detectors
from .adapters.base import AdapterSpec, FetchPlan, Fetched
from .config import Config, SourceConfig
from .db import ledger_add, ledger_sum
from .logs import JsonlLog
from .policy import Policy, Refusal, check_url, host_of, same_site

HARD_CAP = 8_000_000
ROBOTS_CAP = 512_000  # RFC 9309 2.5: the parsing limit MUST be at least 500 KiB; this is exactly that
ROBOTS_MAX_HOPS = 5  # RFC 9309 2.3.1.2: follow at least five consecutive redirects
ROBOTS_UNREACHABLE_TTL_S = 600
REDIRECT_CODES = {301, 302, 303, 307, 308}
DECODE_STEP = 65_536  # most bytes one gzip step may produce: a bomb is caught 64 KB past the cap, not 64 MB
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


def resolve_host(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except socket.gaierror:
        return []


def reachable(host: str) -> bool:
    """Whether ``host`` resolves and this machine has a route to it. Both checks stay on this machine: the DNS
    lookup a fetch makes anyway, and a UDP connect, which picks a route without sending anything."""
    for addr in resolve_host(host):
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.connect((addr.split("%", 1)[0], 443))
            return True
        except OSError:
            continue
    return False


def root_cause(e: BaseException) -> str:
    """The innermost reason behind a network error ("[Errno -3] Temporary failure in name resolution"), not
    httpx's wrapper ("All connection attempts failed")."""
    while True:
        inner = e.__cause__ if e.__cause__ is not None else (None if e.__suppress_context__ else e.__context__)
        if inner is None:
            break
        e = inner
    return (str(e) or type(e).__name__)[:160]


# Denied whatever this Python's ipaddress tables say (they changed in 3.11.10/3.12.4/3.13): ranges that tunnel to,
# translate to, or stand for addresses that are not on the public internet.
DENIED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "100.64.0.0/10",    # carrier-grade NAT
    "192.0.0.0/24",     # IETF protocol assignments
    "198.18.0.0/15",    # benchmarking
    "10.47.83.0/24",    # this machine's WireGuard tunnel (inside 10/8 already; named so a change here is deliberate)
    "64:ff9b::/96",     # NAT64: a public-looking v6 address that a gateway turns into any v4 one, 127.0.0.1 included
    "64:ff9b:1::/48",   # local-use NAT64
    "2002::/16",        # 6to4: embeds a v4 address
    "2001::/32",        # Teredo: embeds a v4 address
    "fec0::/10",        # site-local (deprecated, still routed on some networks)
    "::/96",            # v4-compatible (deprecated)
))


def is_public_address(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if any(ip.version == n.version and ip in n for n in DENIED_NETWORKS):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped is not None and not is_public_address(str(ip.ipv4_mapped))):
        return False
    return True


class AddressRefused(httpx.ConnectError):
    """The pinned-address check refused a connection: no answer, or an address that is not public."""


class DeadlineExceeded(httpx.TimeoutException):
    """A request ran past ``fetch.request_deadline_s`` (connect, headers and body together)."""


class PinnedTransport(httpx.AsyncBaseTransport):
    """Connects to the address the gate checked, never to a second DNS answer.

    The URL's host becomes the checked IP; TLS still verifies the certificate against the name (httpcore's
    ``sni_hostname`` extension) and the Host header still names it. Without this, the address check and the
    connection made two DNS lookups, and a name could answer public for the first and private for the second
    (DNS rebinding). The inner transport keeps no idle connections: a connection opened for one name is never
    reused for another name that shares its address."""

    def __init__(self, inner: httpx.AsyncBaseTransport, pin: Callable[[str], list[str]]):
        self.inner = inner
        self.pin = pin

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        name = url.host
        if url.scheme != "https" or url.port not in (None, 443):
            raise AddressRefused(f"{url.scheme}://{name}:{url.port}: only https on port 443 is fetched", request=request)
        last: httpx.ConnectError | None = None
        for addr in self.pin(name)[:2]:  # v4 first, then one v6 try: no second lookup, no address the gate didn't check
            ext = dict(request.extensions)
            ext["sni_hostname"] = name
            pinned = httpx.Request(request.method, url.copy_with(host=addr), headers=request.headers,
                                   stream=request.stream, extensions=ext)
            try:
                return await self.inner.handle_async_request(pinned)
            except httpx.ConnectError as e:
                last = e
        raise last or AddressRefused(f"{name}: no address to connect to", request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class BoundedDecoder:
    """Content decoding in steps of at most ``DECODE_STEP`` output bytes.

    httpx decodes each network chunk whole, and gzip expands up to about 1000x, so one 64 KB chunk could become 64 MB
    before any cap was checked. Only what the fetcher asks for (``Accept-Encoding: gzip``) is decoded: br, zstd,
    deflate or stacked encodings raise ValueError, and the response is refused unread."""

    def __init__(self, content_encoding: str | None):
        enc = (content_encoding or "").strip().lower()
        if enc in ("", "identity"):
            self._z = None
        elif enc in ("gzip", "x-gzip"):
            self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        else:
            raise ValueError(enc[:40])

    def steps(self, data: bytes) -> Iterable[bytes]:
        if self._z is None:
            if data:
                yield data
            return
        while data and not self._z.eof:
            out = self._z.decompress(data, DECODE_STEP)
            data = self._z.unconsumed_tail
            if out:
                yield out


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

    @asynccontextmanager
    async def slot(self, host: str, min_delay: float, crawl_delay: float | None) -> AsyncIterator[float]:
        """One request at a time per host, across every source sharing this fetcher: hold the host's lock, wait out
        its delays, and keep the lock until the request is done. Yields the delay waited."""
        async with self.state(host).lock:
            yield await self.wait(host, min_delay, crawl_delay)

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
        if r is None:
            return None
        ttl = min(self.ttl, ROBOTS_UNREACHABLE_TTL_S) if self.unreachable(r["status"]) else self.ttl
        return dict(r) if r["fetched_at"] > self.now() - ttl else None

    @staticmethod
    def unreachable(status: int | None) -> bool:
        """A network error or a 5xx answer: robots.txt could not be read (RFC 9309 2.3.1.4). Everything on the
        host is disallowed until it can be, and it is asked again after ten minutes, not a day. A 3xx stored by an
        older version (which read a redirect as "no rules") counts as unreadable too, so it is asked again soon."""
        return status is None or status >= 500 or 300 <= status < 400

    def store(self, host: str, body: str, status: int | None, crawl_delay: float | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO robots(host, body, fetched_at, status, crawl_delay) VALUES (?,?,?,?,?)",
                          (host, body, self.now(), status, crawl_delay))

    @staticmethod
    def evaluate(body: str, status: int | None, url: str, ua: str) -> tuple[bool, float | None]:
        if RobotsCache.unreachable(status) or status in (401, 403):
            return False, None  # unreadable, or the whole host is off limits to robots
        if status >= 400 or not body.strip():
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
                 now: Callable[[], float] = time.time,
                 resolve: Callable[[str], list[str]] | None = None):
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
        self.deadline_s = float(policy.get("fetch", "request_deadline_s", default=20))
        self.daily_bytes = int(cfg.get("budgets", "daily_bytes", default=200_000_000))
        self.refuse_private = bool(policy.get("fetch", "refuse_private_addresses", default=True))
        self.resolve = resolve or resolve_host
        self._addr_cache: dict[str, tuple[float, list[str], str | None]] = {}
        self._robots_exceptions: set[tuple[str, str, int]] = set()
        inner = transport or httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0,
                                                      limits=httpx.Limits(max_keepalive_connections=0))
        self._client = httpx.AsyncClient(
            transport=PinnedTransport(inner, self._pinned), follow_redirects=False, trust_env=False, verify=True,
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

    def _log_robots_exception(self, src_name: str, host: str, status: int) -> None:
        """Once per source and host per process: the reviewed exception is visible in policy.jsonl."""
        key = (src_name, host, status)
        if key in self._robots_exceptions:
            return
        self._robots_exceptions.add(key)
        self.policy_log.write({"event": "robots_reviewed_exception", "source": src_name, "host": host, "status": status,
                               "reason": f"robots.txt answers {status}; the source's trust block records this status as reviewed"})

    async def fetch(self, plan: FetchPlan, src: SourceConfig, spec: AdapterSpec, *, health: str = "ok",
                    boosted: bool = False, review_due_at: float | None = None,
                    extra_hosts: Iterable[str] = ()) -> Fetched:
        now = self.now()
        refusal = self.policy.allow_fetch(plan.url, spec_hosts=spec.hosts, method=plan.method, allowed_methods=spec.methods,
                                          source_health=health, review_due_at=review_due_at, now=now, extra_hosts=extra_hosts)
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
        if self.refuse_private:
            bad = self._private_address(host)
            if bad:
                return self.refuse(plan, src.name, Refusal("private_address", f"{host} resolves to {bad}"))

        crawl_delay: float | None = None
        if self.policy.get("fetch", "robots", "honour", default=True):
            allowed, crawl_delay, robots_status = await self._robots_allow(host, plan.url, spec)
            if (not allowed and robots_status in (401, 403) and robots_status == src.robots_reviewed_status
                    and host in spec.hosts):  # fixed adapter hosts only: never a host handed over at run time
                # A person read this host's terms and recorded the exact answer its robots.txt gives (an API
                # host behind a login wall answers every unknown path, robots.txt included, with 401).
                allowed = True
                self._log_robots_exception(src.name, host, robots_status)
            if not allowed and RobotsCache.unreachable(robots_status):
                return self.refuse(plan, src.name, Refusal("robots_unreachable", f"robots.txt on {host} could not be read "
                                                           f"({robots_status or 'network error'}); asked again in 10 min"))
            if not allowed and robots_status in (401, 403) and host in spec.hosts:
                return self.refuse(plan, src.name, Refusal(
                    "robots_disallow", f"robots.txt on {host} answers {robots_status}, read as the whole host off limits; "
                    f"if you have read the host's terms, record `robots_reviewed_status: {robots_status}` in the trust "
                    f"block of source {src.name}"))
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
        expected = plan.expected_content_type or src.expected_content_type or spec.expected_content_type
        t0 = self.now()
        result = Fetched(plan=plan, status=None, fetched_at=now, final_url=url)
        while True:
            try:
                async with self.gate.slot(host, spec.min_delay_s, crawl_delay):
                    resp, body, raw_bytes, err = await self._request(plan.method, url, headers, plan.body, max_bytes,
                                                                     expected=expected)
            except (httpx.HTTPError, OSError) as e:  # network/TLS errors
                result.error = type(e).__name__
                result.error_detail = root_cause(e)
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
                if hops > self.max_hops or check_url(target) or not same_site(host_of(target), host):
                    result.findings.append(detectors.Finding("integrity", "off_host_redirect", host_of(target) or loc[:60]))
                    break
                if host_of(target) != host:  # www. <-> apex twin: re-check the gate for the twin, then continue there
                    twin = host_of(target)
                    r2 = self.policy.allow_fetch(target, spec_hosts=set(spec.hosts) | {twin}, method=plan.method, allowed_methods=spec.methods,
                                                 source_health=health, review_due_at=review_due_at, now=now, extra_hosts=extra_hosts)
                    if r2 or self.policy.host_denied(twin) or (self.refuse_private and self._private_address(twin)):
                        result.findings.append(detectors.Finding("integrity", "off_host_redirect", twin))
                        break
                    if self.policy.get("fetch", "robots", "honour", default=True):
                        twin_ok, crawl_delay, _ = await self._robots_allow(twin, target, spec)
                        if not twin_ok:  # the twin's own robots.txt governs the twin
                            self.policy_log.write({"event": "refused_fetch", "source": src.name, "url": target,
                                                   "code": "robots_disallow", "reason": f"robots.txt on {twin} disallows "
                                                   f"{urlsplit(target).path}"})
                            result.error = "refused:robots_disallow"
                            break
                    host = twin
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

    def _lookup(self, host: str) -> tuple[list[str], str | None]:
        """One DNS answer per host per 10 minutes, shared by the gate's check and the connection: (addresses, first
        non-public address or None). A failed lookup is not remembered, so the check runs again once DNS is back."""
        now = self.now()
        hit = self._addr_cache.get(host)
        if hit and hit[0] > now - 600:
            return hit[1], hit[2]
        try:
            addrs = list(self.resolve(host))
        except OSError:
            addrs = []
        bad = next((a for a in addrs if not is_public_address(a)), None)
        if addrs:
            self._addr_cache[host] = (now, addrs, bad)
        return addrs, bad

    def _private_address(self, host: str) -> str | None:
        """Return the first non-public address ``host`` resolves to, or None."""
        return self._lookup(host)[1]

    def _pinned(self, host: str) -> list[str]:
        """The addresses a connection to ``host`` may use: exactly those the check saw, IPv4 first."""
        addrs, bad = self._lookup(host)
        if not addrs:
            raise AddressRefused(f"{host} did not resolve")
        if bad and self.refuse_private:
            self.policy_log.write({"event": "address_refused", "host": host, "address": bad,
                                   "reason": "resolved to a non-public address at connect time"})
            raise AddressRefused(f"{host} resolves to {bad}")
        return sorted(addrs, key=lambda a: ":" in a)

    # -- internals ------------------------------------------------------------------------------
    async def _request(self, method: str, url: str, headers: dict, body: bytes | None, max_bytes: int, *,
                       expected: str | None = None, truncate: bool = False):
        """Stream the response under the cap and the deadline. Returns (response, body, raw_bytes, finding|None).
        With ``expected`` (``a/b|c/d``), a 200 whose Content-Type doesn't match is refused before its body is read.
        With ``truncate``, a body over the cap is cut at the cap instead of refused (robots.txt: parse the first 500 KiB)."""
        try:
            async with asyncio.timeout(self.deadline_s) as cm:
                return await self._stream(method, url, headers, body, max_bytes, expected, truncate)
        except TimeoutError:
            if not cm.expired():
                raise
            raise DeadlineExceeded(f"no complete answer within {self.deadline_s:g} s") from None

    async def _stream(self, method: str, url: str, headers: dict, body: bytes | None, max_bytes: int,
                      expected: str | None = None, truncate: bool = False):
        req = self._client.build_request(method, url, headers=headers, content=body)
        resp = await self._client.send(req, stream=True)
        chunks: list[bytes] = []
        total = 0
        stop = False
        finding: detectors.Finding | None = None
        try:
            if resp.status_code in REDIRECT_CODES or resp.status_code == 304:
                return resp, b"", 0, None
            ct = resp.headers.get("content-type")
            if resp.status_code == 200 and expected and not detectors.content_type_matches(ct, expected):
                return resp, b"", 0, detectors.Finding("integrity", "content_type_mismatch", str(ct)[:60])
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes and not truncate:
                return resp, b"", 0, detectors.Finding("integrity", "body_too_large", declared)
            try:
                decoder = BoundedDecoder(resp.headers.get("content-encoding"))
            except ValueError as e:
                return resp, b"", 0, detectors.Finding("integrity", "unsupported_encoding", str(e))
            try:
                async for raw_chunk in resp.aiter_raw():
                    for chunk in decoder.steps(raw_chunk):
                        raw = resp.num_bytes_downloaded
                        seen_total = total + len(chunk)
                        if raw and seen_total / max(raw, 1) > self.ratio_max and seen_total > 65536:
                            finding = detectors.Finding("integrity", "decompression_bomb", f"{seen_total / raw:.0f}x")
                            break
                        if seen_total > max_bytes and truncate:
                            chunks.append(chunk[:max_bytes - total])
                            total = max_bytes
                            stop = True
                            break
                        total = seen_total
                        if total > max_bytes:
                            finding = detectors.Finding("integrity", "body_too_large", f">{max_bytes}")
                            break
                        chunks.append(chunk)
                    if finding or stop:
                        break
            except zlib.error as e:
                finding = detectors.Finding("integrity", "bad_encoding", str(e)[:60])
        finally:
            await resp.aclose()
        return resp, b"".join(chunks), resp.num_bytes_downloaded, finding

    async def _robots_allow(self, host: str, url: str, spec: AdapterSpec) -> tuple[bool, float | None, int | None]:
        ua = self.cfg.user_agent
        rec = self.robots.cached(host)
        if rec is None:
            body, status = await self._read_robots(host, spec)
            _, delay = RobotsCache.evaluate(body, status, url, ua)
            self.robots.store(host, body, status, delay)
            rec = {"body": body, "status": status, "crawl_delay": delay}
        allowed, delay = RobotsCache.evaluate(rec["body"], rec["status"], url, ua)
        return allowed, delay, rec["status"]

    async def _read_robots(self, host: str, spec: AdapterSpec) -> tuple[str, int | None]:
        """Fetch robots.txt the way RFC 9309 asks, on the safe side wherever it leaves a choice. Returns (body, status);
        status None means unreachable, which disallows the whole host for ten minutes.

        - Redirects: followed up to five hops (2.3.1.2) within the same site (the host or its www twin). A redirect to
          another site is not followed, because this fetcher contacts allowlisted hosts only; it and a sixth hop both
          leave robots.txt unreachable (the RFC would allow everything after five hops).
        - Size: the first 500 KiB are parsed, cut at the last full line (2.5); more is never read.
        - A bomb, a corrupt or unrequested encoding, or a deadline overrun: unreachable, never "no rules"."""
        url = f"https://{host}/robots.txt"
        for _ in range(ROBOTS_MAX_HOPS + 1):
            try:
                async with self.gate.slot(host_of(url), spec.min_delay_s, None):
                    resp, raw, _, finding = await self._request("GET", url, {}, None, ROBOTS_CAP, truncate=True)
            except (httpx.HTTPError, OSError):
                return "", None
            if resp.status_code in REDIRECT_CODES:
                target = urljoin(url, resp.headers.get("location", ""))
                thost = host_of(target)
                if check_url(target) or not same_site(thost, host) or self.policy.host_denied(thost):
                    self.policy_log.write({"event": "robots_redirect_refused", "host": host, "target_host": thost or "?",
                                           "reason": "robots.txt redirects to another site; read as unreachable"})
                    return "", None
                url = target
                continue
            if resp.status_code != 200:
                return "", resp.status_code
            if finding is not None:
                return "", None
            text = raw.decode("utf-8", errors="replace")
            if len(raw) >= ROBOTS_CAP:
                text = text[:text.rfind("\n") + 1]
            return text, 200
        self.policy_log.write({"event": "robots_redirect_refused", "host": host, "target_host": host_of(url),
                               "reason": f"more than {ROBOTS_MAX_HOPS} redirects; read as unreachable"})
        return "", None

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
                              "boosted": boosted, "error": error,
                              **({"detail": r.error_detail} if r.error_detail else {})})
