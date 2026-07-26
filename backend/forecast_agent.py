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


def _load_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    try:
        import torch
        from chronos import BaseChronosPipeline
        _model = BaseChronosPipeline.from_pretrained(
            MODEL_NAME, device_map="cpu", torch_dtype=torch.float32)
        logger.info("Chronos forecast model loaded: %s", MODEL_NAME)
    except Exception as e:  # noqa: BLE001
        logger.warning("Chronos model unavailable (%s) — forecast agent disabled", e)
        _model_failed = True
    return _model


def _forecast_sync(closes: list, horizon: int):
    import torch
    pipe = _load_model()
    if pipe is None:
        return None
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
        return hit[1]
    async with _lock:
        hit = _cache.get(base)
        if hit and hit[0] > now:
            return hit[1]
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
                q = await loop.run_in_executor(None, _forecast_sync, closes, horizon)
                if q:
                    payload = summarize(closes, q, source, horizon, base)
            except Exception as e:  # noqa: BLE001
                logger.warning("forecast inference failed: %s", e)
        _cache[base] = (now + CACHE_TTL, payload)
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
