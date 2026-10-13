"""confs.tech conference data (Tier 1): MIT-licensed JSON files on GitHub, one per year and topic."""
from __future__ import annotations

import re

from ..config import SourceConfig
from ..items import DateRef, Item, apply_taint, item_uid
from ..policy import host_of
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register, run_projection

TOPIC_RE = re.compile(r"^[a-z0-9-]{1,40}$")
DEFAULT_TOPICS = ("general", "javascript", "devops", "data", "python")


@register
class ConfsTech:
    spec = AdapterSpec(
        name="confs_tech", kind="events", hosts=frozenset({"raw.githubusercontent.com"}), schema="confs_tech.v1",
        schemas=("confs_tech.v1",), expected_content_type="text/plain|application/json",
        base_interval_s=86400, max_interval_s=7 * 86400, default_tier=1, legal_basis="public_business_info",
        questions=("Which conferences in our target cities are coming up, and when do their CFPs close?",),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        year = int(src.params.get("year", 2026))
        if not 2020 <= year <= 2100:
            return []
        plans = []
        for topic in src.params.get("topics", DEFAULT_TOPICS):
            topic = str(topic)
            if not TOPIC_RE.match(topic):
                continue
            plans.append(FetchPlan(
                url=f"https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences/{year}/{topic}.json",
                key=f"confs_tech:{year}:{topic}", purpose=f"conferences {year}/{topic}", max_bytes=1_500_000))
        return plans

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, self.spec.schema, now, spec=self.spec)
        res = ParseResult(projection=proj)
        topic = fetched.plan.key.rsplit(":", 1)[-1]
        for row in proj.rows:
            dates = [DateRef("event_start", row["startDate"], row.get("endDate"))]
            if row.get("cfpEndDate"):
                dates.append(DateRef("cfp_deadline", row["cfpEndDate"]))
            item = Item(
                uid=item_uid(src.name, f"{row['name']}|{int(row['startDate'])}"), source=src.name, kind="event",
                title=row["name"], url=row["url"], text="", published_at=None, dates=dates,
                attrs={k: row[k] for k in ("city", "country", "online", "cfpUrl") if k in row} | {"topic": topic},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
            for u in (row.get("url"), row.get("cfpUrl")):
                h = host_of(u or "")
                if h:
                    res.candidates.append((h, u, "conference site"))
        return res
