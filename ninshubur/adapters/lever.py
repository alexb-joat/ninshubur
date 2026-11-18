"""Lever public postings API (Tier 0): public, unauthenticated, documented at
https://github.com/lever/postings-api. Job sites are identifiers from config, never URLs from content.

The answer always carries each posting's description, lists and closing text (about 19 KB a posting, checked
18 Nov 2026); the schema declares none of it, so all of it is dropped at projection and never stored. Lever's
robots.txt on api.lever.co allows every path and asks for a 1-second crawl delay, which the fetcher keeps.

Pages, not one answer: without ``limit`` Lever answers every posting at once, and an answer over the
fetcher's cap (8 MB, about 400 postings) is a transport integrity failure that quarantines the whole source.
So each site is read as fixed pages of 200 (``skip=0, 200, ...``) up to ``max_postings`` (default 400, at most
2000). Every page is its own request with its own ETag. A full last page means the site may list more: the
run says so and names the setting. A posting added during a run can shift across a page edge; it is read on
the next run. Sites on Lever's EU instance (api.eu.lever.co) are not read yet."""
from __future__ import annotations

import re

from ..config import SourceConfig
from ..items import DateRef, EntityRef, Item, apply_taint, item_uid
from ..policy import host_of
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register, run_projection

BOARD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JOBS_HOST = "jobs.lever.co"
PAGE = 200  # about 4 MB with descriptions, half the fetcher's cap
DEFAULT_MAX_POSTINGS = 400
MAX_POSTINGS = 2000  # the schema's max_items
MAX_BYTES = 8_000_000


@register
class Lever:
    spec = AdapterSpec(
        name="lever", kind="hiring", hosts=frozenset({"api.lever.co"}), schema="lever_postings.v1",
        schemas=("lever_postings.v1",), expected_content_type="application/json",
        base_interval_s=6 * 3600, max_interval_s=48 * 3600, default_tier=0, legal_basis="public_business_info",
        questions=("Which target companies are hiring for roles that imply they need our service, and since when?",),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        return [FetchPlan(url=f"https://api.lever.co/v0/postings/{board}?mode=json&skip={skip}&limit={PAGE}",
                          key=f"lever:{board}:{skip}", purpose=f"jobs at {board}" + (f", from {skip + 1}" if skip else ""),
                          max_bytes=MAX_BYTES)
                for board in self._boards(src) for skip in range(0, self._max_postings(src), PAGE)]

    @staticmethod
    def _max_postings(src: SourceConfig) -> int:
        try:
            n = int(src.params.get("max_postings", DEFAULT_MAX_POSTINGS))
        except (TypeError, ValueError):
            n = DEFAULT_MAX_POSTINGS
        return min(max(n, PAGE), MAX_POSTINGS)

    @staticmethod
    def _boards(src: SourceConfig) -> list[str]:
        raw = src.params.get("boards") or ([src.params["board"]] if src.params.get("board") else [])
        return [str(b) for b in raw if BOARD_RE.match(str(b))]

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, self.spec.schema, now, spec=self.spec)
        res = ParseResult(projection=proj)
        _, board, skip = fetched.plan.key.split(":")
        companies = src.params.get("companies") or {}
        meta = companies.get(board, {}) if isinstance(companies, dict) else {}
        company_name = str(meta.get("name") or board)
        domain = meta.get("domain")
        entity = EntityRef(type="company", name=company_name, key=f"company:{domain or board}", domain=domain)
        for row in proj.rows:
            # The link shown to a person is always on Lever's own job host; anything else in the field is replaced.
            url = row["hostedUrl"] if host_of(row["hostedUrl"]) == JOBS_HOST else f"https://{JOBS_HOST}/{board}/{row['id']}"
            created = row.get("createdAt")
            workplace = row.get("workplaceType")
            item = Item(
                uid=item_uid(src.name, f"{board}:{row['id']}"), source=src.name, kind="hiring", title=row["text"],
                url=url, text="", published_at=created, entities=[entity],
                dates=[DateRef("posted", created)] if created else [],
                # Many Lever sites fill `team` and leave `department` empty; the team stands in for it then.
                attrs={"board": board, "company": company_name, "location": row.get("location"), "job_id": row["id"],
                       "department": row.get("department") or row.get("team"), "team": row.get("team"),
                       "workplace": workplace, "employment": row.get("commitment"), "country": row.get("country"),
                       "remote": None if workplace in (None, "unspecified") else workplace == "remote"},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
        answered = len(proj.rows) + proj.dropped_rows
        if answered >= PAGE and int(skip) + PAGE >= self._max_postings(src):
            res.notes.append(f"{board}: the last page read ({int(skip) + 1} to {int(skip) + PAGE}) is full, so the site "
                             f"may list more; raise params.max_postings (now {self._max_postings(src)}, at most {MAX_POSTINGS})")
        return res
