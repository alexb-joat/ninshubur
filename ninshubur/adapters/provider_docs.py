"""LLM provider documentation (Tier 0): the providers' own model pages, read as Markdown.

Gives Euboulia the facts behind its suggestions (prices, context windows, status, effort levels) with a
source on every figure. Only the pages listed in KNOWN_PAGES are ever fetched; adding one is a reviewed
code change. Links found in the pages are never followed. Each page is parsed into rows that go through
the ``provider_model.v1`` schema like any other source, so undeclared fields are dropped and tainted
rows are quarantined. The ChatGPT help centre blocks automated readers and is deliberately absent."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..config import SourceConfig
from ..items import DateRef, Item, apply_taint, item_uid
from ..schemas import Projection, get_schema, project
from .base import AdapterSpec, FetchPlan, Fetched, ParseResult, register

KNOWN_PAGES: dict[str, dict[str, str]] = {
    "anthropic": {
        "pricing": "https://platform.claude.com/docs/en/about-claude/pricing.md",
        "models-overview": "https://platform.claude.com/docs/en/about-claude/models/overview.md",
        "effort": "https://platform.claude.com/docs/en/build-with-claude/effort.md",
        "deprecations": "https://platform.claude.com/docs/en/about-claude/model-deprecations.md",
    },
    "openai": {
        "chatgpt-models": "https://learn.chatgpt.com/docs/models.md",
    },
}
OPENAI_MODEL_PAGE = "https://developers.openai.com/docs/models/{id}.md"
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,60}$")
HOSTS = frozenset({"platform.claude.com", "developers.openai.com", "learn.chatgpt.com"})

SCHEMA = "provider_model.v1"


@register
class ProviderDocs:
    spec = AdapterSpec(
        name="provider_docs", kind="tooling", hosts=HOSTS, schema=SCHEMA, schemas=(SCHEMA,),
        expected_content_type="text/markdown|text/plain", base_interval_s=86_400, max_interval_s=7 * 86_400,
        default_tier=0, legal_basis="public_business_info", min_delay_s=2.0,
        questions=("What does each LLM provider currently charge and offer, so Euboulia suggests models from facts, not guesses?",
                   "Which models were deprecated, retired or repriced since we last looked?"),
    )

    def plan(self, src: SourceConfig, state: dict, now: float) -> list[FetchPlan]:
        provider = str(src.params.get("provider", "")).lower()
        pages = KNOWN_PAGES.get(provider)
        if not pages:
            return []
        wanted = src.params.get("pages") or list(pages)
        plans = [FetchPlan(url=pages[k], key=f"provider_docs:{provider}:{k}", purpose=f"{provider} {k}", max_bytes=600_000)
                 for k in wanted if k in pages]
        if provider == "openai":
            for mid in src.params.get("models") or []:
                mid = str(mid).lower()
                if MODEL_ID_RE.match(mid):
                    plans.append(FetchPlan(url=OPENAI_MODEL_PAGE.format(id=mid), key=f"provider_docs:openai:model:{mid}",
                                           purpose=f"openai model page {mid}", max_bytes=200_000))
        return plans

    def parse(self, src: SourceConfig, fetched: Fetched, now: float) -> ParseResult:
        provider = str(src.params.get("provider", "")).lower()
        key = fetched.plan.key.split(":", 2)[-1]  # "<page>" or "model:<id>"
        try:
            md = fetched.text()
        except UnicodeDecodeError as e:
            from ..detectors import Finding
            p = Projection()
            p.findings.append(Finding("high", "not_text", str(e)[:80]))
            return ParseResult(projection=p)
        page = "model-pages" if key.startswith("model:") else key
        rows = parse_page(provider, page, md)
        for r in rows:
            r["provider"] = provider
            r["page"] = page
        proj = project(rows, get_schema(SCHEMA), now, allowlisted_hosts=tuple(sorted(HOSTS)))
        res = ParseResult(projection=proj)
        if not rows:
            res.notes.append(f"{provider}/{page}: no rows recognised; page structure may have changed")
        for row in proj.rows:
            dates = []
            ts = _plain_date(row.get("retires_at"))
            if ts:
                dates.append(DateRef("retirement", ts))
            item = Item(
                uid=item_uid(src.name, f"{provider}:{row['model_id']}:{page}"), source=src.name, kind="market_model",
                title=row.get("name") or row["model_id"], url=fetched.plan.url.removesuffix(".md"), text="",
                published_at=None, dates=dates,
                attrs={k: v for k, v in row.items() if not k.startswith("_")},
                legal_basis=src.legal_basis,
            )
            apply_taint(item, row)
            res.items.append(item)
        return res


# ---------------------------------------------------------------------------------------------
# Parsers: pure functions over the Markdown text. Each returns partial rows keyed by model_id;
# the exporter merges pages. Anything not matched is simply absent (never guessed).
# ---------------------------------------------------------------------------------------------
def parse_page(provider: str, page: str, md: str) -> list[dict]:
    fn = {
        ("anthropic", "pricing"): parse_anthropic_pricing,
        ("anthropic", "models-overview"): parse_anthropic_overview,
        ("anthropic", "effort"): parse_anthropic_effort,
        ("anthropic", "deprecations"): parse_anthropic_deprecations,
        ("openai", "model-pages"): parse_openai_model_page,
        ("openai", "chatgpt-models"): parse_chatgpt_models,
    }.get((provider, page))
    return fn(md) if fn else []


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _money(cell: str) -> float | None:
    m = re.search(r"\$([\d][\d,]*(?:\.\d+)?)", cell)
    return float(m.group(1).replace(",", "")) if m else None


def _tokens(cell: str) -> int | None:
    m = re.search(r"([\d][\d,]*(?:\.\d+)?)\s*([KkMm])?\b", cell)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").upper()
    return int(n * (1_000_000 if unit == "M" else 1_000 if unit == "K" else 1))


def claude_slug(name: str) -> str:
    """'Claude Opus 5.5' -> 'claude-opus-5-5' (the Claude API id form, without a snapshot date)."""
    name = re.sub(r"\(.*?\)", "", name).replace("[", "").replace("]", "").strip()
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def strip_snapshot(model_id: str) -> str:
    return re.sub(r"-\d{8}$", "", model_id)


def parse_anthropic_pricing(md: str) -> list[dict]:
    rows: dict[str, dict] = {}
    in_table = False
    for line in md.splitlines():
        if line.startswith("| Model") and "Base input" in line:
            in_table = True
            continue
        if in_table and not line.startswith("|"):
            if rows:
                break
            continue
        if not in_table or line.startswith("| :") or line.startswith("| -"):
            continue
        cells = _cells(line)
        if len(cells) < 6 or not cells[0].startswith("Claude"):
            continue
        name_cell = cells[0]
        name_core = re.sub(r"\s*\(.*$", "", re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", name_cell)).strip()
        mid = claude_slug(name_core)
        prices = [_money(c) for c in cells[1:]]
        inp, out = prices[0], prices[-1]
        if inp is None or out is None:
            continue
        tier = re.search(r"\((?:for prompts )?(up to|over) ([\d,]+) tokens\)", name_cell)
        row = rows.setdefault(mid, {"model_id": mid, "name": name_core})
        if tier and tier.group(1) == "over":
            row["long_context_threshold"] = int(tier.group(2).replace(",", ""))
            row["long_context_input"] = inp
            row["long_context_output"] = out
        else:
            row["input_per_mtok"] = inp
            row["output_per_mtok"] = out
    return [r for r in rows.values() if "input_per_mtok" in r]


def parse_anthropic_overview(md: str) -> list[dict]:
    header: list[str] | None = None
    table: dict[str, list[str]] = {}
    for line in md.splitlines():
        if line.startswith("| Feature"):
            header = _cells(line)[1:]
            continue
        if header is None:
            continue
        if not line.startswith("|"):
            if table:
                break
            continue
        cells = _cells(line)
        label = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[0]).strip()
        table[label] = cells[1:]
    if not header or "Claude API ID" not in table:
        return []
    out = []
    # "Legacy models (still available): [Claude Fable 5](...), [Claude Opus 5](...), ..." names the legacy set.
    leg = re.search(r"Legacy models \(still available\):(.+)", md)
    for name in re.findall(r"\[([^\]]+)\]\([^)]*\)", leg.group(1)) if leg else []:
        out.append({"model_id": claude_slug(name), "name": name, "status": "legacy"})
    for i, name in enumerate(header):
        api = table["Claude API ID"][i].strip("` ") if i < len(table["Claude API ID"]) else ""
        if not api:
            continue
        row: dict = {"model_id": api, "name": name, "status": "active"}
        if "Context window" in table:
            row["context"] = _tokens(table["Context window"][i])
        if "Max output" in table:
            row["max_output"] = _tokens(table["Max output"][i])
        if "Default effort" in table:
            row["default_effort"] = table["Default effort"][i].strip("` ")
        if "Retirement" in table:
            row["retires_at"] = table["Retirement"][i]
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def parse_anthropic_deprecations(md: str) -> list[dict]:
    out = []
    in_table = False
    for line in md.splitlines():
        if line.startswith("| API model name"):
            in_table = True
            continue
        if in_table and not line.startswith("|"):
            if out:
                break
            continue
        if not in_table or line.startswith("| :") or line.startswith("| -"):
            continue
        cells = _cells(line)
        if len(cells) < 4 or not cells[0].startswith("claude"):
            continue
        row = {"model_id": cells[0], "status": cells[1].lower(), "retires_at": cells[3]}
        if cells[2] and cells[2] != "N/A":
            row["deprecated_at"] = cells[2]
        out.append(row)
    return out


def parse_anthropic_effort(md: str) -> list[dict]:
    m = re.search(r"supportedModels:\n((?:\s+- .+\n)+)", md)
    supported = [strip_snapshot(x.strip()) for x in re.findall(r"^\s+- (\S+)$", m.group(1), re.M)] if m else []
    if not supported:
        return []
    levels: dict[str, set[str] | None] = {"low": None, "medium": None, "high": None, "xhigh": None, "max": None}
    for line in md.splitlines():
        mm = re.match(r"^\| `(max|xhigh)`\s*\|(.*)", line)
        if mm:
            avail = re.search(r"Available on (.+?)\.\s*(?:\||$)", mm.group(2))   # the sentence ends at the cell, not at "5.5"
            if avail:
                names = re.split(r",\s*(?:and\s+)?|\s+and\s+", avail.group(1))
                levels[mm.group(1)] = {claude_slug(n) for n in names if n.strip()}
    # Per-model sections state the API default: "`medium` is the default", "defaults to `high`", "The API default is `high`",
    # "Start with `high`, the default".
    defaults: dict[str, str] = {}
    for sec in re.split(r"\n### ", md)[1:]:
        head, _, body = sec.partition("\n")
        who = re.search(r"Recommended effort levels for (Claude .+?)\s*$", head.strip())
        if not who:
            continue
        d = re.search(r"`(low|medium|high|xhigh|max)`(?:,)? (?:is )?the default|defaults to `(low|medium|high|xhigh|max)`"
                      r"|default is `(low|medium|high|xhigh|max)`|Start with `(low|medium|high|xhigh|max)`, the default", body)
        if d:
            defaults[claude_slug(who.group(1))] = next(g for g in d.groups() if g)
    # The levels table states the generic default: "The default on every model that supports effort except X and Y."
    generic: str | None = None
    excepted: set[str] = set()
    for line in md.splitlines():
        mm = re.match(r"^\| `(low|medium|high|xhigh|max)`\s*\|(.*)", line)
        if mm and "The default on every model that supports effort" in mm.group(2):
            generic = mm.group(1)
            ex = re.search(r"except (.+?)\.", mm.group(2))
            if ex:
                excepted = {claude_slug(n) for n in re.split(r",\s*(?:and\s+)?|\s+and\s+", ex.group(1)) if n.strip()}
    out = []
    for mid in supported:
        efforts = [lvl for lvl, allowed in levels.items() if allowed is None or mid in allowed]
        row = {"model_id": mid, "efforts": efforts}
        d = defaults.get(mid) or (generic if generic and mid not in excepted else None)
        if d in efforts:
            row["default_effort"] = d
        out.append(row)
    return out


def parse_openai_model_page(md: str) -> list[dict]:
    mid = re.search(r"Model ID: `([^`]+)`", md)
    if not mid:
        return []
    row: dict = {"model_id": mid.group(1), "status": "active"}
    title = re.search(r"^# (.+)$", md, re.M)
    if title:
        row["name"] = title.group(1).strip()
    flat = re.sub(r"\s+", " ", md)
    eff = re.search(r"`reasoning\.effort` supports ((?:`[a-z]+`(?: \(default\))?,? ?(?:and )?)+)", flat)
    if eff:
        row["efforts"] = re.findall(r"`([a-z]+)`", eff.group(1))
        d = re.search(r"`([a-z]+)` \(default\)", eff.group(1))
        if d:
            row["default_effort"] = d.group(1)
    ctx = re.search(r"([\d,]+) context window", flat)
    if ctx:
        row["context"] = int(ctx.group(1).replace(",", ""))
    mo = re.search(r"([\d,]+) max output tokens", flat)
    if mo:
        row["max_output"] = int(mo.group(1).replace(",", ""))
    inp = re.search(r"\| Input \| \$([\d.]+) \| 1M tokens", flat)
    out = re.search(r"\| Output \| \$([\d.]+) \| 1M tokens", flat)
    if inp and out:
        row["input_per_mtok"] = float(inp.group(1))
        row["output_per_mtok"] = float(out.group(1))
        lc = re.search(r"(?:more than|>)\s*([\d,]+)K input tokens are priced at ([\d.]+)x input(?: and cache rates)? and ([\d.]+)x output", flat)
        if lc:
            row["long_context_threshold"] = int(lc.group(1).replace(",", "")) * 1000
            row["long_context_input"] = round(row["input_per_mtok"] * float(lc.group(2)), 4)
            row["long_context_output"] = round(row["output_per_mtok"] * float(lc.group(3)), 4)
    return [row]


def parse_chatgpt_models(md: str) -> list[dict]:
    flat = re.sub(r"\s+", " ", md)
    out = []
    for when, name in re.findall(r"On ([A-Z][a-z]+ \d{1,2}, \d{4}), (GPT-[\w.]+(?: [A-Z][a-z]+)?) will retire from ChatGPT", flat):
        mid = re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-")
        out.append({"model_id": mid, "name": name, "status": "retired", "retires_at": when})
    return out


def _plain_date(s: str | None) -> float | None:
    if not s or "not sooner" in s.lower() or "to be announced" in s.lower():
        return None
    for fmt in ("%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None
