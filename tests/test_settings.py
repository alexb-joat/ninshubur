"""The settings engine. Learnable keys are a short explicit list; everything else in the configuration is a guardrail key
that is shown and never set. Preview writes nothing; each refusal exits 2 with its reason. Apply writes the overlay
settings.yaml and one audit line, undo takes it back, and the overlay is read back only for learnable, valid entries."""
from __future__ import annotations

import json
import math
import os
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
    assert code == 2 and "refused: " in err and reason in err, err
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
    for word in ("guardrail", "preview", "Two classes of key", "What `apply` does", "next process start"):
        assert word in text


# --- apply, undo, log -----------------------------------------------------------------------------
posix = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")


def audit(home: Path) -> list[dict]:
    p = home / "logs" / "settings.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def overlay(home: Path) -> dict:
    return yaml.safe_load((home / "settings.yaml").read_text(encoding="utf-8"))


def rows_of(out: str) -> dict[str, list[str]]:
    return {line.split()[0]: line.split() for line in out.splitlines()[1:] if line.split()}


def test_apply_without_yes_prints_the_preview_exits_2_and_writes_nothing(cli, home):
    before = tree(home)
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900")
    assert code == 2 and out.startswith("settings preview (nothing is written)")
    assert re.search(r"800 \(default\)\s+->\s+900", out) and "add --yes" in err
    assert tree(home) == before and not (home / "settings.yaml").exists() and not (home / "logs" / "settings.jsonl").exists()


@posix
def test_apply_writes_the_overlay_and_one_audit_line_and_list_shows_the_overlay(cli, home, monkeypatch):
    monkeypatch.setenv("NINSHUBUR_ROUTER_KEY", "hunter2-not-for-logs")
    config_before = (home / "config.yaml").read_bytes()
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900", "budgets.hourly_rate_usd=70", "--note", "ci run ranked low", "--yes")
    assert code == 0 and err == "" and "applied as #1" in out and "next process start" in out
    assert overlay(home) == {"base_value_usd": {"cfp_submit": 900}, "budgets": {"hourly_rate_usd": 70}}
    assert (home / "config.yaml").read_bytes() == config_before                       # config.yaml is never rewritten
    assert (home / "settings.yaml").stat().st_mode & 0o777 == 0o600 and (home / "logs" / "settings.jsonl").stat().st_mode & 0o777 == 0o600
    assert not list(home.glob("*.tmp"))
    [line] = audit(home)
    assert set(line) == {"id", "ts", "event", "note", "keys"} and line["id"] == 1 and line["event"] == "apply" and line["note"] == "ci run ranked low"
    by_key = {e["key"]: e for e in line["keys"]}
    assert by_key["base_value_usd.cfp_submit"] == {"key": "base_value_usd.cfp_submit", "old": 800, "old_source": "default", "new": 900, "overlay_before": None}
    assert by_key["budgets.hourly_rate_usd"]["old"] == 80 and by_key["budgets.hourly_rate_usd"]["old_source"] == "config"
    assert "hunter2" not in (home / "logs" / "settings.jsonl").read_text() + (home / "settings.yaml").read_text()   # no environment values
    rows = rows_of(cli("settings", "list")[1])
    assert rows["base_value_usd.cfp_submit"][1:4] == ["900", "overlay", "learnable"] and rows["budgets.hourly_rate_usd"][1:3] == ["70", "overlay"]
    assert rows["base_value_usd.tool_outage"][1:3] == ["50", "default"]


def test_apply_refuses_what_preview_refuses_and_writes_nothing(cli, home):
    before = tree(home)
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900", "llm.caps.daily_usd=50", "--yes")
    assert code == 2 and "llm.caps.daily_usd: a guardrail key" in err
    assert tree(home) == before and not (home / "settings.yaml").exists() and not (home / "logs" / "settings.jsonl").exists()


def test_apply_with_the_value_already_in_force_changes_and_logs_nothing(cli, home):
    before = tree(home)
    code, out, err = cli("settings", "apply", "budgets.hourly_rate_usd=80", "--yes")        # config.yaml already says 80
    assert code == 0 and "nothing to change" in out and tree(home) == before
    assert cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")[0] == 0
    bytes_after_first = (home / "settings.yaml").read_bytes()
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900", "budgets.hourly_rate_usd=80", "--yes")
    assert code == 0 and "nothing to change" in out and "(unchanged)" in out
    assert (home / "settings.yaml").read_bytes() == bytes_after_first and len(audit(home)) == 1


@pytest.mark.parametrize("note, reason", [("x" * 201, "limit is 200"), ("two\nlines", "control character"), ("bell\x07", "control character")])
def test_apply_refuses_a_note_that_is_long_or_not_one_plain_line(cli, home, note, reason):
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900", "--note", note, "--yes")
    assert code == 2 and reason in err and not (home / "settings.yaml").exists() and not audit(home)


def test_undo_restores_the_absent_overlay_and_logs_it(cli, home):
    before = tree(home)
    assert cli("settings", "apply", "base_value_usd.cfp_submit=900", "budgets.hourly_rate_usd=70", "--yes")[0] == 0
    code, out, err = cli("settings", "undo")
    assert code == 0 and err == "" and "undid apply #1 as #2" in out
    assert not (home / "settings.yaml").exists()                                          # the overlay is absent again
    assert {k: v for k, v in tree(home).items() if k != "logs/settings.jsonl"} == {k: v for k, v in before.items() if k != "logs/settings.jsonl"}
    first, second = audit(home)
    assert second["event"] == "undo" and second["undoes"] == 1 and second["id"] == 2
    moved = {e["key"]: (e["old"], e["new"]) for e in second["keys"]}
    assert moved == {"base_value_usd.cfp_submit": (900, 800), "budgets.hourly_rate_usd": (70, 80)}
    assert rows_of(cli("settings", "list")[1])["base_value_usd.cfp_submit"][1:3] == ["800", "default"]
    code, out, err = cli("settings", "undo")                                              # the last apply is already undone
    assert code == 2 and "already undone by #2" in err and len(audit(home)) == 2


def test_undo_puts_back_the_overlay_bytes_of_the_apply_before(cli, home):
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    after_first = (home / "settings.yaml").read_bytes()
    cli("settings", "apply", "base_value_usd.cfp_submit=950", "budgets.hourly_rate_usd=70", "--yes")
    assert (home / "settings.yaml").read_bytes() != after_first
    assert cli("settings", "undo")[0] == 0
    assert (home / "settings.yaml").read_bytes() == after_first
    assert rows_of(cli("settings", "list")[1])["base_value_usd.cfp_submit"][1:3] == ["900", "overlay"]


def test_apply_twice_then_undo_the_first_by_id_undoes_the_right_one(cli, home):
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    cli("settings", "apply", "budgets.hourly_rate_usd=70", "--yes")
    code, out, err = cli("settings", "undo", "1")
    assert code == 0 and "undid apply #1 as #3" in out
    assert overlay(home) == {"budgets": {"hourly_rate_usd": 70}}                           # the second apply stays
    assert [(r["id"], r["event"]) for r in audit(home)] == [(1, "apply"), (2, "apply"), (3, "undo")]
    code, out, err = cli("settings", "undo", "1")
    assert code == 2 and "already undone by #3" in err and "cannot be undone twice" in err
    assert cli("settings", "undo", "2")[0] == 0 and not (home / "settings.yaml").exists()


def test_undo_is_refused_while_a_key_has_changed_again_since(cli, home):
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    cli("settings", "apply", "base_value_usd.cfp_submit=950", "--yes")
    bytes_now = (home / "settings.yaml").read_bytes()
    code, out, err = cli("settings", "undo", "1")
    assert code == 2 and "base_value_usd.cfp_submit was changed again since apply #1 (it is 950 now, not 900)" in err
    assert (home / "settings.yaml").read_bytes() == bytes_now and len(audit(home)) == 2
    assert cli("settings", "undo")[0] == 0 and cli("settings", "undo", "1")[0] == 0         # the later one first, then it works
    assert not (home / "settings.yaml").exists()
    (home / "settings.yaml").write_text("base_value_usd:\n  cfp_submit: 1\n", encoding="utf-8")   # a hand edit counts as a change too
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    (home / "settings.yaml").write_text("base_value_usd:\n  cfp_submit: 777\n", encoding="utf-8")
    assert cli("settings", "undo")[0] == 2


def test_undo_with_nothing_logged_or_an_unknown_id_is_refused(cli, home):
    code, out, err = cli("settings", "undo")
    assert code == 2 and "no apply is logged" in err
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    code, out, err = cli("settings", "undo", "9")
    assert code == 2 and "no apply with id 9" in err and (home / "settings.yaml").exists()


def test_undo_does_not_trust_a_damaged_audit_line(cli, home):
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    path = home / "logs" / "settings.jsonl"
    rec = json.loads(path.read_text())
    rec["keys"][0]["key"] = "llm.caps.daily_usd"                                           # a guardrail key in the audit line
    path.write_text(json.dumps(rec) + "\n")
    bytes_now = (home / "settings.yaml").read_bytes()
    code, out, err = cli("settings", "undo")
    assert code == 2 and "cannot be undone" in err and "not learnable" in err
    assert (home / "settings.yaml").read_bytes() == bytes_now and len(path.read_text().splitlines()) == 1
    rec["keys"][0].update(key="base_value_usd.cfp_submit", new=-4)
    path.write_text(json.dumps(rec) + "\n")
    assert cli("settings", "undo")[0] == 2


TAMPERED = "llm:\n  caps:\n    daily_usd: 99\nbase_value_usd:\n  cfp_submit: 900\n  tool_outage: -5\n  nonsense: 1\n"


def test_a_tampered_overlay_is_ignored_with_a_warning_and_never_applied(home, capsys):
    from ninshubur.config import load_config
    write_config(home, sources={}, extra={"llm": {"caps": {"daily_usd": 0.25}}})
    (home / "settings.yaml").write_text(TAMPERED, encoding="utf-8")
    cfg = load_config(home)
    err = capsys.readouterr().err
    assert cfg.get("llm", "caps", "daily_usd") == 0.25                                      # the guardrail key stayed as configured
    assert cfg.get("base_value_usd", "tool_outage") == 50 and cfg.get("base_value_usd", "nonsense") is None
    assert cfg.get("base_value_usd", "cfp_submit") == 900                                   # the one valid, learnable entry is used
    assert len(cfg.warnings) == 3 and all(w.startswith("settings.yaml: ") and w.endswith("; ignored") for w in cfg.warnings)
    assert "llm.caps.daily_usd: not a learnable key" in err and "tool_outage: -5 is outside 0 to 100000" in err and "99" not in err.replace("100000", "")
    assert DEFAULTS["base_value_usd"]["cfp_submit"] == 800 and DEFAULTS["llm"]["caps"]["daily_usd"] == 0.50   # the shared defaults are not written into


def test_apply_and_undo_will_not_rewrite_an_overlay_that_holds_unusable_entries(cli, home):
    (home / "settings.yaml").write_text(TAMPERED, encoding="utf-8")
    before = tree(home)
    code, out, err = cli("settings", "apply", "budgets.hourly_rate_usd=70", "--yes")
    assert code == 2 and "not rewritten" in err and "llm.caps.daily_usd: not a learnable key" in err
    assert tree(home) == before
    code, out, err = cli("settings", "undo")                                                 # nothing logged, so refused for that
    assert code == 2 and tree(home) == before
    (home / "settings.yaml").write_text("base_value_usd: [1, 2\n", encoding="utf-8")        # not even YAML
    code, out, err = cli("settings", "apply", "budgets.hourly_rate_usd=70", "--yes")
    assert code == 2 and "cannot be read as YAML" in err and (home / "settings.yaml").read_text() == "base_value_usd: [1, 2\n"
    assert not (home / "logs" / "settings.jsonl").exists()


def test_a_file_that_cannot_be_written_changes_nothing_and_a_late_failure_is_withdrawn_in_the_log(cli, home, monkeypatch):
    path = home / "logs" / "settings.jsonl"
    path.mkdir()                                                                             # the audit file cannot be opened
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    assert code == 1 and "nothing was changed" in err and not (home / "settings.yaml").exists() and not list(home.glob("*.tmp"))
    path.rmdir()
    real_replace = os.replace
    monkeypatch.setattr(S.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "disk went away")))
    code, out, err = cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")
    monkeypatch.setattr(S.os, "replace", real_replace)
    assert code == 1 and "nothing was changed" in err and not (home / "settings.yaml").exists() and not list(home.glob("*.tmp"))
    assert [(r["id"], r["event"]) for r in audit(home)] == [(1, "apply"), (1, "failed")]
    out = cli("settings", "log")[1]
    assert "apply (failed: not written)" in out
    assert cli("settings", "undo")[0] == 2                                                   # a withdrawn apply is not undoable
    assert cli("settings", "apply", "base_value_usd.cfp_submit=900", "--yes")[0] == 0
    assert [(r["id"], r["event"]) for r in audit(home)][-1] == (2, "apply")                  # the withdrawn id is not reused


def test_log_lists_each_line_and_marks_what_was_undone(cli, home):
    out = cli("settings", "log")[1]
    assert out.strip() == "no settings changes logged yet (logs/settings.jsonl)"
    cli("settings", "apply", "base_value_usd.cfp_submit=900", "--note", "first", "--yes")
    cli("settings", "apply", "budgets.hourly_rate_usd=70", "--yes")
    cli("settings", "undo", "2")
    code, out, err = cli("settings", "log")
    lines = out.splitlines()
    assert code == 0 and lines[0].split() == ["id", "time", "what", "changes", "note"] and len(lines) == 4
    assert lines[1].startswith("#1") and "apply" in lines[1] and "base_value_usd.cfp_submit 800 -> 900" in lines[1] and lines[1].endswith("first")
    assert "(undone by #3)" in lines[2] and "budgets.hourly_rate_usd 80 -> 70" in lines[2]
    assert "undo of #2" in lines[3] and "budgets.hourly_rate_usd 70 -> 80" in lines[3]
    out = cli("settings", "log", "--last", "1")[1]
    assert len(out.splitlines()) == 2 and "#3" in out
    with pytest.raises(SystemExit):
        cli("settings", "log", "--last", "0")
    (home / "logs" / "settings.jsonl").write_text((home / "logs" / "settings.jsonl").read_text() + "not json at all\n")
    assert len(cli("settings", "log")[1].splitlines()) == 4                                  # a damaged line is skipped, not fatal


def test_apply_undo_and_log_never_open_the_main_database(cli, home, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("settings must not open the main database")
    monkeypatch.setattr("ninshubur.__main__.connect", boom)
    assert cli("settings", "apply", "costs.travel_abroad_usd=700", "--yes")[0] == 0
    assert cli("settings", "log")[0] == 0 and cli("settings", "undo")[0] == 0
    assert not (home / "ninshubur.db").exists()


def test_a_value_applied_moves_the_suggestion_the_next_run_builds(home, capsys, monkeypatch):
    """One replay run: base_value_usd.cfp_submit 800 -> 1600 doubles the value of the cfp_submit suggestions, which are linear in it."""
    from ninshubur.__main__ import main
    from tests.conftest import SAMPLES, write_profiles
    monkeypatch.setattr("ninshubur.__main__.time.time", lambda: NOW)
    write_config(home)
    write_profiles(home)

    def run(*args: str) -> str:
        assert main(list(args)) == 0
        return capsys.readouterr().out

    def cfp_values() -> dict[int, float]:
        return {r["id"]: r["est_value"] for r in json.loads(run("suggest", "--json", "--top", "50")) if r["kind"] == "cfp_submit"}

    run("run", "--replay", str(SAMPLES))
    before = cfp_values()
    assert len(before) >= 2 and all(v > 0 for v in before.values())
    run("settings", "apply", "base_value_usd.cfp_submit=1600", "--yes")
    assert cfp_values() == before                                                             # applied, not yet run: nothing moved
    run("run", "--replay", str(SAMPLES))
    after = cfp_values()
    assert after.keys() == before.keys() and all(after[i] == pytest.approx(2 * before[i], abs=0.02) for i in before), (before, after)
    run("settings", "undo")
    run("run", "--replay", str(SAMPLES))
    assert cfp_values() == before                                                             # and back


def test_the_overlay_is_read_whatever_config_file_is_named(home, monkeypatch, tmp_path):
    from ninshubur.config import load_config
    other = tmp_path / "other.yaml"
    other.write_text("sources: {}\nbudgets:\n  hourly_rate_usd: 65\n", encoding="utf-8")
    (home / "settings.yaml").write_text("budgets:\n  hourly_rate_usd: 70\n", encoding="utf-8")
    assert load_config(home, path=other).get("budgets", "hourly_rate_usd") == 70            # the overlay wins over config.yaml, as list says
    (home / "settings.yaml").unlink()
    assert load_config(home, path=other).get("budgets", "hourly_rate_usd") == 65 and load_config(home, path=other).warnings == []
