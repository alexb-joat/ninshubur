"""Replay transport: serve sample files instead of the network (``run --replay DIR``, smoke tests).

Directory layout is by adapter: ``DIR/confs_tech/*.json``, ``DIR/greenhouse/*.json``,
``DIR/hn/{*stories*,*comments*}.json``, ``DIR/statuspage/*.json``, ``DIR/event_sites/*.html`` (served for the
event hosts named in the confs sample). Any host not covered answers 599.
"""
from __future__ import annotations

from pathlib import Path

import httpx


def _first(d: Path, pattern: str) -> bytes | None:
    for p in sorted(d.glob(pattern)):
        return p.read_bytes()
    return None


def replay_transport(root: Path) -> httpx.MockTransport:
    root = Path(root)

    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if path == "/robots.txt":
            return httpx.Response(404, text="")
        body: bytes | None = None
        ct = "application/json"
        if host == "raw.githubusercontent.com":
            topic = path.rsplit("/", 1)[-1]
            body = _first(root / "confs_tech", topic) or _first(root / "confs_tech", "*.json")
            ct = "text/plain; charset=utf-8"
        elif host == "boards-api.greenhouse.io":
            body = _first(root / "greenhouse", "*.json")
        elif host == "hn.algolia.com":
            tags = dict(req.url.params).get("tags", "")
            body = _first(root / "hn", "*comments*.json") if tags.startswith("comment") else _first(root / "hn", "*stories*.json")
        elif host.startswith("status."):
            body = _first(root / "statuspage", "*.json")
        elif (root / "event_sites").is_dir() and not host.endswith((".io", ".com")) or host in ("pycon.pt", "www.saastock.com", "growthsummit.example"):
            # any event host from the confs sample: the homepage links to /speakers/, /sponsors and /agenda
            ct = "text/html; charset=utf-8"
            low = path.lower()
            name = ("cards_linkedin.html" if "speaker" in low else "microdata.html" if "sponsor" in low
                    else "wordpress_plugin.html" if "agenda" in low else "home.html")
            body = _first(root / "event_sites", name)
        if body is None:
            return httpx.Response(599, text="no replay sample for this host")
        return httpx.Response(200, content=body, headers={"Content-Type": ct})

    return httpx.MockTransport(handler)
