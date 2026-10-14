"""Export the ``market_model`` items into Euboulia's ``provider-facts.v1`` file.

Euboulia (the browser extension) consumes this file to build its catalog; every figure carries the page
it was read from and the date it was fetched. Rows that were tainted or held never get here."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .adapters.provider_docs import KNOWN_PAGES, OPENAI_MODEL_PAGE, strip_snapshot

# Which page is authoritative for which field, in priority order.
FIELD_SOURCES: dict[str, dict[str, list[str]]] = {
    "anthropic": {
        "price": ["pricing"], "longContext": ["pricing"], "context": ["models-overview"], "maxOutput": ["models-overview"],
        "status": ["deprecations", "models-overview"], "retiresAt": ["deprecations", "models-overview"],
        "efforts": ["effort"], "defaultEffort": ["models-overview", "effort"], "name": ["models-overview", "pricing"],
    },
    "openai": {
        "price": ["model-pages"], "longContext": ["model-pages"], "context": ["model-pages"], "maxOutput": ["model-pages"],
        "status": ["chatgpt-models", "model-pages"], "retiresAt": ["chatgpt-models"],
        "efforts": ["model-pages"], "defaultEffort": ["model-pages"], "name": ["model-pages", "chatgpt-models"],
    },
}
_FIELD_MAP = {  # facts field -> row key(s)
    "context": "context", "maxOutput": "max_output", "status": "status", "retiresAt": "retires_at",
    "efforts": "efforts", "defaultEffort": "default_effort", "name": "name",
}


def iso_date_text(s: str) -> str:
    """'Not sooner than June 9, 2027' -> 'not before 2027-06-09'; 'October 14, 2026' -> '2026-10-14'; else unchanged."""
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", s)
    if not m:
        return s
    try:
        iso = datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return s
    return f"not before {iso}" if re.search(r"not sooner than|not before", s, re.I) else iso


def euboulia_id(provider: str, model_id: str) -> str:
    """Map a provider id to Euboulia's OpenRouter-style key: claude-opus-5-5 -> anthropic/claude-opus-5.5."""
    if provider == "openai":
        return f"openai/{model_id}"
    mid = strip_snapshot(model_id)
    parts = mid.split("-")
    words = [p for p in parts if not p.isdigit()]
    nums = [p for p in parts if p.isdigit()]
    family = "-".join(w for w in words if w != "claude")
    return f"anthropic/claude-{family}-{'.'.join(nums)}" if nums else f"anthropic/claude-{family}"


def build_provider_facts(rows: list[dict], fetched_at: dict[tuple[str, str], float], now: float) -> dict:
    """``rows``: item attrs dicts (provider, model_id, page, ...). ``fetched_at``: (provider, page) -> epoch."""
    by: dict[tuple[str, str], dict[str, dict]] = {}
    for r in rows:
        prov, page = r.get("provider"), r.get("page")
        mid = r.get("model_id")
        if not prov or not page or not mid or prov not in FIELD_SOURCES:
            continue
        by.setdefault((prov, strip_snapshot(mid)), {})[page] = r
    day = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    providers: dict = {}
    for (prov, mid), pages in sorted(by.items()):
        srcs = providers.setdefault(prov, {"sources": {}, "models": {}})["sources"]
        for page in pages:
            url = OPENAI_MODEL_PAGE.replace("{id}.md", "{id}") if page == "model-pages" else KNOWN_PAGES[prov][page].removesuffix(".md")
            ts = fetched_at.get((prov, page), now)
            srcs.setdefault(page, {"url": url, "fetched": day(ts), "format": "markdown (.md)"})
        model: dict = {"apiId": pages[next(iter(pages))].get("model_id", mid), "sources": {}}
        pri = FIELD_SOURCES[prov]
        def take(field: str, key: str):
            for page in pri.get(field, []):
                row = pages.get(page)
                if row is not None and row.get(key) is not None:
                    return page, row[key]
            return None, None
        for field, key in _FIELD_MAP.items():
            page, val = take(field, key)
            if val is not None:
                model[field] = val
                if field not in ("name",):
                    model["sources"][field] = page
        # The deprecations table says "Active" for models the overview lists as legacy; the overview is more specific.
        ov = pages.get("models-overview")
        if model.get("status") == "active" and ov and ov.get("status") == "legacy":
            model["status"] = "legacy"
            model["sources"]["status"] = "models-overview"
        if "retiresAt" in model:
            model["retiresAt"] = iso_date_text(model["retiresAt"])
        page, inp = take("price", "input_per_mtok")
        _, out = take("price", "output_per_mtok")
        if inp is not None and out is not None:
            model["price"] = {"input": inp, "output": out}
            model["sources"]["price"] = page
            lpage, thr = take("longContext", "long_context_threshold")
            _, li = take("longContext", "long_context_input")
            _, lo = take("longContext", "long_context_output")
            if thr and li is not None and lo is not None:
                model["price"]["longContext"] = {"threshold": thr, "input": li, "output": lo}
                model["sources"]["longContext"] = lpage
        if "name" not in model:
            model["name"] = mid
        if "price" not in model or "status" not in model:
            continue  # Euboulia requires a priced model with a known status; partial rows wait for the other pages
        if "efforts" in model and "defaultEffort" in model and model["defaultEffort"] not in model["efforts"]:
            del model["defaultEffort"]
        if prov == "openai" and pages.get("chatgpt-models") and pages.get("model-pages"):
            model["note"] = "retired from ChatGPT (learn.chatgpt.com); still on the API"
        providers[prov]["models"][euboulia_id(prov, mid)] = model
    return {
        "$schema": "provider-facts.v1", "schemaVersion": 1, "generatedAt": day(now),
        "generatedBy": "ninshubur provider_docs adapter",
        "providers": providers,
    }


def rows_from_db(conn: sqlite3.Connection) -> tuple[list[dict], dict[tuple[str, str], float]]:
    rows, fetched = [], {}
    cur = conn.execute("SELECT attrs, last_seen_at FROM items WHERE kind='market_model' AND tainted=0 AND held=0")
    for attrs_json, seen in cur.fetchall():
        attrs = json.loads(attrs_json)
        rows.append(attrs)
        key = (attrs.get("provider"), attrs.get("page"))
        fetched[key] = max(fetched.get(key, 0.0), float(seen or 0))
    return rows, fetched


def export_provider_facts(conn: sqlite3.Connection, home: Path, now: float, out: Path | None = None) -> Path:
    rows, fetched = rows_from_db(conn)
    facts = build_provider_facts(rows, fetched, now)
    path = out or (home / "feeds" / "provider-facts.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    return path
