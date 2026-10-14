"""Hacker News via the Algolia search API. Stories are Tier 2 (pointers); comments are Tier 3
user content and arrive taint-scanned.

The `whoishiring` account posts monthly "Who is hiring?" and "Who wants to be hired?" threads;
the "Freelancer? Seeking freelancer?" thread stopped after October 2025 but is still handled if
it returns. Only hiring and freelancer threads are followed; "Who wants to be hired?" lists
people, not companies, and is skipped. Follow-ups are templated from the story *id*, never from
a URL string in the payload.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..config import SourceConfig
from ..items import DateRef, EntityRef, Item, apply_taint, item_uid
from ..policy import host_of
from ..schemas import sanitize_str
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register, run_projection

QUERY_RE = re.compile(r"^[\w .+#-]{1,60}$")
SEEKING_FREELANCER = re.compile(r"\bSEEKING\s+FREELANCER\b", re.IGNORECASE)
SEEKING_WORK = re.compile(r"\bSEEKING\s+WORK\b", re.IGNORECASE)
WHO_IS_HIRING = re.compile(r"^Ask HN: Who is hiring\b", re.IGNORECASE)
FREELANCER_THREAD = re.compile(r"^Ask HN: Freelancer\b", re.IGNORECASE)
ROLE_TERMS = ("sdr", "bdr", "sales development", "business development", "account executive", "sales", "growth",
              "marketing", "revenue", "demand generation", "partnerships", "customer success")
_ROLE_RES = [(t, re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.IGNORECASE)) for t in ROLE_TERMS]
FRESH_THREAD_S = 45 * 86400
HN_ITEM = "https://news.ycombinator.com/item?id="
BASE = "https://hn.algolia.com/api/v1/search_by_date"
COMPANY_SLUG = re.compile(r"[^a-z0-9]+")


@register
class HackerNews:
    spec = AdapterSpec(
        name="hn", kind="service_requests", hosts=frozenset({"hn.algolia.com"}), schema="hn_story.v1",
        schemas=("hn_story.v1", "hn_comment.v1"), expected_content_type="application/json",
        base_interval_s=2 * 3600, max_interval_s=12 * 3600, default_tier=2, legal_basis="public_business_info",
        questions=("Which companies say publicly that they are hiring sales, growth or marketing roles, and since when?",
                   "Who is publicly asking for the kind of service we sell, and how do they want to be contacted?",
                   "Which new tools or launches match the problems our profiles care about?"),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        plans: list[FetchPlan] = []
        if src.params.get("whoishiring", True):
            plans.append(FetchPlan(url=f"{BASE}?tags=story,author_whoishiring&hitsPerPage=6", key="hn:whoishiring:latest",
                                   purpose="latest who-is-hiring threads"))
        if src.params.get("freelancer_threads", True):
            plans.append(FetchPlan(url=f"{BASE}?tags=story,author_whoishiring&query=freelancer&hitsPerPage=10",
                                   key="hn:whoishiring:freelancer", purpose="freelancer threads"))
        for q in src.params.get("show_hn_queries", []):
            q = str(q)
            if QUERY_RE.match(q):
                plans.append(FetchPlan(url=f"{BASE}?tags=show_hn&query={quote(q)}&hitsPerPage=50", key=f"hn:show:{q}", purpose=f"Show HN {q}"))
        for q in src.params.get("queries", []):
            q = str(q)
            if QUERY_RE.match(q):
                plans.append(FetchPlan(url=f"{BASE}?tags=story&query={quote(q)}&hitsPerPage=50", key=f"hn:story:{q}", purpose=f"stories {q}"))
        return plans

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        if fetched.plan.schema == "hn_comment.v1":
            return self._parse_comments(src, fetched, now)
        return self._parse_stories(src, fetched, now)

    def _parse_stories(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, "hn_story.v1", now, spec=self.spec)
        res = ParseResult(projection=proj)
        thread_search = fetched.plan.key.startswith("hn:whoishiring:")
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
            title = row["title"]
            kind = "hiring" if WHO_IS_HIRING.match(title) else ("freelancer" if FREELANCER_THREAD.match(title) else None)
            if thread_search and kind and now - row["created_at"] < FRESH_THREAD_S:
                res.followups.append(FetchPlan(
                    url=f"{BASE}?tags=comment,story_{sid}&hitsPerPage=200&page=0",
                    key=f"hn:comments:{sid}", schema="hn_comment.v1", purpose=f"{kind} thread {sid} comments"))
        return res

    def _parse_comments(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        proj = run_projection(fetched, "hn_comment.v1", now, spec=self.spec)
        res = ParseResult(projection=proj)
        for row in proj.rows:
            text = row.get("comment_text") or ""
            cid = row["objectID"]
            common = dict(source=src.name, url=f"{HN_ITEM}{cid}", text=text, published_at=row["created_at"],
                          dates=[DateRef("posted", row["created_at"])], legal_basis=src.legal_basis, tier=3)
            attrs = {"hn_id": cid, "story_id": row.get("story_id"), "author": row.get("author")}
            if SEEKING_FREELANCER.search(text):
                item = Item(uid=item_uid(src.name, f"comment:{cid}"), kind="service_request", title=text[:80] or f"HN comment {cid}",
                            attrs={**attrs, "classification": "SEEKING_FREELANCER"}, **common)
            elif SEEKING_WORK.search(text):
                item = Item(uid=item_uid(src.name, f"comment:{cid}"), kind="social", title=text[:80] or f"HN comment {cid}",
                            attrs={**attrs, "classification": "SEEKING_WORK"}, **common)
            else:
                company = company_of(text)
                roles = [t for t, rx in _ROLE_RES if rx.search(text)]
                if company and roles:
                    slug = COMPANY_SLUG.sub("-", company.lower()).strip("-")[:40]
                    item = Item(uid=item_uid(src.name, f"comment:{cid}"), kind="hiring",
                                title=f"{company}: {', '.join(roles[:3])}",
                                entities=[EntityRef(type="company", name=company, key=f"company:{slug}")],
                                attrs={**attrs, "classification": "HIRING", "company": company, "roles": roles, "board": slug},
                                **common)
                else:
                    item = Item(uid=item_uid(src.name, f"comment:{cid}"), kind="social", title=text[:80] or f"HN comment {cid}",
                                attrs={**attrs, "classification": "OTHER"}, **common)
            apply_taint(item, row)
            res.items.append(item)
        return res


def company_of(text: str) -> str | None:
    """'Company | Role | Location | ...' convention of the hiring threads: the first segment, sanitised.
    Returns None when there is no pipe or the segment looks like a sentence rather than a name."""
    if "|" not in text:
        return None
    head = sanitize_str(text.split("|", 1)[0], 80)
    if not head or len(head) < 2 or len(head.split()) > 8 or head.endswith((".", "?", "!")):
        return None
    return head
