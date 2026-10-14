"""Adapter contract. Adapters are synchronous and pure: they build FetchPlans from identifiers
and parse Fetched bodies through a schema. Only ``fetch.Fetcher`` touches the network."""
from __future__ import annotations

import importlib
import json
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import SourceConfig
from ..detectors import Finding
from ..items import Item
from ..schemas import Projection, get_schema, project


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    kind: str  # events|service_requests|pains|tooling|hiring|news|social|people_company|geo|status
    hosts: frozenset[str]  # exact hostnames; the Fetcher refuses anything else
    schema: str  # key into SCHEMAS (primary schema; adapters may use others for follow-ups)
    expected_content_type: str
    base_interval_s: int
    max_interval_s: int
    default_tier: int
    legal_basis: str
    min_delay_s: float = 1.0
    person_data: bool = False
    needs_key_env: str | None = None
    methods: frozenset[str] = frozenset({"GET"})
    questions: tuple[str, ...] = ()
    schemas: tuple[str, ...] = ()  # every schema this adapter uses (checked at startup)
    # Dynamic-host adapters have no fixed host list: the scheduler hands them a per-run allowlist built
    # from an admitted dataset (never from a page being crawled). ``needs`` names what the scheduler
    # must put into ``state`` before ``plan`` (e.g. "events").
    dynamic_hosts: bool = False
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchPlan:
    url: str
    key: str  # fetch_state key (stable per logical resource)
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    max_bytes: int = 2_000_000
    purpose: str = ""
    schema: str | None = None  # override the spec schema for this plan (follow-ups)
    expected_content_type: str | None = None
    log_url: str | None = None  # what to log instead of url (licensed APIs with person ids)
    derived_from: str | None = None  # the page this link was found on; the scheduler applies policy.allow_derived_link
    anchor_text: str = ""


@dataclass
class Fetched:
    plan: FetchPlan
    status: int | None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    changed: bool = False
    duration_ms: int = 0
    final_url: str = ""
    error: str | None = None
    findings: list[Finding] = field(default_factory=list)
    fetched_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.error is None and not self.findings

    @property
    def unchanged(self) -> bool:
        return self.status == 304 or (self.status == 200 and not self.changed)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class ParseResult:
    items: list[Item] = field(default_factory=list)
    followups: list[FetchPlan] = field(default_factory=list)
    projection: Projection | None = None
    notes: list[str] = field(default_factory=list)
    candidates: list[tuple[str, str, str]] = field(default_factory=list)  # (host, example_url, reason)
    dropped_rows: list[tuple[str, tuple[str, ...], str]] = field(default_factory=list)  # (url, codes, excerpt) -> quarantine

    @property
    def findings(self) -> list[Finding]:
        return list(self.projection.findings) if self.projection else []

    @property
    def violations(self) -> int:
        return self.projection.violations if self.projection else 0


class Adapter(Protocol):
    spec: AdapterSpec

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]: ...

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult: ...


def run_projection(fetched: Fetched, schema_id: str, now: float, *, spec: AdapterSpec,
                   detectors_cfg: dict | None = None) -> Projection:
    """Decode JSON and project it. Any failure is a Projection with a structure finding."""
    schema = get_schema(schema_id)
    try:
        data = fetched.json()
    except (ValueError, UnicodeDecodeError) as e:
        p = Projection()
        p.findings.append(Finding("high", "not_json", str(e)[:80]))
        return p
    return project(data, schema, now, detectors_cfg=detectors_cfg, allowlisted_hosts=tuple(sorted(spec.hosts)))


REGISTRY: dict[str, type] = {}
_LOADED = False


def register(cls: type) -> type:
    REGISTRY[cls.spec.name] = cls
    return cls


def _load_all() -> None:
    """Import every module in this package once so that ``@register`` has run. Adding an adapter
    is adding a file; nothing here needs editing."""
    global _LOADED
    if _LOADED:
        return
    from . import __path__ as pkg_path
    for mod in pkgutil.iter_modules(pkg_path):
        if mod.name != "base":
            importlib.import_module(f"{__package__}.{mod.name}")
    _LOADED = True


def get_adapter(name: str) -> Adapter:
    _load_all()
    try:
        return REGISTRY[name]()
    except KeyError:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(REGISTRY)}") from None


def all_adapters() -> dict[str, type]:
    _load_all()
    return dict(REGISTRY)
