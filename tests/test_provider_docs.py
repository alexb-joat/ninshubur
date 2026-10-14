from __future__ import annotations

import json

from ninshubur.adapters.base import FetchPlan, Fetched, get_adapter
from ninshubur.adapters.provider_docs import (claude_slug, parse_anthropic_deprecations, parse_anthropic_effort,
                                             parse_anthropic_overview, parse_anthropic_pricing, parse_chatgpt_models,
                                             parse_openai_model_page)
from ninshubur.config import SourceConfig
from ninshubur.export_facts import build_provider_facts, euboulia_id
from tests.conftest import NOW, load_sample

S = lambda name: load_sample(f"provider_docs/{name}").decode("utf-8")


def fetched(plan: FetchPlan, body: bytes) -> Fetched:
    return Fetched(plan=plan, status=200, body=body, headers={"content-type": "text/markdown"}, changed=True, fetched_at=NOW)


def test_anthropic_pricing_table_including_haiku_tiers():
    rows = {r["model_id"]: r for r in parse_anthropic_pricing(S("anthropic-pricing.md"))}
    assert rows["claude-opus-5-5"]["input_per_mtok"] == 4 and rows["claude-opus-5-5"]["output_per_mtok"] == 20
    assert rows["claude-fable-5-1"]["input_per_mtok"] == 10 and rows["claude-sonnet-4-6"]["output_per_mtok"] == 15
    h = rows["claude-haiku-5-5"]
    assert (h["input_per_mtok"], h["output_per_mtok"]) == (0.1, 0.5)
    assert (h["long_context_threshold"], h["long_context_input"], h["long_context_output"]) == (100000, 0.5, 2.5)
    assert rows["claude-mythos-5-1"]["name"] == "Claude Mythos 5.1"  # link markup stripped
    assert rows["claude-opus-4-1"]["input_per_mtok"] == 15  # retired note in the name does not break the row


def test_anthropic_overview_columns():
    rows = {r["model_id"]: r for r in parse_anthropic_overview(S("anthropic-models-overview.md"))}
    assert {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"} <= set(rows)
    assert {k for k, v in rows.items() if v["status"] == "active"} == {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
    f = rows["claude-fable-5-1"]
    assert f["context"] == 1_000_000 and f["max_output"] == 128_000 and f["default_effort"] == "high" and f["status"] == "active"
    assert f["retires_at"].startswith("Not sooner than September 1, 2027")
    assert rows["claude-opus-5-5"]["default_effort"] == "medium"
    assert rows["claude-opus-4-8"] == {"model_id": "claude-opus-4-8", "name": "Claude Opus 4.8", "status": "legacy"}
    assert rows["claude-haiku-4-5"]["status"] == "legacy"


def test_anthropic_deprecations_status_table():
    rows = {r["model_id"]: r for r in parse_anthropic_deprecations(S("anthropic-model-deprecations.md"))}
    assert rows["claude-opus-5-5"]["status"] == "active" and "deprecated_at" not in rows["claude-opus-5-5"]
    d = rows["claude-sonnet-4-5-20250929"]
    assert d["status"] == "deprecated" and d["deprecated_at"] == "September 30, 2026" and d["retires_at"] == "November 30, 2026"
    assert rows["claude-opus-4-20250514"]["status"] == "retired"


def test_anthropic_effort_levels_per_model():
    parsed = {r["model_id"]: r for r in parse_anthropic_effort(S("anthropic-effort.md"))}
    rows = {k: v["efforts"] for k, v in parsed.items()}
    assert {k: v.get("default_effort") for k, v in parsed.items() if k in ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-4-7", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5-5")} == {
        "claude-fable-5-1": "high", "claude-opus-5-5": "medium", "claude-opus-4-7": "high", "claude-sonnet-5": "high", "claude-sonnet-4-6": "high", "claude-haiku-5-5": "medium"}
    assert parsed["claude-opus-4-6"]["default_effort"] == "high" and parsed["claude-opus-4-5"]["default_effort"] == "high"   # generic rule, no own section
    assert rows["claude-opus-5-5"] == ["low", "medium", "high", "xhigh", "max"]
    assert rows["claude-opus-4-6"] == ["low", "medium", "high", "max"]        # no xhigh on 4.6
    assert rows["claude-sonnet-4-6"] == ["low", "medium", "high", "max"]
    assert rows["claude-opus-4-5"] == ["low", "medium", "high"]               # neither xhigh nor max
    assert "claude-haiku-4-5" not in rows                                     # not a supported model


def test_openai_model_page():
    (a,) = parse_openai_model_page(S("openai-model-gpt-6-astra.md"))
    assert a["model_id"] == "gpt-6-astra" and a["name"] == "GPT-6 Astra" and a["status"] == "active"
    assert a["efforts"] == ["low", "medium", "high", "xhigh", "max"] and "default_effort" not in a
    assert (a["input_per_mtok"], a["output_per_mtok"]) == (10, 50)
    assert (a["long_context_threshold"], a["long_context_input"], a["long_context_output"]) == (272000, 20, 75)
    assert a["context"] == 1_050_000 and a["max_output"] == 128_000
    (s,) = parse_openai_model_page(S("openai-model-gpt-6.1-sol.md"))
    assert s["default_effort"] == "medium" and s["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert s["long_context_input"] == 4 and s["long_context_output"] == 15


def test_chatgpt_retirement_notice():
    (r,) = parse_chatgpt_models(S("chatgpt-models.md"))
    assert r == {"model_id": "gpt-5.5", "name": "GPT-5.5", "status": "retired", "retires_at": "October 14, 2026"}


def test_slugs_and_euboulia_ids():
    assert claude_slug("Claude Opus 5.5") == "claude-opus-5-5"
    assert claude_slug("Claude Mythos 5.1 ([limited availability](https://x))") == "claude-mythos-5-1"
    assert euboulia_id("anthropic", "claude-sonnet-4-5-20250929") == "anthropic/claude-sonnet-4.5"
    assert euboulia_id("anthropic", "claude-haiku-5-5") == "anthropic/claude-haiku-5.5"
    assert euboulia_id("openai", "gpt-6.1-sol") == "openai/gpt-6.1-sol"


def test_adapter_plans_only_known_pages():
    a = get_adapter("provider_docs")
    anth = a.plan(SourceConfig(name="p", adapter="provider_docs", params={"provider": "anthropic"}), {}, NOW)
    assert [p.key for p in anth] == ["provider_docs:anthropic:pricing", "provider_docs:anthropic:models-overview",
                                    "provider_docs:anthropic:effort", "provider_docs:anthropic:deprecations"]
    assert all(p.url.startswith("https://platform.claude.com/docs/en/") and p.url.endswith(".md") for p in anth)
    oai = a.plan(SourceConfig(name="p", adapter="provider_docs", params={"provider": "openai", "models": ["gpt-6-astra", "Bad Id!", "../x"]}), {}, NOW)
    assert [p.url for p in oai] == ["https://learn.chatgpt.com/docs/models.md", "https://developers.openai.com/docs/models/gpt-6-astra.md"]
    assert a.plan(SourceConfig(name="p", adapter="provider_docs", params={"provider": "someone"}), {}, NOW) == []


def test_adapter_parse_projects_rows_into_items():
    a = get_adapter("provider_docs")
    src = SourceConfig(name="anth", adapter="provider_docs", params={"provider": "anthropic"}, trust={"legal_basis": "public_business_info"})
    plan = a.plan(src, {}, NOW)[0]
    res = a.parse(src, fetched(plan, load_sample("provider_docs/anthropic-pricing.md")), NOW)
    assert res.projection is not None and res.projection.violations == 0 and res.notes == []
    opus = next(i for i in res.items if i.attrs["model_id"] == "claude-opus-5-5")
    assert opus.kind == "market_model" and opus.url == "https://platform.claude.com/docs/en/about-claude/pricing"
    assert set(opus.attrs) == {"provider", "model_id", "page", "name", "input_per_mtok", "output_per_mtok"}
    assert opus.attrs["page"] == "pricing" and not opus.tainted
    # a page whose structure changed produces no rows and says so
    res2 = a.parse(src, fetched(plan, b"# Pricing\n\nEverything moved.\n"), NOW)
    assert res2.items == [] and res2.notes and "no rows recognised" in res2.notes[0]
    # retirement dates become date refs only when they are plain dates
    dep = a.plan(src, {}, NOW)[3]
    res3 = a.parse(src, fetched(dep, load_sample("provider_docs/anthropic-model-deprecations.md")), NOW)
    s45 = next(i for i in res3.items if i.attrs["model_id"] == "claude-sonnet-4-5-20250929")
    assert [d.kind for d in s45.dates] == ["retirement"]
    o55 = next(i for i in res3.items if i.attrs["model_id"] == "claude-opus-5-5")
    assert o55.dates == []


def test_export_merges_pages_into_provider_facts():
    rows: list[dict] = []
    for page, name in [("pricing", "anthropic-pricing.md"), ("models-overview", "anthropic-models-overview.md"),
                       ("effort", "anthropic-effort.md"), ("deprecations", "anthropic-model-deprecations.md")]:
        for r in __import__("ninshubur.adapters.provider_docs", fromlist=["parse_page"]).parse_page("anthropic", page, S(name)):
            rows.append({**r, "provider": "anthropic", "page": page})
    for name in ["openai-model-gpt-6-astra.md", "openai-model-gpt-6.1-sol.md"]:
        for r in parse_openai_model_page(S(name)):
            rows.append({**r, "provider": "openai", "page": "model-pages"})
    for r in parse_chatgpt_models(S("chatgpt-models.md")):
        rows.append({**r, "provider": "openai", "page": "chatgpt-models"})
    facts = build_provider_facts(rows, {("anthropic", "pricing"): NOW}, NOW)
    assert facts["$schema"] == "provider-facts.v1" and facts["generatedAt"] == "2026-10-12"
    a = facts["providers"]["anthropic"]
    opus = a["models"]["anthropic/claude-opus-5.5"]
    assert opus["price"] == {"input": 4, "output": 20} and opus["status"] == "active" and opus["context"] == 1_000_000
    assert opus["efforts"] == ["low", "medium", "high", "xhigh", "max"] and opus["defaultEffort"] == "medium"
    assert opus["sources"] == {"price": "pricing", "context": "models-overview", "maxOutput": "models-overview", "status": "deprecations",
                               "retiresAt": "deprecations", "efforts": "effort", "defaultEffort": "models-overview"}
    assert a["sources"]["pricing"] == {"url": "https://platform.claude.com/docs/en/about-claude/pricing", "fetched": "2026-10-12", "format": "markdown (.md)"}
    haiku = a["models"]["anthropic/claude-haiku-5.5"]
    assert haiku["price"]["longContext"] == {"threshold": 100000, "input": 0.5, "output": 2.5}
    s45 = a["models"]["anthropic/claude-sonnet-4.5"]
    assert s45["status"] == "deprecated" and s45["retiresAt"] == "2026-11-30" and "efforts" not in s45
    o48 = a["models"]["anthropic/claude-opus-4.8"]
    assert o48["status"] == "legacy" and o48["sources"]["status"] == "models-overview" and o48["retiresAt"] == "not before 2027-05-28"
    assert o48["defaultEffort"] == "high" and o48["sources"]["defaultEffort"] == "effort"
    assert opus["retiresAt"] == "not before 2027-09-22"
    o = facts["providers"]["openai"]["models"]
    assert o["openai/gpt-6-astra"]["price"]["longContext"]["output"] == 75 and o["openai/gpt-6-astra"]["status"] == "active"
    assert o["openai/gpt-6.1-sol"]["defaultEffort"] == "medium"
    assert "openai/gpt-5.5" not in o  # only a retirement notice, no price page: waits for the model page
    json.dumps(facts)  # serialisable


def test_admission_probe_leaves_no_fetch_state(home, now):
    """`sources test` fetches once to check H2-H6; that probe must not make the first real run see the page as unchanged."""
    import types
    from ninshubur.__main__ import _test_source
    from ninshubur.config import SourceConfig
    from tests.conftest import Env, write_config, write_profiles
    write_config(home)
    write_profiles(home)
    body = load_sample("provider_docs/anthropic-pricing.md")
    env = Env(home, {("platform.claude.com", "*"): lambda req: __import__("httpx").Response(200, content=body, headers={"Content-Type": "text/markdown", "ETag": '"v1"'})})
    src = SourceConfig(name="pd", adapter="provider_docs", params={"provider": "anthropic", "pages": ["pricing"]},
                       trust={"tier": 0, "legal_basis": "public_business_info", "schema": "provider_model.v1", "reviewed_by": "alex",
                              "reviewed_at": "2026-10-12", "review_every_days": 90, "owner": "Anthropic", "owner_url": "https://platform.claude.com/docs",
                              "terms_url": "https://www.anthropic.com/legal/consumer-terms", "licence": "public docs", "rate_limit": "1/day",
                              "expected_content_type": "text/markdown|text/plain"})
    app = types.SimpleNamespace(fetcher=env.fetcher, policy=env.policy, conn=env.conn, now=env.clock.now)
    checks, test = _test_source(app, src)
    assert test["status"] == 200 and test["rows"] > 0
    assert env.conn.execute("SELECT COUNT(*) FROM fetch_state WHERE key LIKE 'provider_docs:%'").fetchone()[0] == 0
