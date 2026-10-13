from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc).timestamp()
SAMPLES = Path(__file__).parent / "samples"
MALICIOUS = Path(__file__).parent / "malicious"


@pytest.fixture
def now() -> float:
    return NOW


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("NINSHUBUR_HOME", str(h))
    from ninshubur.home import ensure_home
    return ensure_home(h)


@pytest.fixture
def policy(home: Path):
    from ninshubur.policy import Policy
    return Policy.load(home)


class Seen:
    """Every request that reached the mock transport: (host, path, method, has_auth)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, bool]] = []

    def hosts(self) -> set[str]:
        return {c[0] for c in self.calls}


def load_sample(rel: str) -> bytes:
    p = SAMPLES / rel
    if not p.exists():
        p = MALICIOUS / rel
    return p.read_bytes()


def make_transport(routes: dict, seen: Seen | None = None) -> httpx.MockTransport:
    """``routes`` maps (host, path) -> response spec dict or callable(request)->httpx.Response.

    A spec dict: {"body": bytes|str|dict, "status": int, "headers": {...}, "etag": str}.
    Any host not in routes answers 599 so tests catch stray fetches.
    """
    seen = seen or Seen()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        seen.calls.append((host, path, request.method, "authorization" in request.headers or "x-api-key" in request.headers))
        spec = routes.get((host, path)) or routes.get((host, "*"))
        if spec is None:
            return httpx.Response(599, text="unexpected host in test")
        if callable(spec):
            return spec(request)
        etag = spec.get("etag")
        if etag and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        body = spec.get("body", b"")
        headers = dict(spec.get("headers", {}))
        if etag:
            headers["ETag"] = etag
        if isinstance(body, dict) or isinstance(body, list):
            body = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body = body.encode()
        headers.setdefault("Content-Type", "application/json")
        return httpx.Response(spec.get("status", 200), content=body, headers=headers)

    t = httpx.MockTransport(handler)
    t.seen = seen  # type: ignore[attr-defined]
    return t


@pytest.fixture
def seen() -> Seen:
    return Seen()


# --- full-environment helpers ------------------------------------------------------------------
def trust(tier: int, schema: str, ct: str = "application/json", **over) -> dict:
    t = {"tier": tier, "owner": "Test Owner", "owner_url": "https://owner.example/about",
         "terms_url": "https://owner.example/terms", "licence": "MIT", "legal_basis": "public_business_info",
         "rate_limit": "1 req/s", "expected_content_type": ct, "schema": schema, "reviewed_by": "alex",
         "reviewed_at": "2026-10-12", "review_every_days": {0: 180, 1: 120, 2: 90, 3: 60}[tier]}
    t.update(over)
    return t


DEFAULT_SOURCES = {
    "confs": {"adapter": "confs_tech", "params": {"year": 2026, "topics": ["general"]},
              "trust": trust(1, "confs_tech.v1", "text/plain|application/json")},
    "acme-jobs": {"adapter": "greenhouse", "params": {"boards": ["acme"], "companies": {"acme": {"name": "Acme Inc", "domain": "acme.example"}}},
                  "trust": trust(0, "greenhouse_jobs.v1")},
    "hn": {"adapter": "hn", "params": {"freelancer_threads": True}, "trust": trust(2, "hn_story.v1")},
    "claude-status": {"adapter": "statuspage", "params": {"host": "status.claude.com", "provider": "anthropic/claude"},
                      "trust": trust(0, "statuspage_summary.v1")},
}


def write_config(home: Path, sources: dict | None = None, extra: dict | None = None) -> Path:
    import yaml
    cfg = {"sources": sources if sources is not None else DEFAULT_SOURCES}
    cfg.update(extra or {})
    p = home / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def write_profiles(home: Path, profiles: dict | None = None) -> Path:
    import yaml
    profiles = profiles or {"leadget-eu": {
        "industries": ["saas", "devtools"], "titles": ["VP Sales", "Head of Growth", "Founder"],
        "geos": {"cities": ["Lisbon", "Berlin", "Dublin"], "countries": ["Portugal", "Germany", "Ireland"]},
        "pains": ["outbound", "lead generation", "appointment setting"],
        "keywords": ["lead generation", "outbound", "appointment setting", "sdr", "b2b"],
        "negative_keywords": ["internship"], "home_base": "Lisbon",
        "target_companies": [{"name": "Acme Inc", "domain": "acme.example", "greenhouse": "acme"}],
        "tools_in_use": [{"kind": "llm_provider", "id": "anthropic/claude"}],
        "topics": ["python", "saas", "growth"]}}
    p = home / "profiles.yaml"
    p.write_text(yaml.safe_dump({"profiles": profiles}))
    return p


SAMPLE_ROUTES = {
    ("raw.githubusercontent.com", "/robots.txt"): {"status": 404, "body": ""},
    ("raw.githubusercontent.com", "/tech-conferences/conference-data/main/conferences/2026/general.json"):
        {"body": load_sample("confs_tech/general.json"), "headers": {"Content-Type": "text/plain; charset=utf-8"}, "etag": '"c1"'},
    ("boards-api.greenhouse.io", "/robots.txt"): {"status": 404, "body": ""},
    ("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): {"body": load_sample("greenhouse/jobs.json"), "etag": '"g1"'},
    ("hn.algolia.com", "/robots.txt"): {"status": 404, "body": ""},
    ("status.claude.com", "/robots.txt"): {"status": 404, "body": ""},
    ("status.claude.com", "/api/v2/summary.json"): {"body": load_sample("statuspage/summary.json")},
}


def hn_router(stories: bytes, comments: bytes):
    def route(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        body = comments if q.get("tags", "").startswith("comment") else stories
        return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})
    return route


class Env:
    def __init__(self, home: Path, routes: dict, clock=None):
        from ninshubur.config import load_config
        from ninshubur.db import connect
        from ninshubur.fetch import Fetcher
        from ninshubur.ingest import Ingestor
        from ninshubur.policy import Policy
        from ninshubur.schedule import Scheduler
        import random

        self.home = home
        self.clock = clock or _Clock()
        self.seen = Seen()
        self.policy = Policy.load(home)
        self.cfg = load_config(home, policy=self.policy)
        self.conn = connect(home)
        self.fetcher = Fetcher(self.conn, self.policy, self.cfg, home, transport=make_transport(routes, self.seen),
                               sleep=self.clock.sleep, now=self.clock.now)
        self.ingestor = Ingestor(self.conn, self.policy, self.cfg, home, now=self.clock.now)
        self.scheduler = Scheduler(self.conn, self.policy, self.cfg, home, self.fetcher, self.ingestor,
                                   now=self.clock.now, rng=random.Random(1))

    def run_once(self, **kw):
        import asyncio
        return asyncio.run(self.scheduler.run_once(**kw))

    def source(self, name: str) -> dict:
        return self.ingestor.source(name)


class _Clock:
    def __init__(self, t: float = NOW):
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


def grep_home(home: Path, needle: str) -> list[str]:
    """Every file under home (db, wal, logs, feeds) that contains the needle."""
    hits = []
    for p in home.rglob("*"):
        if p.is_file():
            try:
                if needle.encode("utf-8") in p.read_bytes():
                    hits.append(str(p.relative_to(home)))
            except OSError:
                pass
    return hits
