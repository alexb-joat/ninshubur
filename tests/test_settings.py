"""The settings engine. Learnable keys are a short explicit list; everything else in the configuration is a guardrail key
that is shown and never set. Preview writes nothing; each refusal exits 2 with its reason."""
from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
import yaml

from ninshubur import settings as S
from ninshubur.config import DEFAULTS
from tests.conftest import NOW, write_config

ROOT = Path(__file__).resolve().parents[1]

# What may never be learnable, by prefix: anything that changes what is fetched, stored, sent or admitted.
GUARDRAIL_PREFIXES = ("llm", "sinks", "sources", "scheduler", "retention_months", "budgets.daily_bytes", "user_agent", "api", "timezone")
LEARNABLE_GROUPS = ("base_value_usd.", "costs.")
LEARNABLE_SINGLES = {"budgets.daily_human_minutes", "budgets.hourly_rate_usd", "corroboration_value_usd"}


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def cli(home, capsys, monkeypatch):
    from ninshubur.__main__ import main
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    write_config(home, sources={}, extra={"budgets": {"hourly_rate_usd": 80}, "timezone": "Europe/Lisbon"})

    def call(*args: str) -> tuple[int, str, str]:
        code = main(list(args))
        out = capsys.readouterr()
        return code, out.out, out.err
    return call


# --- the registry --------------------------------------------------------------------------
def test_every_learnable_default_sits_inside_its_own_bounds_and_is_read_from_the_defaults():
    assert S.LEARNABLE and len(S.REGISTRY) == len(S.LEARNABLE)  # no key listed twice
    for s in S.LEARNABLE:
        assert s.default is not S.MISSING, s.key
        assert s.default == S.lookup(DEFAULTS, s.path)
        assert S.check_value(s, s.default) is None, s.key
        assert s.minimum <= s.default <= s.maximum and s.minimum < s.maximum
        assert s.kind in ("usd", "minutes") and s.meaning and s.meaning == s.meaning.strip() and "\n" not in s.meaning


def test_the_split_of_the_defaults_into_learnable_and_guardrail_is_the_stated_one():
    leaves = S.flatten(DEFAULTS)
    learnable = {k for k in leaves if k in S.REGISTRY}
    guardrail = set(leaves) - learnable
    # every learnable key is a real default; nothing is learnable that the defaults do not have
    assert set(S.REGISTRY) == learnable
    # the stated list: every default under base_value_usd and costs, and the three singles; nothing else
    expected = {k for k in leaves if k.startswith(LEARNABLE_GROUPS)} | LEARNABLE_SINGLES
    assert learnable == expected
    # every guardrail prefix is present among the defaults, and none of its keys is learnable
    for prefix in GUARDRAIL_PREFIXES:
        under = {k for k in leaves if k == prefix or k.startswith(prefix + ".")}
        assert under, prefix
        assert under <= guardrail, sorted(under & learnable)
    assert {"llm.caps.daily_usd", "budgets.daily_bytes", "scheduler.per_host_rps", "user_agent", "sources"} <= guardrail
    # a guardrail key is not learnable by being set in an overlay either
    assert not any(k.startswith(GUARDRAIL_PREFIXES) for k in S.REGISTRY)


def test_no_policy_key_is_learnable():
    policy = yaml.safe_load((ROOT / "ninshubur" / "policy.yaml").read_text(encoding="utf-8"))
    keys = set(S.flatten(policy))
    assert keys, "policy.yaml should have keys"
    assert not (keys & set(S.REGISTRY))
    assert not any(k.startswith(top + ".") or k == top for k in S.REGISTRY for top in policy)  # no policy group is a registry prefix
    assert "corroboration.value_usd" in keys and "corroboration_value_usd" in S.REGISTRY  # similar names, different files


def test_check_value_types_and_bounds():
    usd = S.REGISTRY["base_value_usd.cfp_submit"]
    minutes = S.REGISTRY["costs.minutes.cfp_submit"]
    assert [S.check_value(usd, v) for v in (0, 900, 12.5, S.USD_MAX)] == [None] * 4
    assert S.check_value(minutes, 0) is None and S.check_value(minutes, 1440) is None
    for bad in (True, "900", None, [1], {"a": 1}, math.nan, math.inf, -1, S.USD_MAX + 1):
        assert S.check_value(usd, bad), bad
    for bad in (1.5, 45.0, 1441, -1, True, "20"):
        assert S.check_value(minutes, bad), bad


# --- list ---------------------------------------------------------------------------------
def test_list_shows_the_value_and_where_it_came_from_and_never_writes(cli, home):
    (home / "settings.yaml").write_text("base_value_usd:\n  cfp_submit: 900\n", encoding="utf-8")
    before = tree(home)
    code, out, err = cli("settings", "list")
    assert code == 0 and err == ""
    rows = {line.split()[0]: line.split() for line in out.splitlines()[1:]}
    assert rows["base_value_usd.cfp_submit"][1:4] == ["900", "overlay", "learnable"]
    assert rows["budgets.hourly_rate_usd"][1:4] == ["80", "config", "learnable"]
    assert rows["base_value_usd.tool_outage"][1:4] == ["50", "default", "learnable"]
    assert set(rows) == set(S.REGISTRY) and "llm.local.model" not in out
    assert tree(home) == before


def test_list_all_adds_the_guardrail_keys_read_only_and_not_the_sources(cli, home):
    code, out, _ = cli("settings", "list", "--all")
    assert code == 0
    rows = {line.split()[0]: line for line in out.splitlines()[1:]}
    assert {"llm.caps.daily_usd", "budgets.daily_bytes", "scheduler.tick_s", "user_agent", "sources", "timezone"} <= set(rows)
    for key in ("llm.caps.daily_usd", "scheduler.tick_s", "sources"):
        assert "guardrail" in rows[key] and "read-only" in rows[key]
    assert "0 configured" in rows["sources"]
    assert rows["timezone"].split()[1:3] == ["Europe/Lisbon", "config"]


def test_list_warns_about_an_overlay_entry_it_cannot_use(cli, home):
    (home / "settings.yaml").write_text("llm:\n  caps:\n    daily_usd: 99\nbase_value_usd:\n  cfp_submit: -5\n  nonsense: 1\n", encoding="utf-8")
    code, out, err = cli("settings", "list")
    assert code == 0
    assert "llm.caps.daily_usd: not a learnable key" in out and "base_value_usd.cfp_submit: -5 is outside 0 to 100000" in out
    assert "base_value_usd.nonsense: not a learnable key" in out and "99" not in out.replace("100000", "")  # the guardrail value is not echoed
    rows = {line.split()[0]: line.split() for line in out.splitlines()[1:] if line.split() and line.split()[0] in S.REGISTRY}
    assert rows["base_value_usd.cfp_submit"][1:3] == ["800", "default"]  # the bad entry changed nothing


# --- preview ------------------------------------------------------------------------------
def test_preview_shows_old_to_new_and_leaves_the_data_home_byte_identical(cli, home):
    before = tree(home)
    code, out, err = cli("settings", "preview", "base_value_usd.cfp_submit=900", "budgets.hourly_rate_usd=80", "costs.minutes.cfp_submit=90")
    assert code == 0 and err == "" and out.startswith("settings preview (nothing is written)")
    lines = {line.split()[0]: line for line in out.splitlines()[2:]}
    assert re.search(r"800 \(default\)\s+->\s+900\s+0 to 100000", lines["base_value_usd.cfp_submit"])
    assert "80 (config)" in lines["budgets.hourly_rate_usd"] and "(unchanged)" in lines["budgets.hourly_rate_usd"]
    assert re.search(r"120 \(default\)\s+->\s+90\s", lines["costs.minutes.cfp_submit"])
    assert tree(home) == before and not (home / "settings.yaml").exists() and not (home / "logs" / "settings.jsonl").exists()


def test_preview_reads_a_float_a_negative_number_and_a_quoted_number_as_what_they_are(cli, home):
    code, out, err = cli("settings", "preview", "base_value_usd.cfp_submit=812.5")
    assert code == 0 and re.search(r"->\s+812\.5\s", out)
    code, out, err = cli("settings", "preview", "base_value_usd.cfp_submit=-1")
    assert code == 2 and "-1 is outside 0 to 100000" in err
    code, out, err = cli("settings", "preview", "base_value_usd.cfp_submit='900'")
    assert code == 2 and "needs a number" in err


@pytest.mark.parametrize("spec, reason", [
    ("llm.local.model=x", "a guardrail key"), ("llm.caps.daily_usd=50", "a guardrail key"), ("scheduler.tick_s=1", "a guardrail key"),
    ("retention_months=1", "a guardrail key"), ("budgets.daily_bytes=5", "a guardrail key"), ("user_agent=x", "a guardrail key"),
    ("api.port=1", "a guardrail key"), ("sinks.ics.path=x", "a guardrail key"), ("timezone=UTC", "a guardrail key"),
    ("sources=x", "a guardrail key"),
    ("base_value_usd.cfp_submt=900", "not a key of the configuration; did you mean base_value_usd.cfp_submit?"),
    ("budgets.hourly_rate=70", "did you mean budgets.hourly_rate_usd?"),
    ("zzzzzzzz=1", "not a key of the configuration"),
    ("base_value_usd=3", "a group, not a setting; its settings: base_value_usd.cfp_submit"),
    ("costs.minutes=3", "a group, not a setting; its settings: costs.minutes.cfp_submit"),
    ("llm=3", "a group, not a setting; everything in it is a guardrail key"),
    ("budgets.hourly_rate_usd=abc", "needs a number, got 'abc'"), ("budgets.hourly_rate_usd=true", "needs a number, got True"),
    ("budgets.hourly_rate_usd=[1]", "needs a number"), ("budgets.hourly_rate_usd=.nan", "needs a number"),
    ("budgets.hourly_rate_usd=.inf", "needs a number"),
    ("costs.minutes.cfp_submit=1.5", "needs a whole number of minutes"), ("budgets.daily_human_minutes=45.0", "needs a whole number of minutes"),
    ("budgets.daily_human_minutes=1441", "outside 0 to 1440"), ("budgets.daily_human_minutes=-1", "outside 0 to 1440"),
    ("corroboration_value_usd=100001", "outside 0 to 100000"),
    ("nokey", "expected KEY=VALUE"), ("=5", "expected KEY=VALUE"),
])
def test_each_refusal_exits_2_with_its_reason(cli, home, spec, reason):
    before = tree(home)
    code, out, err = cli("settings", "preview", spec)
    assert code == 2 and f"refused: " in err and reason in err, err
    assert out.strip() == "settings preview (nothing is written)"
    assert tree(home) == before


def test_one_refusal_makes_the_whole_preview_exit_2_and_a_key_given_twice_is_refused(cli, home):
    code, out, err = cli("settings", "preview", "base_value_usd.cfp_submit=900", "llm.caps.daily_usd=50")
    assert code == 2 and "base_value_usd.cfp_submit" in out and "llm.caps.daily_usd: a guardrail key" in err
    code, out, err = cli("settings", "preview", "base_value_usd.cfp_submit=900", "base_value_usd.cfp_submit=901")
    assert code == 2 and "base_value_usd.cfp_submit: given twice" in err


def test_a_guardrail_refusal_does_not_echo_the_value(cli, home):
    code, out, err = cli("settings", "preview", "llm.cloud.base_url=https://private.example/secret-path-xyz")
    assert code == 2 and "a guardrail key" in err and "secret-path-xyz" not in out + err


def test_settings_needs_a_subcommand_and_never_opens_the_main_database(cli, home, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("settings must not open the main database")
    monkeypatch.setattr("ninshubur.__main__.connect", boom)
    assert cli("settings", "list")[0] == 0 and cli("settings", "preview", "costs.travel_abroad_usd=700")[0] == 0
    assert not (home / "ninshubur.db").exists()
    with pytest.raises(SystemExit):
        cli("settings")


# --- the doc --------------------------------------------------------------------------------
def test_the_docs_key_table_is_the_registrys():
    text = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8")
    block = text[text.index("<!-- gen:settings-keys -->") + len("<!-- gen:settings-keys -->\n"):text.index("<!-- /gen -->")]
    assert block == S.render_key_table() + "\n", "regenerate it: python -c 'from ninshubur.settings import render_key_table; print(render_key_table())'"
    for word in ("guardrail", "preview", "Two classes of key", "What `apply` will do"):
        assert word in text
