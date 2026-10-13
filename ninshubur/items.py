"""Normalised records produced by adapters. Everything here already passed a schema."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

DATE_KINDS = frozenset({
    "event_start", "event_end", "cfp_deadline", "early_bird_deadline", "posted", "released",
    "deprecated", "retirement", "incident_started", "incident_resolved", "role_change", "price_changed",
})

ITEM_KINDS = frozenset({
    "event", "thread", "service_request", "discovery", "hiring", "incident", "status_component",
    "pain", "tooling", "news", "social", "market_model", "deprecation", "person", "company",
})


def item_uid(source: str, canonical_id: str) -> str:
    return hashlib.sha256(f"{source}:{canonical_id}".encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class EntityRef:
    type: str  # person | company
    name: str
    key: str  # stable: company domain or "company:<slug>"; person: provider id
    domain: str | None = None
    city: str | None = None
    country: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class DateRef:
    kind: str
    at: float  # UTC epoch
    end: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in DATE_KINDS:
            raise ValueError(f"unknown date kind {self.kind!r}")


@dataclass
class Item:
    uid: str
    source: str
    kind: str
    title: str
    url: str
    text: str
    published_at: float | None
    entities: list[EntityRef] = field(default_factory=list)
    dates: list[DateRef] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    legal_basis: str = "public_business_info"
    person_data: bool = False
    tainted: bool = False
    taint_codes: tuple[str, ...] = ()
    taint_severity: str | None = None  # None | medium | high
    tier: int | None = None  # overrides the source tier (e.g. HN comments are Tier 3 under a Tier 2 source)


def apply_taint(item: "Item", row: dict) -> "Item":
    """Copy the projection's taint verdict onto an item."""
    codes = tuple(row.get("_taint") or ())
    sev = row.get("_severity")
    item.taint_codes = codes
    item.taint_severity = sev
    item.tainted = bool(codes)
    return item
