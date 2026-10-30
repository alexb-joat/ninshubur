"""OpenAI-compatible chat client for the analysers.

Two slots, and the difference matters for person data:

* ``local``: a model on this machine. The only slot that counts as local, so the only one that may see person data.
  On first use the client asks its ``/models`` list and refuses the slot if the answer comes from a router
  (``owned_by: trioditis``): a router on 127.0.0.1 may still forward to a cloud model.
* ``router``: an OpenAI-compatible gateway such as Trioditis. Always counts as remote, even on 127.0.0.1.
  (The older ``cloud`` slot is remote too.)

Every call goes: ``policy.allow_llm`` -> daily caps (spend so far plus this call's estimate) -> one concurrency
limit per slot -> POST ``/chat/completions`` without redirects or proxy variables -> retries -> cost into the
budget ledger -> one line in ``logs/llm.jsonl``. The log never holds prompt or completion text.

Retries: a 429 with ``Retry-After`` up to 60 s waits and tries again; a 429 that says the budget is spent stops.
Server errors and dropped connections back off (1, 2, 4 s) for 4 tries in all. 400/401/403/404/422 and redirects stop.
"""
from __future__ import annotations

import asyncio
import email.utils
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .db import ledger_add, ledger_sum
from .logs import JsonlLog

MAX_TRIES = 4
MAX_RETRY_AFTER_S = 60.0
CHARS_PER_TOKEN = 4  # rough estimate for the pre-call budget check only; real counts come from the response
REMOTE_SLOTS = ("router", "cloud")
SLOTS = ("local",) + REMOTE_SLOTS
FATAL_STATUSES = {400, 401, 403, 404, 413, 415, 422}


class LLMError(Exception):
    """A call that did not produce a reply. ``fatal`` means the whole run should stop, not just this item."""

    def __init__(self, code: str, message: str, *, fatal: bool = False, status: int | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.fatal, self.status = code, message, fatal, status


class Refused(LLMError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, fatal=False)


class BudgetSpent(LLMError):
    def __init__(self, message: str):
        super().__init__("budget_spent", message, fatal=True)


@dataclass
class Slot:
    name: str
    base_url: str
    model: str
    local: bool
    api_key_env: str | None = None
    timeout_s: float = 60.0
    concurrency: int = 1
    price_in: float = 0.0   # USD per million input tokens
    price_out: float = 0.0  # USD per million output tokens
    params: dict = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    model: str            # what the server says answered (router header first, then the JSON body)
    input_tokens: int
    output_tokens: int
    usd: float
    latency_ms: int
    attempts: int


def _price(spec: Any) -> tuple[float, float]:
    spec = spec if isinstance(spec, dict) else {}
    return float(spec.get("input", 0) or 0), float(spec.get("output", 0) or 0)


def slot_from_config(cfg, name: str, model: str | None = None) -> Slot:
    """Build a slot from ``llm.<name>``. ``model`` picks an entry from ``llm.router.models`` (or overrides the slot model)."""
    if name not in SLOTS:
        raise LLMError("unknown_slot", f"no LLM slot {name!r}; use one of {', '.join(SLOTS)}", fatal=True)
    raw = cfg.get("llm", name, default={}) or {}
    if name != "local" and not raw.get("enabled"):
        raise LLMError("slot_disabled", f"llm.{name}.enabled is false", fatal=True)
    chosen = model or raw.get("model") or ""
    models = raw.get("models") or {}
    if not chosen:
        raise LLMError("no_model", f"llm.{name}: no model given", fatal=True)
    if chosen == "auto" or chosen.startswith("route/"):
        raise LLMError("implicit_model", "name an explicit model; 'auto' and routes hide which model answered", fatal=True)
    if name == "local":
        price_in = price_out = 0.0
        params = dict(raw.get("params") or {})
    elif chosen in models:
        price_in, price_out = _price((models[chosen] or {}).get("price_usd_per_mtok"))
        params = dict((models[chosen] or {}).get("params") or {})
    elif raw.get("price_usd_per_mtok") and chosen == raw.get("model"):
        price_in, price_out = _price(raw.get("price_usd_per_mtok"))
        params = dict(raw.get("params") or {})
    else:
        # No price means no budget check, so a remote model without one is refused rather than run blind.
        raise LLMError("no_price", f"llm.{name}: model {chosen!r} has no price_usd_per_mtok", fatal=True)
    return Slot(name=name, base_url=str(raw.get("base_url", "")).rstrip("/"), model=chosen, local=(name == "local"),
                api_key_env=raw.get("api_key_env"), timeout_s=float(raw.get("timeout_s", 60) or 60),
                concurrency=max(1, int(raw.get("concurrency", 1 if name == "local" else 4) or 1)),
                price_in=price_in, price_out=price_out, params=params)


def estimate_usd(slot: Slot, messages: list[dict], max_tokens: int) -> float:
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return (chars / CHARS_PER_TOKEN) * slot.price_in / 1e6 + max_tokens * slot.price_out / 1e6


def _retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - now)


def _error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
        return str(msg or "")[:200]
    except (ValueError, AttributeError):
        return ""


class LLMClient:
    def __init__(self, conn, policy, cfg, home: Path, *, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 env: dict | None = None):
        import os
        self.conn, self.policy, self.cfg = conn, policy, cfg
        self.now, self.sleep = now, sleep
        self.env = env if env is not None else os.environ
        self.log = JsonlLog(Path(home) / "logs" / "llm.jsonl")
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, verify=True,
                                         headers={"User-Agent": cfg.user_agent})
        self._limits: dict[str, asyncio.Semaphore] = {}
        self._local_checked: bool | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- guards -----------------------------------------------------------------------------------
    def spent_today(self) -> tuple[float, float]:
        since = self.now() - 86400
        return ledger_sum(self.conn, "llm_usd", since), ledger_sum(self.conn, "llm_tokens", since)

    def _check_caps(self, slot: Slot, estimate: float, est_tokens: int) -> None:
        usd, tokens = self.spent_today()
        cap_usd = float(self.cfg.get("llm", "caps", "daily_usd", default=0) or 0)
        cap_tokens = float(self.cfg.get("llm", "caps", "daily_tokens", default=0) or 0)
        if slot.local:
            return  # a local model costs time, not money; its tokens are recorded separately (llm_tokens_local)
        if usd + estimate > cap_usd:
            raise BudgetSpent(f"daily LLM budget: spent ${usd:.4f} + this call up to ${estimate:.4f} > cap ${cap_usd:.2f}")
        if cap_tokens and tokens + est_tokens > cap_tokens:
            raise BudgetSpent(f"daily LLM tokens: {tokens:.0f} + up to {est_tokens} > cap {cap_tokens:.0f}")

    async def verify_local(self) -> None:
        """Refuse a 'local' endpoint that is really a router. Checked once per client."""
        if self._local_checked:
            return
        slot = slot_from_config(self.cfg, "local")
        router_url = str(self.cfg.get("llm", "router", "base_url", default="") or "").rstrip("/")
        if router_url and router_url == slot.base_url:
            raise Refused("local_is_router", "llm.local.base_url is the router's address")
        try:
            resp = await self._client.get(f"{slot.base_url}/models", timeout=10.0)
            owners = {str(m.get("owned_by", "")).lower() for m in resp.json().get("data", [])} if resp.status_code == 200 else set()
        except (httpx.HTTPError, ValueError, AttributeError):
            owners = set()  # an unreachable local model fails later, on the real call
        if "trioditis" in owners:
            raise Refused("local_is_router", "llm.local answers as a Trioditis router; person data must not go there")
        self._local_checked = True

    # -- the call ---------------------------------------------------------------------------------
    async def chat(self, slot_name: str, messages: list[dict], *, model: str | None = None, person_data: bool = False,
                   max_tokens: int = 2000, response_format: dict | None = None, session_id: str | None = None,
                   ref: str | None = None) -> Reply:
        slot = slot_from_config(self.cfg, slot_name, model)
        refusal = self.policy.allow_llm(person_data=person_data, local=slot.local)
        if refusal:
            self._log(slot, ref, session_id, error=f"refused:{refusal.code}")
            raise Refused(refusal.code, refusal.reason)
        if slot.local:
            await self.verify_local()
        est_tokens = sum(len(str(m.get("content", ""))) for m in messages) // CHARS_PER_TOKEN + max_tokens
        self._check_caps(slot, estimate_usd(slot, messages, max_tokens), est_tokens)

        body: dict[str, Any] = {"model": slot.model, "messages": messages, "max_tokens": max_tokens, **slot.params}
        if response_format:
            body["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        key = self.env.get(slot.api_key_env) if slot.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if session_id:
            headers["X-Session-Id"] = session_id

        limit = self._limits.setdefault(slot.name, asyncio.Semaphore(slot.concurrency))
        async with limit:
            return await self._send(slot, body, headers, ref, session_id)

    async def _send(self, slot: Slot, body: dict, headers: dict, ref: str | None, session_id: str | None) -> Reply:
        url = f"{slot.base_url}/chat/completions"
        started = self.now()
        last: LLMError | None = None
        for attempt in range(1, MAX_TRIES + 1):
            try:
                resp = await self._client.post(url, json=body, headers=headers, timeout=slot.timeout_s)
            except httpx.TransportError as e:
                last = LLMError("transport", type(e).__name__)
                await self._backoff(attempt)
                continue
            status = resp.status_code
            if status == 200:
                return self._accept(slot, resp, started, attempt, ref, session_id)
            msg = _error_message(resp)
            if status == 429:
                if "budget" in msg.lower():
                    self._log(slot, ref, session_id, status=status, attempts=attempt, error="budget_spent")
                    raise BudgetSpent(f"router says: {msg}")
                wait = _retry_after(resp.headers.get("Retry-After"), self.now())
                if wait is not None and wait > MAX_RETRY_AFTER_S:
                    self._log(slot, ref, session_id, status=status, attempts=attempt, error="rate_limited_long")
                    raise LLMError("rate_limited", f"Retry-After {wait:.0f}s is over {MAX_RETRY_AFTER_S:.0f}s", fatal=True, status=status)
                last = LLMError("rate_limited", msg or "429", status=status)
                if attempt < MAX_TRIES:
                    await self.sleep(wait if wait is not None else 2 ** (attempt - 1))
                continue
            if 300 <= status < 400 or status in FATAL_STATUSES:
                self._log(slot, ref, session_id, status=status, attempts=attempt, error=f"http_{status}")
                raise LLMError(f"http_{status}", msg or "request refused", fatal=True, status=status)
            last = LLMError(f"http_{status}", msg or "server error", status=status)
            await self._backoff(attempt)
        self._log(slot, ref, session_id, attempts=MAX_TRIES, error=last.code if last else "failed")
        raise last or LLMError("failed", "no reply")

    async def _backoff(self, attempt: int) -> None:
        if attempt < MAX_TRIES:
            await self.sleep(2 ** (attempt - 1))

    def _accept(self, slot: Slot, resp: httpx.Response, started: float, attempt: int, ref: str | None,
                session_id: str | None) -> Reply:
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as e:
            self._log(slot, ref, session_id, status=200, attempts=attempt, error="bad_response")
            raise LLMError("bad_response", f"not an OpenAI chat completion ({type(e).__name__})") from e
        header_model = resp.headers.get("X-Trioditis-Model")
        if header_model is not None and header_model != slot.model:
            # The router answered with a different model than we asked for (fallback): the result would be
            # attributed to the wrong model in the evaluation, so it is rejected.
            self._log(slot, ref, session_id, status=200, attempts=attempt, error="model_mismatch", model_reported=header_model)
            raise LLMError("model_mismatch", f"asked for {slot.model}, router used {header_model}")
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
        usd = tin * slot.price_in / 1e6 + tout * slot.price_out / 1e6
        ledger_ref = ref or f"llm:{slot.name}:{slot.model}"
        ledger_add(self.conn, "llm_usd", usd, ledger_ref)
        ledger_add(self.conn, "llm_tokens_local" if slot.local else "llm_tokens", tin + tout, ledger_ref)
        latency = int((self.now() - started) * 1000)
        reported = header_model or str(data.get("model") or slot.model)
        self._log(slot, ref, session_id, status=200, attempts=attempt, tokens_in=tin, tokens_out=tout, usd=round(usd, 6),
                  latency_ms=latency, model_reported=reported)
        return Reply(text=text, model=reported, input_tokens=tin, output_tokens=tout, usd=usd, latency_ms=latency, attempts=attempt)

    def _log(self, slot: Slot, ref: str | None, session_id: str | None, **fields: Any) -> None:
        # Never the prompt, never the completion: only what is needed to audit cost and failures.
        self.log.write({"slot": slot.name, "model": slot.model, "local": slot.local, "ref": ref, "session": session_id, **fields})
