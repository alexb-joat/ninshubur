"""What the explorer reads from a page. This module runs *inside* the bwrap sandbox (``sandbox.py``), so it imports
only the standard library and the bounded HTML parser: no network client, no policy file, no database, no YAML.

It returns links and a few declared fields, nothing else:

- ``links``: absolute http(s) URLs from ``<a href>``, resolved against the page URL (a ``<base>`` tag is ignored, so
  a page can't re-point its relative links), without links hidden from people (``hidden``, ``aria-hidden="true"``,
  ``display:none``, ``visibility:hidden`` on the link or an ancestor: honeypots) and without ``rel`` nofollow, ugc
  or sponsored (the site says not to vouch for them);
- ``records``: schema.org ``Event`` (and its subtypes) and ``JobPosting`` objects from JSON-LD, reduced to name, url,
  dates, place and organisation. Person objects are dropped wherever they appear: the explorer stores no person
  fields;
- ``title``, ``description``, ``lang``, the visible ``text`` (first 20,000 characters) and ``attr_texts`` (alt,
  title, aria-label and meta text), so the parent can scan the page and its hidden channels for injection. The text
  is scanned and then dropped: it is never stored.

The parent treats every byte of this output as untrusted: it checks the shape and sizes again, scans the text for
taint, and sends links through the frontier like any other offer.
"""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..parse.htmltree import KEEP_ATTRS, Doc, Node, parse_html

VERSION = 1
KEEP = KEEP_ATTRS | {"hidden", "aria-hidden", "style", "aria-label", "lang"}
MAX_LINKS = 200
MAX_URL = 2048
MAX_RECORDS = 50
MAX_ATTR_TEXTS = 40
MAX_TEXT = 20_000
MAX_HEADER = 4096
_WS = re.compile(r"\s+")
_HIDING_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_SKIP_REL = frozenset({"nofollow", "ugc", "sponsored"})
_META_TEXT = frozenset({"description", "keywords", "og:title", "og:description", "twitter:title", "twitter:description"})
_EXTRA_EVENT_TYPES = frozenset({"Hackathon", "Festival", "CourseInstance"})


def _clip(s, n: int) -> str:
    return _WS.sub(" ", s).strip()[:n] if isinstance(s, str) else ""


def _hidden(node: Node) -> bool:
    for n in (node, *node.ancestors()):
        a = n.attrs
        if "hidden" in a or a.get("aria-hidden", "").strip().lower() == "true" or _HIDING_STYLE.search(a.get("style", "")):
            return True
    return False


def _link(base: str, href: str) -> str | None:
    try:
        u = urlsplit(urljoin(base, href.strip()))
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    return urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))


def links(doc: Doc, base: str) -> tuple[list[str], int]:
    out: list[str] = []
    seen: set[str] = set()
    hidden = 0
    for node in doc.find_all("a"):
        href = node.attrs.get("href")
        if not href:
            continue
        if _hidden(node):
            hidden += 1
            continue
        if _SKIP_REL & set(node.attrs.get("rel", "").lower().split()):
            continue
        u = _link(base, href)
        if u and u not in seen and len(u) <= MAX_URL:
            seen.add(u)
            out.append(u)
            if len(out) >= MAX_LINKS:
                break
    return out, hidden


def _types(obj: dict) -> set[str]:
    t = obj.get("@type")
    return {t} if isinstance(t, str) else {x for x in t if isinstance(x, str)} if isinstance(t, list) else set()


def _is_person(v) -> bool:
    return isinstance(v, dict) and "Person" in _types(v)


def _name(v, n: int = 200) -> str:
    """A text value, the first string of a list, or the name of an object, never of a Person."""
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, (str, dict)) and not _is_person(x)), "")
    if isinstance(v, dict):
        return "" if _is_person(v) else _clip(v.get("name"), n)
    return _clip(v, n)


def _place(v) -> str:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        if "VirtualLocation" in _types(v):
            return "online"
        if v.get("name"):
            return _clip(v.get("name"), 160)
        a = v.get("address")
        if isinstance(a, dict):
            return _clip(", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry") if isinstance(
                a.get("addressCountry"), str) else _name(a.get("addressCountry"))) if isinstance(x, str) and x), 160)
        return _clip(a, 160)
    return _clip(v, 160)


def _record(obj: dict, base: str) -> dict | None:
    types = _types(obj)
    if "JobPosting" in types:
        kind, name = "hiring", _name(obj.get("title")) or _name(obj.get("name"))
        org = obj.get("hiringOrganization")
        org = "" if _is_person(org) else (_clip(org, 160) if isinstance(org, str) else _name(org, 160))
        start, end = _clip(obj.get("datePosted"), 40), _clip(obj.get("validThrough"), 40)
        place = "remote" if _clip(obj.get("jobLocationType"), 40).upper() == "TELECOMMUTE" else _place(obj.get("jobLocation"))
    elif any(t.endswith("Event") for t in types) or types & _EXTRA_EVENT_TYPES:
        kind, name = "event", _name(obj.get("name"))
        org = obj.get("organizer")
        org = _name(org, 160) if isinstance(org, (dict, list)) else ""  # a bare string may be a person's name: dropped
        start, end = _clip(obj.get("startDate"), 40), _clip(obj.get("endDate"), 40)
        place = _place(obj.get("location"))
    else:
        return None
    if not name:
        return None
    url = obj.get("url")
    url = _link(base, url) if isinstance(url, str) else None
    if url and len(url) > MAX_URL:
        url = None  # a tracking link without end: the record goes on without it, as links() drops such a link
    return {"type": kind, "name": name, "url": url or "", "start": start, "end": end, "place": place, "org": org}


def _walk(obj, budget: int = 20_000):
    stack, seen = [obj], 0
    while stack and seen < budget:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            yield cur
            stack.extend(v for k, v in cur.items() if isinstance(v, (dict, list)) and not _is_person(v))
        elif isinstance(cur, list):
            stack.extend(x for x in cur if not _is_person(x))


def _load(raw: str):
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return None


def records(blobs: list, base: str) -> list[dict]:
    out: list[dict] = []
    for blob in blobs:
        for obj in _walk(blob):
            r = _record(obj, base)
            if r and r not in out:
                out.append(r)
                if len(out) >= MAX_RECORDS:
                    return out
    return out


def attr_texts(doc: Doc) -> list[str]:
    out: list[str] = []
    for n in doc.find_all():
        a = n.attrs
        vals = [a.get("alt"), a.get("title"), a.get("aria-label")]
        if n.tag == "meta" and (a.get("name") or a.get("property") or "").lower() in _META_TEXT:
            vals.append(a.get("content"))
        for v in vals:
            if isinstance(v, str) and v.strip():
                out.append(v[:300])
                if len(out) >= MAX_ATTR_TEXTS:
                    return out
    return out


def _meta(doc: Doc, *names: str) -> str:
    for n in doc.find_all("meta"):
        if (n.attrs.get("name") or n.attrs.get("property") or "").lower() in names:
            return n.attrs.get("content", "")
    return ""


def extract(body: bytes, url: str, content_type: str) -> dict:
    out = {"v": VERSION, "title": "", "description": "", "lang": "", "links": [], "hidden_links": 0, "records": [],
           "attr_texts": [], "text": "", "limited": False, "nodes": 0}
    if "json" in content_type.lower():
        data = _load(body.decode("utf-8", errors="replace"))
        out["records"] = records([data], url) if data is not None else []
        out["limited"] = data is None
        return out
    doc = parse_html(body, keep_attrs=KEEP)
    titles = doc.find_all("title")
    html = doc.find_all("html")
    out.update(
        title=_clip(titles[0].own_text(), 300) if titles else "",
        description=_clip(_meta(doc, "description", "og:description"), 500),
        lang=_clip(html[0].attrs.get("lang", ""), 20) if html else "",
        records=records([b for b in (_load(raw) for raw in doc.ld_blocks) if b is not None], url),
        attr_texts=attr_texts(doc), text=doc.text(MAX_TEXT), limited=doc.limited, nodes=doc.count)
    out["links"], out["hidden_links"] = links(doc, url)
    return out


def sandbox_main() -> None:
    """Entry point inside the sandbox: one JSON header line (``url``, ``content_type``, ``max_bytes``), then the
    body; one JSON object out on stdout."""
    raw = sys.stdin.buffer
    header = json.loads(raw.readline(MAX_HEADER))
    body = raw.read(int(header["max_bytes"]) + 1)
    if len(body) > int(header["max_bytes"]):
        raise SystemExit("body over max_bytes")
    sys.stdout.buffer.write(json.dumps(extract(body, str(header["url"]), str(header["content_type"]))).encode())
