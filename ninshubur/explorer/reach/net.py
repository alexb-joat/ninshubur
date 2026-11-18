"""The shared hardened ``Fetcher``, bound to the explorer's database and logs (``~/.ninshubur/explorer/logs``), with
a fixed-host spec per reach API. The explorer's byte ledger, robots cache and fetch log are its own."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ...adapters.base import AdapterSpec
from ...config import Config, SourceConfig
from ...fetch import Fetcher
from ...policy import Policy
from ..store import explorer_home


def reach_spec(name: str, hosts: set[str], schema: str, *, min_delay_s: float = 1.0) -> AdapterSpec:
    return AdapterSpec(name=name, kind="news", hosts=frozenset(hosts), schema=schema, schemas=(schema,),
                       expected_content_type="application/json", base_interval_s=86400, max_interval_s=7 * 86400,
                       default_tier=3, legal_basis="public_business_info", min_delay_s=min_delay_s)


def reach_source(spec: AdapterSpec) -> SourceConfig:
    return SourceConfig(name=spec.name, adapter=spec.name,
                        trust={"tier": 3, "schema": spec.schema, "expected_content_type": spec.expected_content_type})


def explorer_fetcher(conn: sqlite3.Connection, policy: Policy, cfg: Config, home: Path, *, transport=None,
                     resolve=None, now=None, sleep=None) -> Fetcher:
    kw = {"transport": transport, "resolve": resolve}
    if now is not None:
        kw["now"] = now
    if sleep is not None:
        kw["sleep"] = sleep
    return Fetcher(conn, policy, cfg, explorer_home(home), **kw)
