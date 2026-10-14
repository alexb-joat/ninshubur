"""A bounded HTML parser for hostile pages.

Built on the stdlib ``html.parser``: no rendering, no scripts, no network, no regex over raw markup.
Elements that can carry active content are discarded at parse time and never enter the tree
(``script`` is kept only when it is ``application/ld+json``, as text). Hard caps on node count,
depth, attribute and text size make a malicious page cost a bounded amount of memory and time;
when a cap trips, parsing stops and ``Doc.limited`` is set so the caller can flag the page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterator

DROP = frozenset({"script", "style", "template", "iframe", "object", "embed", "svg", "noscript", "canvas", "audio", "video", "math"})
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
KEEP_ATTRS = frozenset({"href", "alt", "title", "itemprop", "itemtype", "itemscope", "class", "content", "datetime", "name", "property", "type", "rel"})
MAX_ATTR = 2048
MAX_TEXT = 4096
MAX_LD_BLOCK = 200_000
MAX_LD_BLOCKS = 50
_WS = re.compile(r"\s+")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: "Node | None"
    depth: int
    children: list["Node"] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(sorted(self.attrs.get("class", "").split()))

    @property
    def key(self) -> str:
        return self.tag + "." + ".".join(self.classes)

    def own_text(self) -> str:
        return _WS.sub(" ", " ".join(self.texts)).strip()

    def text(self, max_len: int = 500) -> str:
        parts: list[str] = []
        total = 0
        for n in iter_nodes(self):
            for t in n.texts:
                if not t.strip():
                    continue
                parts.append(t)
                total += len(t)
                if total > max_len * 4:
                    break
        return _WS.sub(" ", " ".join(parts)).strip()[:max_len]

    def lines(self, max_lines: int = 8, max_len: int = 160) -> list[str]:
        """Visible text split at block boundaries: one entry per block-level descendant with text."""
        out: list[str] = []
        for n in iter_nodes(self):
            if n.tag in BLOCK and n.texts:
                t = n.own_text()
                if t:
                    out.append(t[:max_len])
            elif n is self and n.texts:
                t = n.own_text()
                if t:
                    out.append(t[:max_len])
            if len(out) >= max_lines:
                break
        return out

    def find_all(self, tag: str | None = None, pred: Callable[["Node"], bool] | None = None) -> list["Node"]:
        return [n for n in iter_nodes(self) if (tag is None or n.tag == tag) and (pred is None or pred(n))]

    def ancestors(self) -> Iterator["Node"]:
        n = self.parent
        while n is not None:
            yield n
            n = n.parent


BLOCK = frozenset({"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "span", "a", "td", "th", "dt", "dd", "figcaption",
                   "small", "strong", "em", "b", "i", "label", "section", "article", "header", "footer", "cite", "blockquote"})


def iter_nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


@dataclass
class Doc:
    root: Node
    count: int = 0
    limited: bool = False
    dropped: int = 0
    scripts: int = 0
    ld_blocks: list[str] = field(default_factory=list)
    comments: int = 0

    def find_all(self, tag: str | None = None, pred: Callable[[Node], bool] | None = None) -> list[Node]:
        return self.root.find_all(tag, pred)

    def links(self) -> list[tuple[str, str, Node]]:
        return [(n.attrs["href"], n.text(200), n) for n in self.root.find_all("a") if n.attrs.get("href")]

    def text(self, max_len: int = 20000) -> str:
        return self.root.text(max_len)


class _Parser(HTMLParser):
    def __init__(self, max_nodes: int, max_depth: int):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None, 0)
        self.doc = Doc(self.root)
        self.stack: list[Node] = [self.root]
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.drop_depth = 0  # > 0 while inside a dropped element
        self.ld_capture: list[str] | None = None
        self.ld_size = 0
        self.stopped = False

    # -- helpers
    def _attrs(self, attrs) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in attrs:
            if k in KEEP_ATTRS and v is not None:
                out[k] = v[:MAX_ATTR]
            elif k in KEEP_ATTRS:
                out[k] = ""
        return out

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        if self.drop_depth:
            if tag in DROP:
                self.drop_depth += 1
            return
        if tag == "script":
            a = dict(attrs)
            if (a.get("type") or "").strip().lower() == "application/ld+json" and len(self.doc.ld_blocks) < MAX_LD_BLOCKS:
                self.ld_capture = []
                self.ld_size = 0
            else:
                self.doc.scripts += 1
                self.drop_depth = 1
            return
        if tag in DROP:
            self.doc.dropped += 1
            self.drop_depth = 1
            return
        parent = self.stack[-1]
        if parent.depth + 1 > self.max_depth or self.doc.count >= self.max_nodes:
            self.doc.limited = True
            self.stopped = True
            return
        node = Node(tag, self._attrs(attrs), parent, parent.depth + 1)
        parent.children.append(node)
        self.doc.count += 1
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and not self.drop_depth and not self.stopped and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if self.stopped:
            return
        if tag == "script" and self.ld_capture is not None:
            self.doc.ld_blocks.append("".join(self.ld_capture))
            self.ld_capture = None
            return
        if self.drop_depth:
            if tag in DROP or tag == "script":
                self.drop_depth -= 1
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.stopped:
            return
        if self.ld_capture is not None:
            if self.ld_size < MAX_LD_BLOCK:
                self.ld_capture.append(data[: MAX_LD_BLOCK - self.ld_size])
                self.ld_size += len(data)
            return
        if self.drop_depth or not data or not data.strip():
            return
        node = self.stack[-1]
        if sum(len(t) for t in node.texts) < MAX_TEXT:
            node.texts.append(data[:MAX_TEXT])

    def handle_comment(self, data):
        self.doc.comments += 1


def parse_html(body: bytes | str, *, max_nodes: int = 200_000, max_depth: int = 200) -> Doc:
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    p = _Parser(max_nodes, max_depth)
    try:
        p.feed(body)
        p.close()
    except Exception:  # noqa: BLE001 - html.parser can raise on pathological input; the page is then just "limited"
        p.doc.limited = True
    return p.doc


def json_ld(doc: Doc, *, max_depth: int = 32) -> list:
    """Parsed JSON-LD objects, depth-checked; invalid blocks are skipped."""
    from ..detectors import depth_of
    out = []
    for raw in doc.ld_blocks:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if depth_of(data, limit=max_depth) > max_depth:
            continue
        out.append(data)
    return out


def walk_ld(obj, want: set[str]) -> Iterator[dict]:
    """Yield every dict in a JSON-LD structure whose @type (or any of its @type list) is in ``want``."""
    stack = [obj]
    seen = 0
    while stack and seen < 50_000:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, dict):
            t = cur.get("@type")
            types = {t} if isinstance(t, str) else set(t) if isinstance(t, list) else set()
            if types & want:
                yield cur
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)


def microdata(doc: Doc, want: tuple[str, ...] = ("Person", "Organization")) -> list[dict]:
    """``itemscope`` nodes whose itemtype ends with one of ``want``; props from descendant itemprops
    (own text or ``content``), stopping at nested itemscopes."""
    out = []
    for n in doc.find_all(pred=lambda x: "itemscope" in x.attrs):
        itype = n.attrs.get("itemtype", "").rstrip("/").rsplit("/", 1)[-1]
        if itype not in want:
            continue
        props: dict[str, str] = {}
        stack = list(n.children)
        while stack:
            c = stack.pop()
            if "itemscope" in c.attrs:
                nested = c.attrs.get("itemtype", "").rstrip("/").rsplit("/", 1)[-1]
                if nested == "Organization" and c.attrs.get("itemprop") in ("worksFor", "affiliation", "memberOf"):
                    name = next((d.attrs.get("content") or d.own_text() for d in iter_nodes(c) if d.attrs.get("itemprop") == "name"), "")
                    if name:
                        props.setdefault("worksFor", name)
                continue
            prop = c.attrs.get("itemprop")
            if prop:
                val = c.attrs.get("content") or (c.attrs.get("href") if c.tag == "a" else None) or c.text(200)
                if val and prop not in props:
                    props[prop] = val
            stack.extend(c.children)
        out.append({"type": itype, "props": props, "node": n})
    return out


def card_groups(doc: Doc, min_size: int = 3, max_groups: int = 40) -> list[list[Node]]:
    """Groups of >= min_size sibling elements sharing tag and classes: the usual speaker/sponsor grid."""
    groups: list[list[Node]] = []
    for n in iter_nodes(doc.root):
        if len(n.children) < min_size:
            continue
        by_key: dict[str, list[Node]] = {}
        for c in n.children:
            if c.tag in ("div", "li", "article", "a", "section", "figure", "td"):
                by_key.setdefault(c.key, []).append(c)
        for key, members in by_key.items():
            if len(members) >= min_size:
                groups.append(members)
                if len(groups) >= max_groups:
                    return groups
    return groups
