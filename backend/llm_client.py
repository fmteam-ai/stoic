"""v1.60.59 — the ONE LLM entry point. Every AI feature calls `complete()`; nothing else builds a chat client.

Provider selection (per call, no restart):
  ANTHROPIC_API_KEY set  → direct Anthropic API (official `anthropic` SDK)           provider "anthropic_direct"
  else EMERGENT_LLM_KEY  → emergentintegrations LlmChat (Emergent Universal Key)      provider "emergent"
  else                   → LlmUnavailable (callers keep their existing fail-closed paths)

Model names come from llm_models.model_for() only. Every call runs under the llm_timeout budget, is sampled for the
AI-latency card, counted per feature/day for Bot Health and capped by a per-tier max_tokens. Keys are never logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from llm_models import PROVIDER, TIERS, model_for

logger = logging.getLogger(__name__)

PROVIDER_DIRECT = "anthropic_direct"
PROVIDER_EMERGENT = "emergent"
PROVIDER_NONE = "none"

# cost control — hard output caps per tier (callers may only lower them)
MAX_TOKENS: dict[str, int] = {"fast": 1024, "analysis": 2048, "deep": 8192, "ai_optimizer": 4096, "ai_optimizer_fallback": 4096}
DEFAULT_MAX_TOKENS = 2048
DAILY_CALL_CAP = int(os.environ.get("LLM_DAILY_CALL_CAP") or 2000)   # all features together, per UTC day

# error kinds (Bot Health wording)
KIND_BLOCKED = "universal_key_blocked_offhost"
KIND_AUTH = "auth"
KIND_MODEL = "model_not_found"
KIND_TIMEOUT = "timeout"
KIND_RATE = "rate_limited"
KIND_CAP = "daily_cap"
KIND_OTHER = "error"

_status: dict = {"last_error": None, "last_error_kind": None, "last_error_at": None, "last_error_provider": None,
                 "last_ok_at": None, "last_ok_provider": None}
_counters: dict[tuple[str, str], dict] = {}     # (day, feature) → {calls, ok, errors, provider}
_cap_notified_day: str | None = None


class LlmUnavailable(RuntimeError):
    """No usable LLM credential / host blocked / daily cap — callers treat it like any other LLM failure."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _key(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def active_provider() -> str:
    if _key("ANTHROPIC_API_KEY"):
        return PROVIDER_DIRECT
    if _key("EMERGENT_LLM_KEY"):
        return PROVIDER_EMERGENT
    return PROVIDER_NONE


def configured() -> bool:
    return active_provider() != PROVIDER_NONE


def redact(text: str) -> str:
    """Strip any configured key (and sk-… shaped tokens) from a message before it is logged or returned."""
    out = str(text or "")
    for name in ("ANTHROPIC_API_KEY", "EMERGENT_LLM_KEY"):
        k = _key(name)
        if k and k in out:
            out = out.replace(k, "***")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", out)


def classify_error(exc: BaseException, provider: str) -> tuple[str, str]:
    """(kind, human message) — a 403 from the Emergent proxy means the host is not allowed, not a bad key."""
    if isinstance(exc, LlmUnavailable):
        return exc.kind, str(exc)
    if isinstance(exc, asyncio.TimeoutError):
        return KIND_TIMEOUT, "LLM call timed out"
    name = type(exc).__name__
    s = redact(f"{name}: {exc}")
    low = s.lower()
    status = getattr(exc, "status_code", None)
    if provider == PROVIDER_EMERGENT and (status == 403 or "403" in low or "forbidden" in low):
        return KIND_BLOCKED, ("Universal Key not allowed from this host — Emergent proxy answered 403 Forbidden "
                              "(off-platform access is refused before the key is checked). Set ANTHROPIC_API_KEY "
                              "under Admin → Integrations → AI to use Claude directly.")
    if status in (401, 403) or "authentication" in low or "invalid x-api-key" in low or "permissiondenied" in low:
        return KIND_AUTH, f"LLM credential rejected ({name})"
    if status == 404 or "notfound" in low or "model_not_found" in low or "not_found_error" in low:
        return KIND_MODEL, f"model not available on {provider}: {s[:160]}"
    if status == 429 or "ratelimit" in low or "rate limit" in low or "overloaded" in low:
        return KIND_RATE, f"LLM rate-limited / overloaded ({name})"
    return KIND_OTHER, s[:300]


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _count(feature: str, provider: str, ok: bool) -> None:
    c = _counters.setdefault((_day(), feature), {"calls": 0, "ok": 0, "errors": 0, "provider": provider})
    c["calls"] += 1
    c["ok" if ok else "errors"] += 1
    c["provider"] = provider
    try:
        from database import get_db
        db = get_db()
        if db is not None:
            async def _w():
                await db.llm_call_counters.update_one(
                    {"day": _day(), "feature": feature},
                    {"$inc": {"calls": 1, ("ok" if ok else "errors"): 1}, "$set": {"provider": provider, "at": datetime.now(timezone.utc).isoformat()}},
                    upsert=True)
            asyncio.get_running_loop().create_task(asyncio.wait_for(_w(), timeout=2.0))
    except Exception:  # noqa: BLE001 — telemetry never breaks a feature
        pass


def _today_calls() -> int:
    d = _day()
    return sum(c["calls"] for (day, _f), c in _counters.items() if day == d)


def _note_error(provider: str, kind: str, msg: str) -> None:
    _status.update({"last_error": msg, "last_error_kind": kind, "last_error_at": datetime.now(timezone.utc).isoformat(),
                    "last_error_provider": provider})


def _history_messages(history: list | None) -> list[dict]:
    """[{role: user|assistant, content: str}] — anything else is dropped; a leading assistant turn is skipped."""
    out = []
    for m in history or []:
        role, content = (m or {}).get("role"), str((m or {}).get("content") or "").strip()
        if role in ("user", "assistant") and content:
            if not out and role == "assistant":
                continue
            if out and out[-1]["role"] == role:        # Anthropic requires alternating roles — merge same-role turns
                out[-1]["content"] += "\n\n" + content
                continue
            out.append({"role": role, "content": content})
    return out


class _Shim:
    """What llm_timeout._record_bg reads for the AI-latency card."""

    def __init__(self, provider: str, model: str):
        self.provider, self.model = provider, model


async def _direct(model: str, system: str, user_text: str, history: list[dict], max_tokens: int) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=_key("ANTHROPIC_API_KEY"), max_retries=1)
    try:
        resp = await client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                            messages=history + [{"role": "user", "content": user_text}])
    finally:
        await client.close()
    return "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", "") == "text")


async def _emergent(model: str, provider: str, system: str, user_text: str, history: list[dict], max_tokens: int,
                    feature: str) -> str:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
    initial = [{"role": "system", "content": system}] + history
    chat = LlmChat(api_key=_key("EMERGENT_LLM_KEY"), session_id=f"{feature}-{uuid.uuid4().hex[:10]}",
                   system_message=system, initial_messages=initial).with_model(provider, model).with_params(max_tokens=max_tokens)
    return str(await chat.send_message(UserMessage(text=user_text)))


async def complete(tier: str, system: str, user_text: str, *, feature: str, history: list | None = None,
                   seconds: float | None = None, max_tokens: int | None = None, model: str | None = None,
                   provider: str | None = None) -> str:
    """One LLM turn → text. Raises (LlmUnavailable, asyncio.TimeoutError, provider errors) — callers fail closed."""
    from llm_timeout import _record_bg, ai_timeout_sec
    if tier not in TIERS:
        raise KeyError(f"unknown LLM tier {tier!r}")
    model = model or model_for(tier)
    cap = MAX_TOKENS.get(tier, DEFAULT_MAX_TOKENS)
    max_tokens = min(int(max_tokens), cap) if max_tokens else cap
    active = active_provider()
    if active == PROVIDER_NONE:
        raise LlmUnavailable(KIND_AUTH, "no LLM credential configured (ANTHROPIC_API_KEY or EMERGENT_LLM_KEY)")
    if _today_calls() >= DAILY_CALL_CAP:
        global _cap_notified_day
        if _cap_notified_day != _day():
            _cap_notified_day = _day()
            logger.warning("LLM daily call cap %s reached — AI features fail closed until 00:00 UTC", DAILY_CALL_CAP)
        _note_error(active, KIND_CAP, f"daily LLM call cap ({DAILY_CALL_CAP}) reached")
        raise LlmUnavailable(KIND_CAP, f"daily LLM call cap ({DAILY_CALL_CAP}) reached")
    hist = _history_messages(history)
    secs = seconds or ai_timeout_sec()
    shim = _Shim(active, model)
    t0 = time.monotonic()
    try:
        if active == PROVIDER_DIRECT:
            text = await asyncio.wait_for(_direct(model, system, user_text, hist, max_tokens), timeout=secs)
        else:
            text = await asyncio.wait_for(_emergent(model, provider or PROVIDER, system, user_text, hist, max_tokens, feature), timeout=secs)
    except asyncio.TimeoutError:
        _record_bg(shim, feature, (time.monotonic() - t0) * 1000, "timeout", secs)
        _count(feature, active, False)
        _note_error(active, KIND_TIMEOUT, f"{feature}: timed out after {secs:.0f}s")
        logger.warning("AI call %s timed out after %.0fs — using fallback", feature, secs)
        raise
    except Exception as e:  # noqa: BLE001 — classify, record, re-raise for the caller's fallback
        _record_bg(shim, feature, (time.monotonic() - t0) * 1000, "error", secs)
        _count(feature, active, False)
        kind, msg = classify_error(e, active)
        _note_error(active, kind, f"{feature}: {msg}")
        logger.warning("AI call %s failed on %s (%s): %s", feature, active, kind, redact(msg)[:200])
        raise
    _record_bg(shim, feature, (time.monotonic() - t0) * 1000, "ok", secs)
    _count(feature, active, True)
    _status.update({"last_ok_at": datetime.now(timezone.utc).isoformat(), "last_ok_provider": active})
    return (text or "").strip()


def status() -> dict:
    """Bot Health / Integrations: active provider, last error (redacted) and today's per-feature counters. No keys."""
    d = _day()
    feats = {f: {k: v for k, v in c.items()} for (day, f), c in sorted(_counters.items()) if day == d}
    return {"provider": active_provider(), "model_provider": PROVIDER, "models": {t: model_for(t) for t in TIERS},
            "max_tokens": dict(MAX_TOKENS), "daily_call_cap": DAILY_CALL_CAP, "today_calls": _today_calls(),
            "features_today": feats, "direct_key_configured": bool(_key("ANTHROPIC_API_KEY")),
            "emergent_key_configured": bool(_key("EMERGENT_LLM_KEY")),
            **{k: (redact(v) if isinstance(v, str) else v) for k, v in _status.items()}}


async def status_with_db(db) -> dict:
    """status() + today's counters from Mongo (survive restarts / other workers)."""
    out = status()
    try:
        rows = await db.llm_call_counters.find({"day": _day()}, {"_id": 0}).to_list(length=200)
        for r in rows:
            f = r.get("feature")
            cur = out["features_today"].setdefault(f, {"calls": 0, "ok": 0, "errors": 0, "provider": r.get("provider")})
            cur.update({"calls": max(cur["calls"], int(r.get("calls") or 0)), "ok": max(cur["ok"], int(r.get("ok") or 0)),
                        "errors": max(cur["errors"], int(r.get("errors") or 0))})
        out["today_calls"] = sum(c["calls"] for c in out["features_today"].values())
    except Exception:  # noqa: BLE001
        pass
    return out
