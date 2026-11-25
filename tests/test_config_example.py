"""config.example.yaml stays true to the code: its User-Agent names the running version, and its router block loads and,
once a person fills in its commented model, turns the slot on."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import ninshubur
from ninshubur.config import DEFAULTS, ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"


def _example_text() -> str:
    return EXAMPLE.read_text(encoding="utf-8")


def _with_router(text: str, models_block: bool, enabled: bool) -> str:
    """The example's own router block, optionally switched on and with its commented `models:` lines uncommented and filled in."""
    if enabled:
        text = text.replace("    enabled: false\n    base_url: http://127.0.0.1:8787/v1", "    enabled: true\n    base_url: http://127.0.0.1:8787/v1")
    if models_block:
        text = re.sub(r"^(    )# (models:\n)    # (  <model name your router serves>:\n)    # (    price_usd_per_mtok: .*)$",
                      r"\1\2\1\3\1\4", text, flags=re.M)
        text = text.replace("<model name your router serves>", "example-model")
        text = text.replace("<USD per million input tokens>", "0").replace("<USD per million output tokens>", "0")
    return text


def test_the_default_and_the_examples_user_agent_name_the_running_version_and_agree():
    default = DEFAULTS["user_agent"]
    assert default.startswith(f"Ninshubur/{ninshubur.__version__} (")
    assert yaml.safe_load(_example_text())["user_agent"] == default       # fails when a release bumps one and not the other
    assert "Ninshubur/0.1" not in _example_text()


def test_the_example_config_loads_and_its_router_slot_is_off(home):
    cfg = load_config(home, path=EXAMPLE)
    assert cfg.get("llm", "router", "enabled") is False
    assert cfg.get("llm", "router", "api_key_env") == "NINSHUBUR_ROUTER_KEY"
    assert cfg.get("llm", "router", "models") == {}            # the model lines are comments: nothing priced is invented


def test_the_router_block_enabled_without_a_model_is_refused_with_the_key_named(home, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_with_router(_example_text(), models_block=False, enabled=True), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"llm\.router\.models"):
        load_config(home, path=p)


def test_uncommenting_the_examples_model_lines_turns_the_router_slot_on(home, tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_with_router(_example_text(), models_block=True, enabled=True), encoding="utf-8")
    cfg = load_config(home, path=p)
    assert cfg.get("llm", "router", "enabled") is True
    assert cfg.get("llm", "router", "models") == {"example-model": {"price_usd_per_mtok": {"input": 0, "output": 0}}}
