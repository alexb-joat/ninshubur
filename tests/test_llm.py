"""LLM client: person data never reaches a remote slot, budgets stop runs, retries follow the rules, logs hold no text.
Every test runs on a mock transport; nothing here needs a model or Trioditis."""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import httpx
import pytest
import yaml

from ninshubur.config import ConfigError, load_config
from ninshubur.db import connect, ledger_add, ledger_sum
from ninshubur.llm import BudgetSpent, LLMClient, LLMError, Refused, slot_from_config
from ninshubur.policy import Policy
from tests.conftest import NOW

LOCAL = "http://127.0.0.1:11434/v1"
ROUTER = "http://127.0.0.1:8787/v1"
PROMPT = "Company evidence: ACME is hiring a RevOps engineer."
HAIKU = {"price_usd_per_mtok": {"input": 1.0, "output": 5.0}}


class Clock:
    def __init__(self) -> None:
        self.t = NOW
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def write_cfg(home: Path, **llm) -> None:
    base = {"router": {"enabled": True, "base_url": ROUTER, "models": {"haiku": HAIKU}},
            "caps": {"daily_usd": 1.0, "daily_tokens": 1_000_000}}
    for k, v in llm.items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    (home / "config.yaml").write_text(yaml.safe_dump({"llm": base}), encoding="utf-8")


def ok(model: str | None = "haiku", tin: int = 1000, tout: int = 200, text: str = '{"qualified": true}') -> httpx.Response:
    headers = {"X-Trioditis-Model": model} if model else {}
    return httpx.Response(200, headers=headers, json={"model": "upstream-id", "choices": [{"message": {"content": text}}],
                                                      "usage": {"prompt_tokens": tin, "completion_tokens": tout}})


class Server:
    """Answers each request from a list of responses (or callables) in order, recording what it was sent."""

    def __init__(self, *answers, models_owner: str = "library"):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.models_owner = models_owner

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen", "owned_by": self.models_owner}]})
        self.requests.append(request)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return await a(request) if inspect.iscoroutinefunction(a) else (a(request) if callable(a) else a)


def make(home: Path, server: Server, clock: Clock | None = None, env: dict | None = None):
    clock = clock or Clock()
    policy = Policy.load(home)
    cfg = load_config(home, policy=policy)
    conn = connect(home)
    client = LLMClient(conn, policy, cfg, home, transport=httpx.MockTransport(server), now=clock.now, sleep=clock.sleep,
                       env=env or {})
    return client, conn, clock


def run(coro):
    return asyncio.run(coro)


async def call(client, slot="router", **kw):
    try:
        return await client.chat(slot, [{"role": "user", "content": PROMPT}], model=kw.pop("model", "haiku" if slot == "router" else None), **kw)
    finally:
        await client.aclose()


# -- person data ------------------------------------------------------------------------------------
def test_person_data_never_reaches_the_router(home):
    write_cfg(home)
    server = Server(ok())
    client, conn, _ = make(home, server)
    with pytest.raises(Refused) as e:
        run(call(client, person_data=True))
    assert e.value.code == "person_data_remote_llm"
    assert server.requests == []


def test_person_data_may_go_to_the_local_model(home):
    write_cfg(home)
    server = Server(ok(model=None))
    client, conn, _ = make(home, server)
    reply = run(call(client, slot="local", person_data=True))
    assert reply.usd == 0 and len(server.requests) == 1
    assert ledger_sum(conn, "llm_tokens_local", 0) == 1200 and ledger_sum(conn, "llm_tokens", 0) == 0


def test_a_local_slot_that_answers_as_trioditis_is_refused(home):
    write_cfg(home)
    server = Server(ok(model=None), models_owner="trioditis")
    client, conn, _ = make(home, server)
    with pytest.raises(Refused) as e:
        run(call(client, slot="local", person_data=True))
    assert e.value.code == "local_is_router" and server.requests == []


def test_config_refuses_a_local_address_equal_to_the_router(home):
    write_cfg(home, local={"base_url": ROUTER})
    with pytest.raises(ConfigError, match="router"):
        load_config(home, policy=Policy.load(home))


def test_config_refuses_a_router_without_priced_models(home):
    write_cfg(home, router={"models": {}})
    with pytest.raises(ConfigError, match="price"):
        load_config(home, policy=Policy.load(home))


def test_auto_and_unpriced_models_are_refused(home):
    write_cfg(home)
    cfg = load_config(home, policy=Policy.load(home))
    with pytest.raises(LLMError, match="explicit"):
        slot_from_config(cfg, "router", "auto")
    with pytest.raises(LLMError) as e:
        slot_from_config(cfg, "router", "sonnet")
    assert e.value.code == "no_price"


# -- budget -----------------------------------------------------------------------------------------
def test_spent_budget_stops_before_the_request(home):
    write_cfg(home)
    server = Server(ok())
    client, conn, clock = make(home, server)
    ledger_add(conn, "llm_usd", 0.999, "earlier", ts=clock.now() - 60)
    with pytest.raises(BudgetSpent):
        run(call(client))
    assert server.requests == []


def test_router_budget_429_stops_without_retrying(home):
    write_cfg(home)
    server = Server(httpx.Response(429, json={"error": {"message": "cloud budget exceeded: daily_usd; no local model"}}))
    client, conn, clock = make(home, server)
    with pytest.raises(BudgetSpent):
        run(call(client))
    assert len(server.requests) == 1 and clock.sleeps == []


def test_cost_goes_into_the_ledger(home):
    write_cfg(home)
    client, conn, _ = make(home, Server(ok(tin=1_000_000, tout=100_000)))
    reply = run(call(client, max_tokens=100))
    assert reply.usd == pytest.approx(1.0 + 0.5)
    assert ledger_sum(conn, "llm_usd", 0) == pytest.approx(1.5)
    assert ledger_sum(conn, "llm_tokens", 0) == 1_100_000


# -- retries ----------------------------------------------------------------------------------------
def test_retry_after_is_honoured(home):
    write_cfg(home)
    server = Server(httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"message": "slow down"}}), ok())
    client, conn, clock = make(home, server)
    reply = run(call(client))
    assert clock.sleeps == [7.0] and reply.attempts == 2


def test_retry_after_over_a_minute_stops_the_run(home):
    write_cfg(home)
    server = Server(httpx.Response(429, headers={"Retry-After": "600"}))
    client, conn, clock = make(home, server)
    with pytest.raises(LLMError) as e:
        run(call(client))
    assert e.value.fatal and clock.sleeps == []


def test_server_errors_back_off_then_give_up_after_four_tries(home):
    write_cfg(home)
    server = Server(httpx.Response(502))
    client, conn, clock = make(home, server)
    with pytest.raises(LLMError) as e:
        run(call(client))
    assert len(server.requests) == 4 and clock.sleeps == [1, 2, 4] and not e.value.fatal


def test_dropped_connection_is_retried(home):
    write_cfg(home)
    server = Server(httpx.ConnectError("reset"), ok())
    client, conn, clock = make(home, server)
    assert run(call(client)).attempts == 2


@pytest.mark.parametrize("status", [404, 422, 401, 302])
def test_client_errors_and_redirects_stop_at_once(home, status):
    write_cfg(home)
    server = Server(httpx.Response(status, headers={"Location": "https://elsewhere.example/"} if status == 302 else {}))
    client, conn, clock = make(home, server)
    with pytest.raises(LLMError) as e:
        run(call(client))
    assert e.value.fatal and len(server.requests) == 1


def test_a_different_model_from_the_router_is_rejected_and_not_billed(home):
    write_cfg(home)
    client, conn, _ = make(home, Server(ok(model="sonnet")))
    with pytest.raises(LLMError) as e:
        run(call(client))
    assert e.value.code == "model_mismatch" and ledger_sum(conn, "llm_usd", 0) == 0


# -- request shape, log and concurrency -----------------------------------------------------------
def test_request_carries_session_key_and_model_params(home):
    write_cfg(home, router={"models": {"haiku": {**HAIKU, "params": {"temperature": 0}}}})
    server = Server(ok())
    client, conn, _ = make(home, server, env={"NINSHUBUR_ROUTER_KEY": "k-123"})
    run(call(client, session_id="ninshubur/run-1"))
    req = server.requests[0]
    body = json.loads(req.content)
    assert req.headers["X-Session-Id"] == "ninshubur/run-1" and req.headers["Authorization"] == "Bearer k-123"
    assert body["model"] == "haiku" and body["temperature"] == 0 and body["max_tokens"] == 2000


def test_log_holds_no_prompt_or_completion_text(home):
    write_cfg(home)
    client, conn, _ = make(home, Server(ok(text="SECRET-COMPLETION")))
    run(call(client, ref="company:acme"))
    log = (home / "logs" / "llm.jsonl").read_text(encoding="utf-8")
    assert "ACME is hiring" not in log and "SECRET-COMPLETION" not in log
    rec = json.loads(log.splitlines()[-1])
    assert rec["ref"] == "company:acme" and rec["tokens_in"] == 1000 and rec["model_reported"] == "haiku"


def test_router_concurrency_limit_holds(home):
    write_cfg(home, router={"concurrency": 2})
    in_flight = {"now": 0, "max": 0}

    async def slow(request):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        return ok()

    client, conn, _ = make(home, Server(slow))

    async def many():
        try:
            await asyncio.gather(*(client.chat("router", [{"role": "user", "content": "x"}], model="haiku", max_tokens=10)
                                   for _ in range(6)))
        finally:
            await client.aclose()

    run(many())
    assert in_flight["max"] == 2
