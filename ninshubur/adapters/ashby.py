"""Ashby public job posting API (Tier 0): public, unauthenticated, documented at
https://developers.ashbyhq.com/docs/public-job-posting-api. Job boards are identifiers from config, never
URLs from content.

The response always carries each posting's description (HTML and plain text); the schema declares none of
it, so the description is dropped at projection and never stored. Unlisted postings (``isListed: false``)
are skipped. Every other path on api.ashbyhq.com, robots.txt included, answers 401, so a source needs
``robots_reviewed_status: 401`` in its trust block after review (see config.example.yaml)."""
from __future__ import annotations

import re

from ..config import SourceConfig
from ..items import DateRef, EntityRef, Item, apply_taint, item_uid
from ..policy import host_of
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register, run_projection

BOARD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JOBS_HOST = "jobs.ashbyhq.com"
# The descriptions make a posting about 20 KB, so a large board passes the 2 MB default.
MAX_BYTES = 8_000_000


@register
class Ashby:
    spec = AdapterSpec(
        name="ashby", kind="hiring", hosts=frozenset({"api.ashbyhq.com"}), schema="ashby_jobs.v1",
        schemas=("ashby_jobs.v1",), expected_content_type="application/json",
        base_interval_s=6 * 3600, max_interval_s=48 * 3600, default_tier=0, legal_basis="public_business_info",
        questions=("Which target companies are hiring for roles that imply they need our service, and since when?",),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        return [FetchPlan(url=f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false",
                          key=f"ashby:{board}", purpose=f"jobs at {board}", max_bytes=MAX_BYTES)
                for board in self._boards(src)]

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
        unlisted = 0
        for row in proj.rows:
            if row.get("isListed") is False:
                unlisted += 1
                continue
            # The link shown to a person is always on Ashby's own job host; anything else in the field is replaced.
            url = row["jobUrl"] if host_of(row["jobUrl"]) == JOBS_HOST else f"https://{JOBS_HOST}/{board}/{row['id']}"
            posted = row.get("publishedAt")
            item = Item(
                uid=item_uid(src.name, f"{board}:{row['id']}"), source=src.name, kind="hiring", title=row["title"],
                url=url, text="", published_at=posted, entities=[entity],
                dates=[DateRef("posted", posted)] if posted else [],
                attrs={"board": board, "company": company_name, "location": row.get("location"), "job_id": row["id"],
                       "department": row.get("department"), "workplace": row.get("workplaceType"),
                       "employment": row.get("employmentType"), "remote": row.get("isRemote")},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
        if unlisted:
            res.notes.append(f"{board}: {unlisted} unlisted posting(s) skipped")
        return res
