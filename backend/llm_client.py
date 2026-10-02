"""Single entry point for every LLM call in the backend.

``await complete(feature=..., system=..., user=..., schema=Model)`` returns an
``LLMResult`` and NEVER raises. ``ok=False`` means "no opinion": every call
site maps it onto its existing fail-safe path (neutral sentiment, rule-based
fallback, no lesson, ...).

Backends (env ``LLM_BACKEND``):
  * ``anthropic`` — official SDK (``AsyncAnthropic``). Credentials come from the
    SDK's default resolution (``ANTHROPIC_API_KEY``). Structured output via
    ``output_config.format`` (JSON schema built from the Pydantic model with
    ``anthropic.transform_schema``), validated again with Pydantic here.
  * ``emergent``  — the legacy ``emergentintegrations`` ``LlmChat`` wrapper with
    ``EMERGENT_LLM_KEY``; schemas fall back to text-mode JSON parsing.
  Default: ``anthropic`` when ``ANTHROPIC_API_KEY`` is set, else ``emergent``.

Other env:
  LLM_DISABLED=1                 kill switch — every call returns ok=False
  LLM_BREAKER_FAILURES (5)       consecutive transport failures that open a
  LLM_BREAKER_COOLDOWN_S (60)    feature's circuit for this many seconds
  LLM_HOT_PATH_TIMEOUT_S / LLM_SWEEP_TIMEOUT_S, LLM_MODEL_*, LLM_EFFORT_*,
  LLM_CONCURRENCY_<TIER>         see llm_models
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ValidationError

import llm_models
from llm_models import UNTRUSTED_PREAMBLE, untrusted_block

logger = logging.getLogger("llm-client")

SDK_MAX_RETRIES = 2

# Error kinds. Only TRANSPORT/API-class failures (network, HTTP errors,
# timeouts, missing credentials) count toward the circuit breaker and are
# worth retrying on another model. refusal / max_tokens / parse / empty mean
# a (paid) response arrived — retrying elsewhere would pay twice.
TRANSPORT_KINDS = frozenset({"timeout", "api_error", "bad_request",
                             "not_configured", "unavailable"})


@dataclass
class LLMResult:
    ok: bool
    data: Any = None             # validated schema instance (when schema given)
    text: str = ""
    error: str | None = None
    error_kind: str | None = None  # disabled|circuit_open|timeout|api_error|
    #                                bad_request|not_configured|unavailable|
    #                                refusal|max_tokens|parse|empty
    stop_reason: str | None = None
    model: str | None = None
    backend: str | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0

    @property
    def retryable(self) -> bool:
        return self.error_kind in TRANSPORT_KINDS


# ----------------------------------------------------------------- config
def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def disabled() -> bool:
    return _truthy(os.environ.get("LLM_DISABLED"))


def backend() -> str:
    b = (os.environ.get("LLM_BACKEND") or "").strip().lower()
    if b in ("anthropic", "emergent"):
        return b
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "emergent"


def is_configured() -> bool:
    """True when the selected backend has credentials to make a call."""
    if disabled():
        return False
    if backend() == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return bool(os.environ.get("EMERGENT_LLM_KEY"))


# ------------------------------------------------- per-loop shared state
# Semaphores and the SDK's HTTP pool are bound to an event loop; tests (and
# worker processes) may run several loops, so state is kept per loop.
_loop_state: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = \
    weakref.WeakKeyDictionary()


def _state() -> dict:
    loop = asyncio.get_running_loop()
    st = _loop_state.get(loop)
    if st is None:
        st = {"sems": {}, "clients": {}}
        _loop_state[loop] = st
    return st


def _semaphore(feature: str) -> asyncio.Semaphore:
    sems = _state()["sems"]
    sem = sems.get(feature)
    if sem is None:
        sem = sems[feature] = asyncio.Semaphore(llm_models.concurrency_for(feature))
    return sem


def _anthropic_client():
    """Cached ``AsyncAnthropic`` (per loop, per credential)."""
    import anthropic
    clients = _state()["clients"]
    key = (os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_BASE_URL"))
    c = clients.get(key)
    if c is None:
        # SDK default credential resolution (ANTHROPIC_API_KEY, ...).
        c = clients[key] = anthropic.AsyncAnthropic(max_retries=SDK_MAX_RETRIES)
    return c


# -------------------------------------------------------- circuit breaker
_breakers: dict[str, dict] = {}


def _breaker_cfg() -> tuple[int, float]:
    try:
        n = max(1, int(os.environ.get("LLM_BREAKER_FAILURES", "5")))
    except ValueError:
        n = 5
    try:
        m = max(1.0, float(os.environ.get("LLM_BREAKER_COOLDOWN_S", "60")))
    except ValueError:
        m = 60.0
    return n, m


def _breaker_open(key: str) -> bool:
    b = _breakers.get(key)
    return bool(b and b.get("open_until", 0.0) > time.monotonic())


def _breaker_record(key: str, transport_failure: bool) -> None:
    """Keyed per (feature, model): an outage of one model must not block
    the fallback model of the same feature."""
    b = _breakers.setdefault(key, {"fails": 0, "open_until": 0.0})
    if not transport_failure:
        b["fails"] = 0
        return
    b["fails"] += 1
    n, cooldown = _breaker_cfg()
    if b["fails"] >= n:
        b["open_until"] = time.monotonic() + cooldown
        b["fails"] = 0
        logger.warning("LLM circuit OPEN for %s for %.0fs", key, cooldown)


def reset_breakers() -> None:
    _breakers.clear()


# ------------------------------------------------------------ usage sink
_pending: set = set()


async def _write_usage(doc: dict) -> None:
    try:
        from database import get_db
        await get_db().llm_usage.insert_one(doc)
    except Exception:  # noqa: BLE001 — accounting must never break a call
        logger.debug("llm_usage write failed", exc_info=True)


def _record_usage(doc: dict) -> None:
    """Fire-and-forget so the hot path never waits on Mongo."""
    try:
        task = asyncio.get_running_loop().create_task(_write_usage(doc))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception:  # noqa: BLE001
        logger.debug("llm_usage scheduling failed", exc_info=True)


async def drain_usage() -> None:
    """Await outstanding usage writes (tests / graceful shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


# --------------------------------------------------------------- helpers
def _json_candidate(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return text


def _validate(schema: type[BaseModel], text: str, lenient: bool) -> BaseModel:
    """Validate model output. ``lenient`` (text-mode backend) strips fences
    and surrounding prose first; structured output is parsed verbatim."""
    return schema.model_validate_json(_json_candidate(text) if lenient else text)


def _clean_history(history) -> list[dict]:
    out: list[dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":   # API: first message must be user
        out.pop(0)
    return out


def _max_tokens_for(model: str, requested: int, effort: str | None) -> int:
    """Thinking counts toward max_tokens on adaptive-thinking models; leave
    headroom so a reply sized for a non-thinking model isn't cut off."""
    if not effort:
        return requested
    floor = {"low": 4000, "medium": 8000}.get(effort, 16000)
    return max(requested, floor)


# ------------------------------------------------------------- backends
async def _call_anthropic(*, feature, model, system, messages, schema,
                          max_tokens, effort, timeout_s, cache_system) -> LLMResult:
    import anthropic

    output_config: dict = {}
    if effort:
        output_config["effort"] = effort
    if schema is not None:
        output_config["format"] = {"type": "json_schema",
                                   "schema": anthropic.transform_schema(schema)}
    sys_block: dict = {"type": "text", "text": system}
    if cache_system:
        sys_block["cache_control"] = {"type": "ephemeral"}
    kwargs: dict = {
        "model": model,
        "max_tokens": _max_tokens_for(model, max_tokens, effort),
        "system": [sys_block],
        "messages": messages,
        "timeout": timeout_s,
    }
    if output_config:
        kwargs["output_config"] = output_config

    client = _anthropic_client()
    try:
        resp = await client.messages.create(**kwargs)
    except anthropic.APITimeoutError as e:
        return LLMResult(ok=False, error=f"timeout: {e}", error_kind="timeout")
    except anthropic.APIStatusError as e:
        kind = "api_error" if (e.status_code >= 500 or e.status_code in (408, 409, 429)) \
            else "bad_request"
        if e.status_code in (401, 403):
            kind = "not_configured"
        return LLMResult(ok=False, error=f"HTTP {e.status_code}: {e.message}", error_kind=kind)
    except anthropic.APIConnectionError as e:
        return LLMResult(ok=False, error=f"connection: {e}", error_kind="api_error")
    except Exception as e:  # noqa: BLE001 — e.g. no credentials resolvable
        return LLMResult(ok=False, error=f"{type(e).__name__}: {e}", error_kind="api_error")

    u = getattr(resp, "usage", None)
    usage = {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        "cache_read_input_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
    }
    text = "".join(getattr(b, "text", "") for b in (resp.content or [])
                   if getattr(b, "type", None) == "text")
    res = LLMResult(ok=False, text=text, stop_reason=resp.stop_reason,
                    model=getattr(resp, "model", None) or model, usage=usage)
    # Branch on stop_reason BEFORE trusting content.
    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        res.error, res.error_kind = f"model refused (category={cat})", "refusal"
        return res
    if resp.stop_reason == "max_tokens":
        res.error, res.error_kind = "output truncated at max_tokens", "max_tokens"
        return res
    if schema is not None:
        try:
            res.data = _validate(schema, text, lenient=False)
        except (ValidationError, ValueError) as e:
            res.error, res.error_kind = f"schema validation failed: {e}", "parse"
            return res
    elif not text.strip():
        res.error, res.error_kind = "empty response", "empty"
        return res
    res.ok = True
    return res


async def _call_emergent(*, feature, model, system, messages, schema,
                         timeout_s, chat_cls=None, message_cls=None) -> LLMResult:
    key = os.environ.get("EMERGENT_LLM_KEY")
    if chat_cls is None or message_cls is None:
        try:
            from emergentintegrations.llm.chat import LlmChat, UserMessage
        except Exception as e:  # noqa: BLE001
            return LLMResult(ok=False, error=f"emergentintegrations unavailable: {e}",
                             error_kind="unavailable")
        chat_cls = chat_cls or LlmChat
        message_cls = message_cls or UserMessage
    if not key:
        return LLMResult(ok=False, error="EMERGENT_LLM_KEY not set",
                         error_kind="not_configured")
    sys_text = system
    if schema is not None:
        sys_text += ("\n\nRespond ONLY with one JSON object (no prose, no markdown "
                     "fences) that validates against this JSON schema:\n"
                     + json.dumps(schema.model_json_schema(), separators=(",", ":")))
    # The wrapper takes one user turn; prior turns are replayed as a transcript.
    *prior, last = messages
    user_text = last["content"]
    if prior:
        transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in prior)
        user_text = f"Conversation so far:\n{transcript}\n\nUSER: {user_text}"
    try:
        chat = chat_cls(api_key=key,
                        session_id=f"{feature}-{uuid.uuid4().hex[:8]}",
                        system_message=sys_text,
                        ).with_model(llm_models.PROVIDER,
                                     llm_models.emergent_model_id(model))
        raw = await chat.send_message(message_cls(text=user_text))
    except asyncio.TimeoutError:
        raise
    except Exception as e:  # noqa: BLE001
        return LLMResult(ok=False, error=f"{type(e).__name__}: {e}", error_kind="api_error")
    text = str(raw or "").strip()
    # The wrapper exposes no token usage — estimate (chars/4) for accounting.
    usage = {"input_tokens": (len(sys_text) + len(user_text)) // 4,
             "output_tokens": len(text) // 4,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "estimated": True}
    res = LLMResult(ok=False, text=text, model=model, usage=usage, stop_reason="end_turn")
    if schema is not None:
        try:
            res.data = _validate(schema, text, lenient=True)
        except (ValidationError, ValueError) as e:
            res.error, res.error_kind = f"schema validation failed: {e}", "parse"
            return res
    elif not text:
        res.error, res.error_kind = "empty response", "empty"
        return res
    res.ok = True
    return res


# ------------------------------------------------------------- public API
async def complete(*, feature: str, system: str, user: str,
                   schema: type[BaseModel] | None = None,
                   untrusted: list[str] | None = None,
                   timeout_s: float | None = None,
                   max_tokens: int = 1024,
                   effort: str | None = None,
                   history: list[dict] | None = None,
                   model: str | None = None,
                   cache_system: bool = True,
                   usage_meta: dict | None = None,
                   emergent_chat_cls=None,
                   emergent_message_cls=None) -> LLMResult:
    """One LLM round-trip. Never raises.

    feature     llm_models feature key — picks model, effort, timeout, limits
    system      static instructions (cached; keep volatile data in ``user``)
    user        the request text
    schema      Pydantic model → structured output, ``result.data`` instance
    untrusted   third-party lines, fenced in <untrusted_data> after ``user``
    history     prior turns [{"role": "user"|"assistant", "content": str}]
    model       explicit model id (default ``llm_models.model_for(feature)``)
    usage_meta  extra fields for the db.llm_usage document
    emergent_*  override the wrapper classes (keeps legacy monkeypatches working)
    """
    # An explicit wrapper class (legacy test monkeypatch) pins the emergent path.
    be = "emergent" if emergent_chat_cls is not None else backend()
    model = model or llm_models.model_for(feature)
    if disabled():
        return LLMResult(ok=False, error="LLM_DISABLED", error_kind="disabled",
                         model=model, backend=be)
    breaker_key = f"{feature}:{model}"
    if _breaker_open(breaker_key):
        return LLMResult(ok=False, error="circuit open", error_kind="circuit_open",
                         model=model, backend=be)

    timeout_s = float(timeout_s or llm_models.timeout_for(feature))
    if effort is None:
        effort = llm_models.effort_for(feature, model)
    elif not llm_models.supports_effort(model):
        effort = None
    if untrusted:
        system = f"{system}\n\n{UNTRUSTED_PREAMBLE}"
        user = f"{user}\n\n{untrusted_block(untrusted)}"
    messages = _clean_history(history) + [{"role": "user", "content": user}]

    async def _run() -> LLMResult:
        async with _semaphore(feature):
            if be == "anthropic":
                return await _call_anthropic(
                    feature=feature, model=model, system=system, messages=messages,
                    schema=schema, max_tokens=max_tokens, effort=effort,
                    timeout_s=timeout_s, cache_system=cache_system)
            return await _call_emergent(
                feature=feature, model=model, system=system, messages=messages,
                schema=schema, timeout_s=timeout_s,
                chat_cls=emergent_chat_cls, message_cls=emergent_message_cls)

    try:
        res = await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        res = LLMResult(ok=False, error=f"timed out after {timeout_s:.0f}s",
                        error_kind="timeout")
    except Exception as e:  # noqa: BLE001 — the contract is: never raise
        res = LLMResult(ok=False, error=f"{type(e).__name__}: {e}", error_kind="api_error")
    res.backend = be
    res.model = res.model or model

    _breaker_record(breaker_key, transport_failure=res.error_kind in TRANSPORT_KINDS)
    if not res.ok:
        logger.warning("LLM %s failed (%s/%s): %s", feature, be, res.error_kind, res.error)

    if res.usage:
        res.cost_usd = llm_models.estimate_cost_usd(
            res.model, res.usage.get("input_tokens", 0), res.usage.get("output_tokens", 0),
            cache_write_tokens=res.usage.get("cache_creation_input_tokens", 0),
            cache_read_tokens=res.usage.get("cache_read_input_tokens", 0))
        _record_usage({
            "feature": feature, "model": res.model, "backend": be,
            "ok": res.ok, "stop_reason": res.stop_reason, "error_kind": res.error_kind,
            **res.usage, "estimated_cost_usd": res.cost_usd,
            "at": datetime.now(timezone.utc), **(usage_meta or {})})
    return res
