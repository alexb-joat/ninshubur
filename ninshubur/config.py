"""Configuration: ``config.yaml`` (sources with trust blocks, budgets, LLM endpoints, sinks).

Secrets never live here, only the names of environment variables. Loading validates the
shape and the trust blocks; anything invalid is a ``ConfigError`` and nothing runs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .home import data_home
from .policy import SECONDS_PER_DAY, Policy, _parse_day, check_url, host_of

DEFAULTS: dict[str, Any] = {
    "user_agent": "Ninshubur/0.1 (+https://leadget.ai/bot; contact: alex@leadget.ai)",
    "timezone": "Europe/Lisbon",
    "llm": {
        "local": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b", "embed_model": "nomic-embed-text",
                  "timeout_s": 60},
        "cloud": {"enabled": False, "base_url": "https://api.openai.com/v1", "api_key_env": "NINSHUBUR_CLOUD_KEY",
                  "model": "gpt-5-mini", "price_usd_per_mtok": {"input": 0.25, "output": 2.0}},
        "caps": {"daily_usd": 0.50, "daily_tokens": 300000},
        "person_data": "local_only",
    },
    "budgets": {"daily_bytes": 200_000_000, "daily_human_minutes": 45, "hourly_rate_usd": 60},
    "scheduler": {"tick_s": 30, "max_concurrent_sources": 4, "per_host_rps": 1.0, "boost_cooldown_s": 600, "jitter": 0.10},
    "retention_months": 12,
    "corroboration_value_usd": 300,
    "base_value_usd": {
        "service_request_lead": 400, "hiring_signal_outreach": 250, "outreach_window": 200, "conference_prospecting": 600,
        "cfp_submit": 800, "local_meeting_suggestion": 150, "pain_content_idea": 60, "tooling_adopt": 100,
        "tool_deprecation": 300, "tool_price_change": 100, "tool_outage": 50,
    },
    "costs": {
        "travel_same_city_usd": 0, "travel_same_country_usd": 150, "travel_abroad_usd": 600,
        "minutes": {"conference_prospecting": 20, "cfp_submit": 120, "hiring_signal_outreach": 15, "service_request_lead": 10,
                    "tool_outage": 0, "outreach_window": 15, "local_meeting_suggestion": 20},
    },
    "sinks": {
        "ics": {"path": "feeds/timings.ics"},
        "trello": {"enabled": False, "key_env": "TRELLO_KEY", "token_env": "TRELLO_TOKEN", "list_id": ""},
        "google_calendar": {"enabled": False, "credentials_env": "GOOGLE_CAL_CREDENTIALS_JSON", "calendar_id": "primary"},
        "platform": {"enabled": False, "base_url": "", "token_env": "LEADGET_API_TOKEN"},
    },
    "api": {"host": "127.0.0.1", "port": 8791},
    "sources": {},
}

TRUST_FIELDS = ("tier", "owner", "owner_url", "terms_url", "licence", "legal_basis", "rate_limit",
                "expected_content_type", "schema", "reviewed_by", "reviewed_at", "review_every_days")


class ConfigError(Exception):
    pass


@dataclass
class SourceConfig:
    name: str
    adapter: str
    params: dict = field(default_factory=dict)
    enabled: bool = True
    trust: dict = field(default_factory=dict)
    base_interval_s: int | None = None
    max_interval_s: int | None = None

    @property
    def tier(self) -> int:
        return int(self.trust.get("tier", 3))

    @property
    def schema_id(self) -> str:
        return str(self.trust.get("schema", ""))

    @property
    def legal_basis(self) -> str:
        return str(self.trust.get("legal_basis", "public_business_info"))

    @property
    def expected_content_type(self) -> str:
        return str(self.trust.get("expected_content_type", ""))

    def reviewed_at_ts(self) -> float | None:
        return _parse_day(self.trust.get("reviewed_at"))

    def review_due_at(self) -> float | None:
        at = self.reviewed_at_ts()
        every = self.trust.get("review_every_days")
        if at is None or every is None:
            return None
        return at + int(every) * SECONDS_PER_DAY


@dataclass
class Config:
    data: dict
    sources: dict[str, SourceConfig]
    path: Path | None
    warnings: list[str] = field(default_factory=list)

    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def user_agent(self) -> str:
        return str(self.data["user_agent"])


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(home: Path | None = None) -> Path | None:
    env = os.environ.get("NINSHUBUR_CONFIG")
    candidates = [Path(env).expanduser()] if env else []
    candidates += [(home or data_home()) / "config.yaml", Path("config.yaml")]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(home: Path | None = None, path: Path | None = None, policy: Policy | None = None) -> Config:
    path = path or config_path(home)
    raw: dict = {}
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise ConfigError(f"cannot load {path}: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} is not a mapping")
    data = _deep_merge(DEFAULTS, raw)
    cfg = Config(data=data, sources={}, path=path)
    _validate_llm(cfg)
    _validate_api(cfg)
    for name, s in (data.get("sources") or {}).items():
        cfg.sources[name] = _source(name, s, policy, cfg.warnings)
    return cfg


def _validate_llm(cfg: Config) -> None:
    local = cfg.get("llm", "local", "base_url")
    if check_url(str(local), allow_http_local=True) is not None or host_of(str(local)) not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(f"llm.local.base_url must be a local http(s) endpoint, got {local!r}")
    cloud = cfg.get("llm", "cloud", "base_url")
    if cfg.get("llm", "cloud", "enabled") and check_url(str(cloud)) is not None:
        raise ConfigError(f"llm.cloud.base_url must be https, got {cloud!r}")
    if cfg.get("llm", "person_data") != "local_only":
        raise ConfigError("llm.person_data must be 'local_only'")
    for k in ("daily_usd", "daily_tokens"):
        if float(cfg.get("llm", "caps", k, default=0)) < 0:
            raise ConfigError(f"llm.caps.{k} must be >= 0")


def _validate_api(cfg: Config) -> None:
    host = str(cfg.get("api", "host"))
    key = os.environ.get("NINSHUBUR_API_KEY", "")
    if host not in ("127.0.0.1", "localhost", "::1") and len(key) < 32:
        raise ConfigError("api.host is not loopback and NINSHUBUR_API_KEY is missing or shorter than 32 chars")


def _source(name: str, s: Any, policy: Policy | None, warnings: list[str]) -> SourceConfig:
    if not isinstance(s, dict) or not s.get("adapter"):
        raise ConfigError(f"source {name!r}: needs an adapter")
    if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum():
        raise ConfigError(f"source name {name!r}: letters, digits, - and _ only")
    trust = s.get("trust") or {}
    missing = [k for k in TRUST_FIELDS if trust.get(k) in (None, "")]
    if missing:
        raise ConfigError(f"source {name!r}: trust block missing {missing}")
    try:
        tier = int(trust["tier"])
    except (TypeError, ValueError):
        raise ConfigError(f"source {name!r}: tier must be 0-3") from None
    if tier not in (0, 1, 2, 3):
        raise ConfigError(f"source {name!r}: tier must be 0-3")
    for u in ("owner_url", "terms_url"):
        if check_url(str(trust[u])):
            raise ConfigError(f"source {name!r}: trust.{u} must be a valid https URL")
    if _parse_day(trust.get("reviewed_at")) is None:
        raise ConfigError(f"source {name!r}: trust.reviewed_at must be an ISO date")
    if policy is not None:
        if trust["legal_basis"] not in policy.legal_bases:
            raise ConfigError(f"source {name!r}: legal_basis {trust['legal_basis']!r} not allowed by policy")
        limit = int(policy.tier(tier)["review_every_days"])
        if int(trust["review_every_days"]) > limit:
            raise ConfigError(f"source {name!r}: review_every_days {trust['review_every_days']} exceeds tier {tier} max {limit}")
    params = s.get("params") or {}
    if not isinstance(params, dict):
        raise ConfigError(f"source {name!r}: params must be a mapping")
    return SourceConfig(name=name, adapter=str(s["adapter"]), params=params, enabled=bool(s.get("enabled", True)),
                        trust=trust, base_interval_s=s.get("base_interval_s"), max_interval_s=s.get("max_interval_s"))
