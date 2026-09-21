"""iter-62 · Forecast Agent — Chronos-Bolt zero-shot probabilistic forecasts.

Pretrained transformer (no training on our thin data). Produces quantile
bands over the next horizon from M15 candles (EA v1.42 feed) or daily bars
as fallback. Gate blocks a trade only when the ENTIRE 80% forecast interval
moves against it. Fail-open everywhere: missing model/data → no forecast,
no veto. Inference runs in a thread executor, cached 5 min per symbol."""
import asyncio
import logging
import time

from pip_utils import base_symbol

logger = logging.getLogger(__name__)

MODEL_NAME = "amazon/chronos-bolt-tiny"
QUANTILE_LEVELS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
CACHE_TTL = 300
HORIZON_M15 = 8      # 2 hours ahead
HORIZON_DAILY = 3    # 3 days ahead
MIN_CONTEXT = 30

_model = None
_model_failed = False
_cache: dict = {}    # base_symbol -> (expires, payload)
_lock = asyncio.Lock()

# Runtime telemetry for the Bot Health "Forecast" card. Persisted per process
# (API and each worker load their own model) in db.forecast_runtime so the
# API can report what worker-trading is actually doing.
_stats: dict = {"model_loaded": False, "model_failed": False,
                "load_ms": None, "loaded_at": None, "last_error": None,
                "forecasts_total": 0, "inference_errors": 0,
                "last_forecast_at": None, "last_latency_ms": None,
                "last_symbol": None, "last_source": None,
                "cache_hits": 0, "cache_misses": 0}


def _process_role() -> str:
    import os
    return os.environ.get("STOIC_PROCESS_ROLE") or "api"


def runtime_status() -> dict:
    import os
    import socket
    from ml_runtime import _memory_budget_gb, ml_runtime_enabled
    budget = _memory_budget_gb()
    return {**_stats, "model": MODEL_NAME, "role": _process_role(),
            "holder": f"{socket.gethostname()}:{os.getpid()}",
            "cache_entries": len(_cache), "cache_ttl_s": CACHE_TTL,
            "agent_enabled": os.environ.get("FORECAST_AGENT_ENABLED",
                                            "true").lower() == "true",
            "ml_runtime_enabled": ml_runtime_enabled(),
            "memory_budget_gb": None if budget == float("inf")
            else round(budget, 1),
            "torch_available": _torch_available(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                        time.gmtime())}


def _torch_available() -> bool:
    import importlib.util
    return (importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("chronos") is not None)


async def _persist_status(db) -> None:
    try:
        st = runtime_status()
        await db.forecast_runtime.update_one(
            {"_id": st["role"]}, {"$set": st}, upsert=True)
    except Exception as e:  # noqa: BLE001 — telemetry never breaks trading
        logger.debug("forecast runtime persist failed: %s", e)


def _load_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    from ml_runtime import ml_runtime_enabled
    if not ml_runtime_enabled():
        logger.info("Chronos forecast model skipped — heavy ML disabled "
                    "(container memory budget)")
        _model_failed = True
        _stats["last_error"] = "heavy ML disabled (memory gate)"
        return None
    try:
        import torch
        from chronos import BaseChronosPipeline
        t0 = time.time()
        _model = BaseChronosPipeline.from_pretrained(
            MODEL_NAME, device_map="cpu", torch_dtype=torch.float32)
        _stats.update({"model_loaded": True, "model_failed": False,
                       "load_ms": round((time.time() - t0) * 1000),
                       "loaded_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                  time.gmtime()),
                       "last_error": None})
        logger.info("Chronos forecast model loaded: %s", MODEL_NAME)
    except Exception as e:  # noqa: BLE001
        logger.warning("Chronos model unavailable (%s) — forecast agent disabled", e)
        _model_failed = True
        _stats.update({"model_failed": True,
                       "last_error": f"{type(e).__name__}: {str(e)[:200]}"})
    return _model


def _forecast_sync(closes: list, horizon: int):
    pipe = _load_model()
    if pipe is None:
        return None
    import torch  # after the gate: never import torch when ML is disabled
    ctx = torch.tensor(closes[-512:], dtype=torch.float32)
    quantiles, _ = pipe.predict_quantiles(
        inputs=ctx, prediction_length=horizon, quantile_levels=QUANTILE_LEVELS)
    return quantiles[0].tolist()   # [horizon][len(QUANTILE_LEVELS)]


def summarize(closes: list, q: list, source: str, horizon: int,
              symbol: str = "XAUUSD") -> dict:
    last = float(closes[-1])
    vals = [float(v) for v in q[-1]]
    q10, q50, q90 = vals[0], vals[len(vals) // 2], vals[-1]
    out = {
        "model": MODEL_NAME, "source": source, "horizon": horizon,
        "last": round(last, 5),
        "q10": round(q10, 5), "q50": round(q50, 5), "q90": round(q90, 5),
        "median_change_pct": round((q50 - last) / last * 100, 3),
        "band_low_pct": round((q10 - last) / last * 100, 3),
        "band_high_pct": round((q90 - last) / last * 100, 3),
        "quantile_levels": QUANTILE_LEVELS[:len(vals)],
        "quantile_values": [round(v, 5) for v in vals],
    }
    try:
        from prob_forecast import scenario_table
        out["distribution"] = scenario_table(
            symbol, last, out["quantile_levels"], vals)
    except Exception as e:  # noqa: BLE001
        logger.debug("scenario table failed: %s", e)
    return out


async def get_forecast(db, user_id: str, symbol: str) -> dict | None:
    # Deployment kill-switch — set FORECAST_AGENT_ENABLED=false on
    # memory-constrained pods to skip loading the Chronos/torch model
    # entirely (fail-open: no forecast, no veto). Default enabled.
    import os
    if os.environ.get("FORECAST_AGENT_ENABLED", "true").lower() != "true":
        return None
    base = base_symbol(symbol)
    now = time.time()
    hit = _cache.get(base)
    if hit and hit[0] > now:
        _stats["cache_hits"] += 1
        return hit[1]
    async with _lock:
        hit = _cache.get(base)
        if hit and hit[0] > now:
            _stats["cache_hits"] += 1
            return hit[1]
        _stats["cache_misses"] += 1
        closes, source, horizon = None, None, None
        cdoc = await db.intraday_candles.find_one({"user_id": user_id, "symbol": base})
        bars = (cdoc or {}).get("bars") or []
        if len(bars) >= MIN_CONTEXT:
            closes = [float(b["c"]) for b in bars]
            source, horizon = "M15", HORIZON_M15
        else:
            try:
                from market import get_history
                hist = await get_history(symbol)
                if hist and len(hist) >= MIN_CONTEXT:
                    closes = [float(b["close"]) for b in hist if b.get("close")]
                    source, horizon = "daily", HORIZON_DAILY
            except Exception as e:  # noqa: BLE001
                logger.debug("forecast history fallback failed: %s", e)
        payload = None
        if closes:
            try:
                loop = asyncio.get_running_loop()
                t0 = time.time()
                q = await loop.run_in_executor(None, _forecast_sync, closes, horizon)
                if q:
                    payload = summarize(closes, q, source, horizon, base)
                    _stats.update({
                        "forecasts_total": _stats["forecasts_total"] + 1,
                        "last_forecast_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                        "last_latency_ms": round((time.time() - t0) * 1000),
                        "last_symbol": base, "last_source": source})
            except Exception as e:  # noqa: BLE001
                logger.warning("forecast inference failed: %s", e)
                _stats["inference_errors"] += 1
                _stats["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        _cache[base] = (now + CACHE_TTL, payload)
        await _persist_status(db)
        return payload


def forecast_gate(action: str, fc: dict | None) -> str | None:
    """Veto only when the entire 80% interval moves against the trade."""
    if action not in ("BUY", "SELL") or not fc:
        return None
    if action == "BUY" and fc["q90"] < fc["last"]:
        return (f"Forecast gate: even the 90th-percentile path is below current "
                f"price ({fc['band_high_pct']:+.2f}% .. {fc['band_low_pct']:+.2f}% "
                f"over next {fc['horizon']} {fc['source']} steps) — BUY fights the "
                f"entire forecast band. Vetoed.")
    if action == "SELL" and fc["q10"] > fc["last"]:
        return (f"Forecast gate: even the 10th-percentile path is above current "
                f"price ({fc['band_low_pct']:+.2f}% .. {fc['band_high_pct']:+.2f}% "
                f"over next {fc['horizon']} {fc['source']} steps) — SELL fights the "
                f"entire forecast band. Vetoed.")
    return None
