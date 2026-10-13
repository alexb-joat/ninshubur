"""Greenhouse Job Board API (Tier 0): public, unauthenticated, ``content=false`` returns exactly the
fields we declare. Boards are identifiers from config, never URLs from content."""
from __future__ import annotations

import re

from ..config import SourceConfig
from ..items import DateRef, EntityRef, Item, apply_taint, item_uid
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register, run_projection

BOARD_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


@register
class Greenhouse:
    spec = AdapterSpec(
        name="greenhouse", kind="hiring", hosts=frozenset({"boards-api.greenhouse.io"}), schema="greenhouse_jobs.v1",
        schemas=("greenhouse_jobs.v1",), expected_content_type="application/json",
        base_interval_s=6 * 3600, max_interval_s=48 * 3600, default_tier=0, legal_basis="public_business_info",
        questions=("Which target companies are hiring for roles that imply they need our service, and since when?",),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        plans = []
        for board in self._boards(src):
            plans.append(FetchPlan(url=f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=false",
                                   key=f"greenhouse:{board}", purpose=f"jobs at {board}"))
        return plans

    @staticmethod
    def _boards(src: SourceConfig) -> list[str]:
        raw = src.params.get("boards") or ([src.params["board"]] if src.params.get("board") else [])
        return [str(b) for b in raw if BOARD_RE.match(str(b))]

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, self.spec.schema, now, spec=self.spec)
        res = ParseResult(projection=proj)
        board = fetched.plan.key.split(":", 1)[-1]
        companies = src.params.get("companies") or {}
        meta = companies.get(board, {}) if isinstance(companies, dict) else {}
        company_name = str(meta.get("name") or board)
        domain = meta.get("domain")
        entity = EntityRef(type="company", name=company_name, key=f"company:{domain or board}", domain=domain)
        for row in proj.rows:
            posted = row.get("first_published") or row.get("updated_at")
            item = Item(
                uid=item_uid(src.name, f"{board}:{row['id']}"), source=src.name, kind="hiring", title=row["title"],
                url=row["absolute_url"], text="", published_at=posted, entities=[entity],
                dates=[DateRef("posted", posted)] if posted else [],
                attrs={"board": board, "company": company_name, "location": row.get("location"), "job_id": row["id"]},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
        return res
