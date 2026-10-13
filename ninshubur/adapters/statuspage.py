"""Atlassian Statuspage summaries (Tier 0): official status pages of providers we depend on.
Known hosts are listed here; adding one is a reviewed code change, not a config knob."""
from __future__ import annotations

from ..config import SourceConfig
from ..items import DateRef, Item, apply_taint, item_uid
from ..policy import host_of
from ..schemas import get_schema, project
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register

KNOWN_HOSTS = frozenset({"status.claude.com", "status.anthropic.com", "status.openai.com", "www.githubstatus.com",
                         "status.openrouter.ai", "status.hubspot.com"})


@register
class Statuspage:
    spec = AdapterSpec(
        name="statuspage", kind="status", hosts=KNOWN_HOSTS, schema="statuspage_summary.v1",
        schemas=("statuspage_summary.v1", "statuspage_components.v1"), expected_content_type="application/json",
        base_interval_s=600, max_interval_s=3600, default_tier=0, legal_basis="public_business_info",
        questions=("Is a provider we rely on having an incident right now, and how bad is it?",),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        host = str(src.params.get("host", "")).lower()
        if host not in KNOWN_HOSTS:
            return []
        return [FetchPlan(url=f"https://{host}/api/v2/summary.json", key=f"statuspage:{host}", purpose=f"status of {host}",
                          max_bytes=1_000_000)]

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        host = host_of(fetched.plan.url)
        provider = str(src.params.get("provider") or host)
        try:
            data = fetched.json()
        except (ValueError, UnicodeDecodeError) as e:
            from ..detectors import Finding
            from ..schemas import Projection
            p = Projection()
            p.findings.append(Finding("high", "not_json", str(e)[:80]))
            return ParseResult(projection=p)
        hosts = tuple(sorted(self.spec.hosts))
        inc = project(data, get_schema("statuspage_summary.v1"), now, allowlisted_hosts=hosts)
        comp = project(data, get_schema("statuspage_components.v1"), now, allowlisted_hosts=hosts)
        merged = inc
        merged.checked += comp.checked
        merged.violations += comp.violations
        merged.dropped_rows += comp.dropped_rows
        merged.findings.extend(f for f in comp.findings if f not in merged.findings)
        res = ParseResult(projection=merged)
        for row in inc.rows:
            dates = []
            if row.get("created_at"):
                dates.append(DateRef("incident_started", row["created_at"]))
            if row.get("resolved_at"):
                dates.append(DateRef("incident_resolved", row["resolved_at"]))
            item = Item(
                uid=item_uid(src.name, f"incident:{row['id']}"), source=src.name, kind="incident", title=row["name"],
                url=row.get("shortlink") or f"https://{host}/incidents/{row['id']}", text="",
                published_at=row.get("created_at"), dates=dates,
                attrs={"provider": provider, "status": row["status"], "impact": row.get("impact"),
                       "updated_at": row.get("updated_at"), "resolved": row["status"] in ("resolved", "postmortem", "completed")},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
        for row in comp.rows:
            item = Item(
                uid=item_uid(src.name, f"component:{row['id']}"), source=src.name, kind="status_component",
                title=row["name"], url=f"https://{host}/", text="", published_at=row.get("updated_at"),
                attrs={"provider": provider, "status": row["status"], "component_id": row["id"]},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
        return res
