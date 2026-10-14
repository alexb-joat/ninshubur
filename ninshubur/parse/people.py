"""Extract speakers and sponsoring organisations from an event web page.

Layers, in order of trust: JSON-LD, microdata, repeated HTML cards. The first layer that yields a
plausible set (2 to ``MAX_ROWS`` rows) wins for persons; organisations additionally come from
sponsor/exhibitor/partner sections. Rows are raw candidates: the caller must run them through
``schemas.project`` (``event_person.v1`` / ``event_org.v1``) which sanitises, validates the name
rule and taint-scans every text field. Emails and phone numbers are only counted, never returned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..detectors import has_hidden_unicode, scan_text
from .htmltree import Doc, Node, card_groups, iter_nodes, json_ld, microdata, walk_ld

MAX_ROWS = 300
SECTION_TERMS = {"sponsor": "sponsor", "exhibitor": "exhibitor", "partner": "partner", "organizer": "organizer", "organiser": "organizer"}
_LINKEDIN = re.compile(r"^https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9\-_%]{3,100})/?(?:[?#].*)?$", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]?){2,4}\d{2,4}(?!\d)")
_NAME = re.compile(r"^(?!.*(\d|@|https?:))[^\W\d_][\w'’.-]*(?: [^\W\d_][\w'’.-]*){1,4}$")
_SPLIT = re.compile(r"\s+(?:at|@|bei|chez|en)\s+|\s*[|,·•–—-]\s*", re.I)
_TITLE_WORDS = re.compile(r"\b(ceo|cto|coo|cfo|cmo|cro|vp|head|director|manager|lead|founder|co-founder|engineer|developer|architect|"
                          r"scientist|analyst|consultant|president|partner|principal|evangelist|advocate|officer|chief|senior|staff|"
                          r"professor|researcher|owner|specialist|strategist|designer|product|marketing|sales|growth)\b", re.I)
_GENERIC_ALT = re.compile(r"^(logo|image|img|photo|picture|icon|sponsor|partner|exhibitor|speaker|avatar)s?$", re.I)


@dataclass
class Extraction:
    persons: list[dict] = field(default_factory=list)
    orgs: list[dict] = field(default_factory=list)
    person_layer: str | None = None
    notes: list[str] = field(default_factory=list)
    contact_data: int = 0  # emails/phones seen on the page: counted, never stored
    external_hosts: list[tuple[str, str]] = field(default_factory=list)  # (host, url) for the candidates table


def normalise_handle(href: str | None) -> str | None:
    if not href:
        return None
    m = _LINKEDIN.match(href.strip())
    if not m:
        return None
    return f"https://www.linkedin.com/in/{m.group(1).rstrip('/')}"


_HIDDEN = re.compile("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def looks_like_name(s: str) -> bool:
    """Shape test only; hidden characters are ignored here so that a disguised name is *kept* and then
    flagged by the taint scan in ``project`` rather than silently skipped."""
    s = _HIDDEN.sub("", s).strip()
    return 3 <= len(s) <= 60 and bool(_NAME.match(s)) and any(w[:1].isupper() for w in s.split())


def _part(p: str) -> str | None:
    """A title or company fragment: short, no address/email/URL, not a sentence."""
    p = p.strip(" \t.;:")
    if not (2 <= len(p) <= 80) or "@" in p or "http" in p.lower() or ". " in p or p.count(" ") > 8:
        return None
    return p


def split_title_company(line: str) -> tuple[str | None, str | None]:
    """'CTO at Acme' / 'CTO, Acme' / 'Acme | CTO' -> (title, company); a lone line is a title if it has
    title words, else a company."""
    line = line.strip()
    if not line or len(line) > 160:
        return None, None
    parts = [_part(p) for p in _SPLIT.split(line)]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        a, b = parts[0], parts[-1]
        if _TITLE_WORDS.search(a) and not _TITLE_WORDS.search(b):
            return a, b
        if _TITLE_WORDS.search(b) and not _TITLE_WORDS.search(a):
            return b, a
        return a, b
    single = _part(line)
    if not single:
        return None, None
    if _TITLE_WORDS.search(single):
        return single, None
    return None, single


def _ld_name(v) -> str | None:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        n = v.get("name")
        return n.strip() if isinstance(n, str) and n.strip() else None
    return None


def _ld_persons(data, role: str, out: list[dict]) -> None:
    for p in walk_ld(data, {"Person"}):
        name = _ld_name(p)
        if not name:
            continue
        works = p.get("worksFor") or p.get("affiliation") or p.get("memberOf")
        company = _ld_name(works[0] if isinstance(works, list) and works else works)
        handle = None
        same = p.get("sameAs")
        for u in (same if isinstance(same, list) else [same]):
            if isinstance(u, str):
                handle = normalise_handle(u)
                if handle:
                    break
        out.append({"name": name, "title": p.get("jobTitle") if isinstance(p.get("jobTitle"), str) else None,
                    "company": company, "handle": handle, "role": role, "layer": "jsonld"})


def _ld_orgs(data, out: list[dict]) -> None:
    for ev in walk_ld(data, {"Event", "BusinessEvent", "EducationEvent", "ExhibitionEvent"}):
        for key, role in (("sponsor", "sponsor"), ("funder", "sponsor"), ("organizer", "organizer")):
            v = ev.get(key)
            for o in (v if isinstance(v, list) else [v]):
                name = _ld_name(o)
                if name:
                    domain = None
                    if isinstance(o, dict) and isinstance(o.get("url"), str):
                        domain = (urlsplit(o["url"]).hostname or "").lower() or None
                    out.append({"name": name, "domain": domain, "role": role, "layer": "jsonld"})


def _section_role(node: Node) -> str | None:
    """Role implied by the nearest heading/anchor/class text above this node, if it names a sponsor-like section."""
    for n in [node, *node.ancestors()]:
        hay = (n.attrs.get("class", "") + " " + n.attrs.get("name", "")).lower()
        for term, role in SECTION_TERMS.items():
            if term in hay:
                return role
        for h in n.find_all(pred=lambda x: x.tag in ("h1", "h2", "h3", "h4", "h5")):
            t = h.text(80).lower()
            for term, role in SECTION_TERMS.items():
                if term in t:
                    return role
            break
    return None


def _cards_persons(doc: Doc, out: list[dict], notes: list[str]) -> None:
    best: list[dict] = []
    for group in card_groups(doc):
        if len(group) > MAX_ROWS * 2:
            notes.append("too_many_items")  # a planted wall of cards: flagged, not harvested
            continue
        rows: list[dict] = []
        for card in group:
            lines = card.lines(max_lines=6)
            if not lines:
                continue
            name_i = next((i for i, l in enumerate(lines) if looks_like_name(l)), None)
            if name_i is None:
                continue
            handle = None
            for a in card.find_all("a"):
                handle = normalise_handle(a.attrs.get("href"))
                if handle:
                    break
            has_img = bool(card.find_all("img"))
            if not (handle or has_img):
                continue
            title = company = None
            for l in lines[name_i + 1: name_i + 3]:
                t, c = split_title_company(l)
                title = title or t
                company = company or c
            # The whole card (bio included) is taint-scanned here; only the fields below are ever stored.
            pre = [(f.severity, f.code) for f in scan_text(card.text(1500))]
            rows.append({"name": lines[name_i], "title": title, "company": company, "handle": handle, "role": "speaker",
                         "layer": "cards", "_pretaint": pre})
        if len(rows) > len(best) and 2 <= len(rows) <= MAX_ROWS * 2:
            best = rows
    out.extend(best)


def _section_orgs(doc: Doc, page_host: str, out: list[dict], ext: Extraction) -> None:
    seen: set[str] = set()
    for group in card_groups(doc, min_size=2):
        role = _section_role(group[0].parent) if group[0].parent else None
        if not role:
            continue
        for card in group:
            name = domain = None
            for a in card.find_all("a"):
                h = (urlsplit(a.attrs.get("href", "")).hostname or "").lower()
                if h and h != page_host and not h.endswith("." + page_host) and "linkedin.com" not in h:
                    domain = h[4:] if h.startswith("www.") else h
                    ext.external_hosts.append((h, a.attrs["href"]))
                    break
            for img in card.find_all("img"):
                alt = (img.attrs.get("alt") or "").strip()
                if alt and not _GENERIC_ALT.match(alt) and len(alt) <= 120:
                    name = re.sub(r"\s*logos?$", "", alt, flags=re.I).strip() or None
                    break
            if not name:
                t = card.text(120)
                if t and len(t) <= 80 and not looks_like_name(t):
                    name = t
            if not name and domain:
                name = domain.split(".")[0].capitalize()
            if not name:
                continue
            k = (domain or name.lower())
            if k in seen:
                continue
            seen.add(k)
            out.append({"name": name, "domain": domain, "role": role, "layer": "section"})


def extract(doc: Doc, page_url: str) -> Extraction:
    ext = Extraction()
    page_host = (urlsplit(page_url).hostname or "").lower()
    text = doc.text(200_000)
    ext.contact_data = len(_EMAIL.findall(text)) + len(_PHONE.findall(text))

    persons: list[dict] = []
    orgs: list[dict] = []
    for data in json_ld(doc):
        _ld_persons(data, "speaker", persons)
        _ld_orgs(data, orgs)
    if 2 <= len(persons) <= MAX_ROWS * 2:
        ext.person_layer = "jsonld"
    else:
        persons = []
        for md in microdata(doc):
            if md["type"] == "Person" and md["props"].get("name"):
                p = md["props"]
                persons.append({"name": p["name"], "title": p.get("jobTitle"), "company": p.get("worksFor") or p.get("affiliation"),
                                "handle": normalise_handle(p.get("sameAs") or p.get("url")), "role": "speaker", "layer": "microdata"})
            elif md["type"] == "Organization" and md["props"].get("name"):
                role = _section_role(md["node"]) or "sponsor"
                orgs.append({"name": md["props"]["name"], "domain": (urlsplit(md["props"].get("url", "")).hostname or "").lower() or None,
                             "role": role, "layer": "microdata"})
        if 2 <= len(persons) <= MAX_ROWS * 2:
            ext.person_layer = "microdata"
        else:
            persons = []
            _cards_persons(doc, persons, ext.notes)
            if persons:
                ext.person_layer = "cards"
    if len(persons) > MAX_ROWS:
        ext.notes.append(f"persons truncated from {len(persons)}")
    _section_orgs(doc, page_host, orgs, ext)
    # dedupe
    seen_p: set[str] = set()
    for p in persons:
        k = p.get("handle") or (p["name"].lower() + "|" + (p.get("company") or "").lower())
        if k in seen_p:
            continue
        seen_p.add(k)
        ext.persons.append(p)
    seen_o: set[str] = set()
    for o in orgs:
        k = o.get("domain") or o["name"].lower()
        if k in seen_o:
            continue
        seen_o.add(k)
        ext.orgs.append(o)
    if doc.limited:
        ext.notes.append("structure_limit")
    return ext
