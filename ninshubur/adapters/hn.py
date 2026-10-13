"""Hacker News via the Algolia search API. Stories are Tier 2 (pointers); comments in
"Freelancer? Seeking freelancer?" threads are Tier 3 user content and arrive taint-scanned.

Follow-ups are templated from the story *id* found in a story record, never from a URL string.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..config import SourceConfig
from ..items import DateRef, Item, apply_taint, item_uid
from ..policy import host_of
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register, run_projection

QUERY_RE = re.compile(r"^[\w .+#-]{1,60}$")
SEEKING_FREELANCER = re.compile(r"\bSEEKING\s+FREELANCER\b", re.IGNORECASE)
SEEKING_WORK = re.compile(r"\bSEEKING\s+WORK\b", re.IGNORECASE)
FRESH_THREAD_S = 45 * 86400
HN_ITEM = "https://news.ycombinator.com/item?id="


@register
class HackerNews:
    spec = AdapterSpec(
        name="hn", kind="service_requests", hosts=frozenset({"hn.algolia.com"}), schema="hn_story.v1",
        schemas=("hn_story.v1", "hn_comment.v1"), expected_content_type="application/json",
        base_interval_s=2 * 3600, max_interval_s=12 * 3600, default_tier=2, legal_basis="public_business_info",
        questions=("Who is publicly asking for the kind of service we sell, and how do they want to be contacted?",
                   "Which new tools or launches match the problems our profiles care about?"),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        plans: list[FetchPlan] = []
        base = "https://hn.algolia.com/api/v1/search_by_date"
        if src.params.get("freelancer_threads", True):
            plans.append(FetchPlan(url=f"{base}?tags=story,author_whoishiring&query=freelancer&hitsPerPage=10",
                                   key="hn:whoishiring:freelancer", purpose="freelancer threads"))
        for q in src.params.get("show_hn_queries", []):
            q = str(q)
            if QUERY_RE.match(q):
                plans.append(FetchPlan(url=f"{base}?tags=show_hn&query={quote(q)}&hitsPerPage=50",
                                       key=f"hn:show:{q}", purpose=f"Show HN {q}"))
        for q in src.params.get("queries", []):
            q = str(q)
            if QUERY_RE.match(q):
                plans.append(FetchPlan(url=f"{base}?tags=story&query={quote(q)}&hitsPerPage=50",
                                       key=f"hn:story:{q}", purpose=f"stories {q}"))
        return plans

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        if fetched.plan.schema == "hn_comment.v1":
            return self._parse_comments(src, fetched, now)
        return self._parse_stories(src, fetched, now)

    def _parse_stories(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, "hn_story.v1", now, spec=self.spec)
        res = ParseResult(projection=proj)
        is_freelancer_search = fetched.plan.key == "hn:whoishiring:freelancer"
        for row in proj.rows:
            sid = row["objectID"]
            item = Item(
                uid=item_uid(src.name, f"story:{sid}"), source=src.name, kind="thread", title=row["title"],
                url=row.get("url") or f"{HN_ITEM}{sid}", text="", published_at=row["created_at"],
                dates=[DateRef("posted", row["created_at"])],
                attrs={"hn_id": sid, "author": row.get("author"), "points": row.get("points"),
                       "num_comments": row.get("num_comments"), "hn_url": f"{HN_ITEM}{sid}"},
                legal_basis=src.legal_basis, tier=2,
            )
            apply_taint(item, row)
            res.items.append(item)
            if row.get("url"):
                h = host_of(row["url"])
                if h and h != "news.ycombinator.com":
                    res.candidates.append((h, row["url"], "linked from HN story"))
            if is_freelancer_search and "freelancer" in row["title"].lower() and now - row["created_at"] < FRESH_THREAD_S:
                res.followups.append(FetchPlan(
                    url=f"https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{sid}&hitsPerPage=200&page=0",
                    key=f"hn:comments:{sid}", schema="hn_comment.v1", purpose=f"comments of thread {sid}"))
        return res

    def _parse_comments(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, "hn_comment.v1", now, spec=self.spec)
        res = ParseResult(projection=proj)
        for row in proj.rows:
            text = row.get("comment_text") or ""
            if SEEKING_FREELANCER.search(text):
                cls = "SEEKING_FREELANCER"
            elif SEEKING_WORK.search(text):
                cls = "SEEKING_WORK"
            else:
                cls = "OTHER"
            cid = row["objectID"]
            item = Item(
                uid=item_uid(src.name, f"comment:{cid}"), source=src.name,
                kind="service_request" if cls == "SEEKING_FREELANCER" else "social",
                title=text[:80] or f"HN comment {cid}", url=f"{HN_ITEM}{cid}", text=text, published_at=row["created_at"],
                dates=[DateRef("posted", row["created_at"])],
                attrs={"hn_id": cid, "story_id": row.get("story_id"), "author": row.get("author"), "classification": cls},
                legal_basis=src.legal_basis, tier=3,
            )
            apply_taint(item, row)
            res.items.append(item)
        return res
