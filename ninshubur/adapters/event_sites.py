"""Event websites (Tier 2, batch-admitted): the homepage of each conference in an admitted Tier 1
dataset and up to a few same-host pages that list speakers, sponsors, exhibitors or partners.

Why this adapter is different: its hosts are not fixed. The scheduler hands it the events from the
``items`` table (``state["events"]``) and builds the per-run host allowlist from *their* URLs; a
page can never add a host. Links found on a page are followed only through
``policy.allow_derived_link`` (same host, allowed path terms, at most ``pages_per_event``), and
everything extracted goes through ``event_person.v1`` / ``event_org.v1`` like any other source.
Each event keeps its own health in the source state so one hostile site cannot poison the batch.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin, urlsplit

from ..config import SourceConfig
from ..items import EntityRef, Item, apply_taint, item_uid
from ..parse.htmltree import parse_html
from ..parse.people import extract
from ..policy import check_url, host_of
from ..schemas import get_schema, project
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register

DAY = 86400.0
PATH_TERMS = ("speaker", "sponsor", "exhibitor", "partner", "lineup", "agenda")
_TERM_RE = re.compile("|".join(PATH_TERMS), re.I)
PERSON_ROLES = {"speaker", "host", "panelist"}


@register
class EventSites:
    spec = AdapterSpec(
        name="event_sites", kind="people_company", hosts=frozenset(), schema="event_org.v1",
        schemas=("event_org.v1", "event_person.v1"), expected_content_type="text/html",
        base_interval_s=DAY, max_interval_s=7 * DAY, default_tier=2, legal_basis="public_business_info",
        person_data=True, dynamic_hosts=True, needs=("events",),
        questions=("Which companies paid for presence (sponsor, exhibitor, partner) at events in our markets, so we can offer "
                   "them a measurable channel before or after the event?",
                   "Which speakers at those events hold the titles our profiles target?"),
    )

    # -- planning -------------------------------------------------------------------------------
    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        p = src.params
        per_day = int(p.get("events_per_day", 20))
        include_past = float(p.get("include_past_days", 365)) * DAY
        recrawl = float(p.get("recrawl_upcoming_days", 7)) * DAY
        per_event: dict = state.setdefault("per_event", {})
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        daily = state.setdefault("daily", {"day": day, "n": 0})
        if daily.get("day") != day:
            daily.update(day=day, n=0)
        budget = max(0, per_day - int(daily.get("n", 0)))
        if budget == 0:
            return []
        ranked = []
        for ev in state.get("events", []):
            url = ev.get("url") or ""
            if check_url(url) or not ev.get("uid"):
                continue
            st = per_event.get(ev["uid"], {})
            if st.get("flagged") or int(st.get("fails", 0)) >= 3:
                continue
            start = ev.get("start") or 0.0
            end = ev.get("end") or start
            upcoming = start >= now
            if not upcoming and now - end > include_past:
                continue
            last = float(st.get("last", 0.0))
            if last and (not upcoming or now - last < recrawl):
                continue  # held events are crawled once; upcoming ones at most every `recrawl`
            # never crawled first, then upcoming soonest, then most recently held
            rank = (0 if not last else 1, 0 if upcoming else 1, abs(start - now))
            ranked.append((rank, ev))
        ranked.sort(key=lambda r: r[0])
        plans = []
        for _, ev in ranked[:budget]:
            plans.append(FetchPlan(url=ev["url"], key=f"event_sites:{ev['uid']}:home", max_bytes=2_000_000,
                                   expected_content_type="text/html", purpose=f"event homepage: {ev.get('title', '')[:60]}"))
        return plans

    @staticmethod
    def event_uid(plan: FetchPlan) -> str:
        return plan.key.split(":")[1]

    # -- parsing ----------------------------------------------------------------------------------
    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        uid = self.event_uid(fetched.plan)
        page_url = fetched.final_url or fetched.plan.url
        page_host = host_of(page_url)
        doc = parse_html(fetched.body)
        ext = extract(doc, page_url)
        pp = project(ext.persons, get_schema("event_person.v1"), now, allowlisted_hosts=(page_host,))
        po = project(ext.orgs, get_schema("event_org.v1"), now, allowlisted_hosts=(page_host,))
        merged = pp
        merged.checked += po.checked
        merged.violations += po.violations
        merged.dropped_rows += po.dropped_rows
        merged.findings.extend(f for f in po.findings if f not in merged.findings)
        from ..detectors import Finding
        if doc.limited:
            merged.findings.append(Finding("high", "structure_limit", "parser cap hit"))
        if "too_many_items" in ext.notes and not any(f.code == "too_many_items" for f in merged.findings):
            merged.findings.append(Finding("high", "too_many_items", "card wall over the cap"))
        res = ParseResult(projection=merged)

        entities: list[EntityRef] = []
        taint_codes: list[str] = []
        worst: str | None = None
        kept_p = kept_o = 0
        for row in pp.rows:
            if row["_severity"] == "high":
                res.dropped_rows.append((page_url, row["_taint"], row["name"]))
                continue
            if row["_severity"] == "medium":
                worst = "medium"
                taint_codes.extend(row["_taint"])
            entities.append(EntityRef(type="person", name=row["name"], key=_person_key(row), title=row.get("title"),
                                      company=row.get("company"), handle=row.get("handle"), role=row.get("role", "speaker")))
            kept_p += 1
        for row in po.rows:
            if row["_severity"] == "high":
                res.dropped_rows.append((page_url, row["_taint"], row["name"]))
                continue
            if row["_severity"] == "medium":
                worst = "medium"
                taint_codes.extend(row["_taint"])
            domain = row.get("domain")
            entities.append(EntityRef(type="company", name=row["name"], key=f"company:{domain or _slug(row['name'])}",
                                      domain=domain, role=row.get("role", "sponsor")))
            kept_o += 1
        if res.dropped_rows:
            worst = "medium"  # a page that carried a hostile row is held; the clean rows stay, the hostile ones are quarantined
            taint_codes.append("dropped_rows")

        is_home = fetched.plan.key.endswith(":home")
        page_n = 0 if is_home else int(fetched.plan.key.rsplit(":", 1)[1])
        item = Item(
            uid=item_uid(src.name, f"{uid}:{page_n}"), source=src.name, kind="prospect_page",
            title=f"{fetched.plan.purpose or 'event page'} ({kept_p} people, {kept_o} organisations)",
            url=page_url, text="", published_at=None, entities=entities,
            attrs={"event_uid": uid, "page": page_n, "persons": kept_p, "orgs": kept_o, "layer": ext.person_layer,
                   "contact_data": ext.contact_data, "dropped": len(res.dropped_rows), "notes": ext.notes,
                   "roles": sorted({e.role for e in entities if e.role})},
            legal_basis=src.legal_basis, person_data=kept_p > 0, tier=2,
        )
        item.taint_codes = tuple(dict.fromkeys(taint_codes))
        item.taint_severity = worst
        item.tainted = bool(taint_codes)
        res.items.append(item)

        for host, url in ext.external_hosts:
            res.candidates.append((host, url, "linked from an event sponsor section"))

        if is_home:
            max_pages = int(src.params.get("pages_per_event", 4))
            seen: set[str] = set()
            n = 0
            for href, text, _ in doc.links():
                if n >= max_pages:
                    break
                target = urljoin(page_url, href.strip())
                if "#" in target:
                    target = target.split("#", 1)[0]
                if not target or target in seen or target.rstrip("/") == page_url.rstrip("/"):
                    continue
                if check_url(target):
                    continue
                if host_of(target) != page_host:
                    if _TERM_RE.search(href) or _TERM_RE.search(text):
                        res.candidates.append((host_of(target), target, "event subpage on another host"))
                    continue
                if not (_TERM_RE.search(urlsplit(target).path) or _TERM_RE.search(text)):
                    continue
                seen.add(target)
                n += 1
                res.followups.append(FetchPlan(url=target, key=f"event_sites:{uid}:{n}", max_bytes=2_000_000,
                                               expected_content_type="text/html", purpose=f"{(text or 'page')[:40]} of {fetched.plan.purpose[16:60]}",
                                               derived_from=page_url, anchor_text=text[:80]))
        return res

    # -- per-event state (called by the scheduler) ---------------------------------------------------
    def update_state(self, state: dict, plan: FetchPlan, *, ok: bool, codes: tuple[str, ...], now: float) -> None:
        uid = self.event_uid(plan)
        per_event = state.setdefault("per_event", {})
        st = per_event.setdefault(uid, {"fails": 0, "pages": 0})
        if plan.key.endswith(":home"):
            daily = state.setdefault("daily", {"day": time.strftime("%Y-%m-%d", time.gmtime(now)), "n": 0})
            daily["n"] = int(daily.get("n", 0)) + 1
        if ok:
            st["last"] = now
            st["pages"] = int(st.get("pages", 0)) + 1
            st["fails"] = 0
        else:
            st["fails"] = int(st.get("fails", 0)) + 1
        bad = [c for c in codes if c in ("off_host_redirect", "content_type_mismatch", "body_too_large", "decompression_bomb",
                                         "structure_limit", "too_many_items", "schema_drift", "binary_magic", "cert_error")]
        if bad:
            st["flagged"] = sorted(set(st.get("flagged", [])) | set(bad))
            st["flagged_at"] = now


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "unnamed"


def _person_key(row: dict) -> str:
    if row.get("handle"):
        return "person:" + row["handle"].rsplit("/in/", 1)[-1].lower()
    return "person:" + _slug(row["name"]) + "@" + _slug(row.get("company") or "")
