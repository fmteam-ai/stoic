"""Fix plan A2 — every LLM call made from the trading loop runs under a hard timeout
(AI_CALL_TIMEOUT_SEC, default 10 s). A slow model raises asyncio.TimeoutError into the
caller's existing fallback path instead of stalling the cycle.

AI latency card — every call is also recorded (provider, model, label group, duration,
outcome) in `ai_latency_samples` (TTL 7 days) so Bot Health can show p50/p95, timeout and
error rates per provider and flag slow providers before they cost signals.
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SEC = 10.0
SAMPLE_TTL_DAYS = 7
SLOW_P95_RATIO = 0.6          # p95 above 60 % of the timeout → "slow"
_indexed = False


def ai_timeout_sec() -> float:
    try:
        return max(2.0, float(os.environ.get("AI_CALL_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


async def _record(chat, label: str, ms: float, outcome: str, timeout_s: float) -> None:
    global _indexed
    try:
        from database import get_db
        db = get_db()
        if db is None:
            return
        now = datetime.now(timezone.utc)
        if not _indexed:
            _indexed = True
            await db.ai_latency_samples.create_index("expires_at", expireAfterSeconds=0)
            await db.ai_latency_samples.create_index([("at", -1)])
        await db.ai_latency_samples.insert_one({
            "provider": str(getattr(chat, "provider", None) or "unknown"),
            "model": str(getattr(chat, "model", None) or "unknown"),
            "label": str(label or "llm").split(":")[0],
            "ms": round(float(ms), 1), "outcome": outcome, "timeout_s": timeout_s,
            "at": now.isoformat(), "expires_at": now + timedelta(days=SAMPLE_TTL_DAYS)})
    except Exception as e:  # noqa: BLE001 — telemetry must never break the trading loop
        logger.debug("ai latency sample not recorded: %s", type(e).__name__)


async def send_with_timeout(chat, message, *, label: str = "llm", seconds: float | None = None):
    secs = seconds or ai_timeout_sec()
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(chat.send_message(message), timeout=secs)
    except asyncio.TimeoutError:
        logger.warning("AI call %s timed out after %.0fs — using fallback", label, secs)
        await _record(chat, label, (time.monotonic() - t0) * 1000, "timeout", secs)
        raise
    except Exception:
        await _record(chat, label, (time.monotonic() - t0) * 1000, "error", secs)
        raise
    await _record(chat, label, (time.monotonic() - t0) * 1000, "ok", secs)
    return out


def _pct(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return float(sorted_vals[i])


async def latency_summary(db, hours: int = 24) -> dict:
    """Per provider/model/label: calls, ok/timeout/error counts, p50/p95/max ms, verdict."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = await db.ai_latency_samples.find({"at": {"$gte": since}}, {"_id": 0}).sort("at", -1).limit(20000).to_list(length=20000)
    timeout_ms = ai_timeout_sec() * 1000
    groups: dict = {}
    for r in rows:
        key = (r.get("provider"), r.get("model"))
        g = groups.setdefault(key, {"provider": key[0], "model": key[1], "calls": 0, "ok": 0, "timeout": 0, "error": 0,
                                    "ms": [], "labels": {}, "last_at": r.get("at")})
        g["calls"] += 1
        g[r.get("outcome") if r.get("outcome") in ("ok", "timeout", "error") else "error"] += 1
        if r.get("outcome") == "ok":
            g["ms"].append(float(r.get("ms") or 0))
        g["labels"][r.get("label") or "llm"] = g["labels"].get(r.get("label") or "llm", 0) + 1
        g["last_at"] = max(g["last_at"] or "", r.get("at") or "")
    out = []
    for g in groups.values():
        ms = sorted(g.pop("ms"))
        p50, p95, mx = _pct(ms, 50), _pct(ms, 95), (ms[-1] if ms else 0.0)
        fail_rate = (g["timeout"] + g["error"]) / g["calls"] if g["calls"] else 0.0
        if g["calls"] >= 3 and (fail_rate >= 0.2 or g["timeout"] >= 3):
            verdict, why = "failing", f"{g['timeout']} timeouts / {g['error']} errors in {g['calls']} calls"
        elif g["calls"] >= 3 and p95 >= SLOW_P95_RATIO * timeout_ms:
            verdict, why = "slow", f"p95 {p95 / 1000:.1f}s is {p95 / timeout_ms:.0%} of the {timeout_ms / 1000:.0f}s timeout"
        elif g["calls"] < 3:
            verdict, why = "insufficient_data", f"{g['calls']} call(s) in {hours} h"
        else:
            verdict, why = "healthy", f"p95 {p95 / 1000:.1f}s · {fail_rate:.0%} failures"
        g.update({"p50_ms": round(p50), "p95_ms": round(p95), "max_ms": round(mx), "fail_rate": round(fail_rate, 3),
                  "verdict": verdict, "why": why})
        out.append(g)
    out.sort(key=lambda g: ({"failing": 0, "slow": 1, "healthy": 2, "insufficient_data": 3}[g["verdict"]], -g["calls"]))
    worst = out[0]["verdict"] if out else "no_data"
    return {"hours": hours, "timeout_s": ai_timeout_sec(), "slow_p95_ratio": SLOW_P95_RATIO, "samples": len(rows),
            "providers": out, "overall": worst, "built_at": datetime.now(timezone.utc).isoformat()}
