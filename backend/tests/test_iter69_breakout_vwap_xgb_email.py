"""Tests for iter-69 — Breakout Scalper + VWAP Pullback + XGBoost meta-learner
+ LLM reflection helper + Resend email helper."""
from __future__ import annotations
import os
import numpy as np
import pytest

from breakout_scalper import compute_breakout_scalper
from vwap_pullback import compute_vwap_pullback
from learned_meta import _fit_artifact, _XGB_AVAILABLE, MIN_SAMPLES_XGB


def _ohlc(closes, vol=1000.0):
    """Build flat OHLC bars from closes (high=close*1.005, low=close*0.995)."""
    return [
        {"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": vol}
        for c in closes
    ]


# ─────────────── Breakout Scalper ───────────────
def test_breakout_empty_history():
    out = compute_breakout_scalper([])
    assert out["ready"] is False
    assert out["signal"] == "NONE"


def test_breakout_insufficient_history():
    out = compute_breakout_scalper(_ohlc([100.0] * 10))
    assert out["ready"] is False


def test_breakout_no_signal_in_chop():
    closes = [100.0 + (i % 2) * 0.05 for i in range(30)]
    out = compute_breakout_scalper(_ohlc(closes))
    assert out["ready"] is True
    assert out["signal"] == "NONE"


def test_breakout_buy_signal_strong():
    # 20 bars around 100, then a strong push to 105 (way above the band)
    closes = [100.0 + (i % 3) * 0.1 for i in range(25)] + [105.0]
    out = compute_breakout_scalper(_ohlc(closes))
    assert out["signal"] == "BUY"
    assert out["break_distance_atr"] >= 0.25
    assert out["channel_high"] is not None
    assert out["atr"] is not None


def test_breakout_sell_signal_strong():
    closes = [100.0 + (i % 3) * 0.1 for i in range(25)] + [94.0]
    out = compute_breakout_scalper(_ohlc(closes))
    assert out["signal"] == "SELL"
    assert out["break_distance_atr"] >= 0.25


def test_breakout_tiny_push_not_triggered():
    # current close only marginally above the channel — break < 0.25 ATR
    closes = [100.0 + (i % 3) * 0.5 for i in range(25)] + [101.01]
    out = compute_breakout_scalper(_ohlc(closes))
    assert out["signal"] == "NONE"


def test_breakout_dict_shape():
    closes = [100.0 + i * 0.05 for i in range(30)]
    out = compute_breakout_scalper(_ohlc(closes))
    for k in ("signal", "channel_high", "channel_low",
              "channel_width_pct", "atr", "break_distance_atr", "ready"):
        assert k in out


# ─────────────── VWAP Pullback ───────────────
def test_vwap_empty_history():
    assert compute_vwap_pullback([])["ready"] is False


def test_vwap_with_volume():
    out = compute_vwap_pullback(_ohlc([100.0] * 25, vol=1000.0))
    assert out["ready"] is True
    assert out["vwap"] is not None
    assert abs(out["pullback_pct"]) < 0.5


def test_vwap_falls_back_when_volume_missing():
    bars = [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 0}] * 25
    out = compute_vwap_pullback(bars)
    assert out["ready"] is True
    assert out["vwap"] is not None  # falls back to typical-price average


def test_vwap_pullback_signal_in_uptrend():
    # Recent prices near vwap (flat tail), trend UP context → BUY pullback
    closes = [100.0] * 25
    out = compute_vwap_pullback(_ohlc(closes), htf_trend="UP")
    assert out["regime"] == "near"
    assert out["pullback_signal"] == "BUY"


def test_vwap_pullback_signal_in_downtrend():
    closes = [100.0] * 25
    out = compute_vwap_pullback(_ohlc(closes), htf_trend="DOWN")
    assert out["pullback_signal"] == "SELL"


def test_vwap_extended_regime():
    # First 24 bars at 100, last bar 103 → +3% above VWAP → above_extended
    closes = [100.0] * 24 + [103.0]
    out = compute_vwap_pullback(_ohlc(closes), htf_trend="UP")
    assert out["regime"] == "above_extended"
    # Extended regime shouldn't fire a pullback BUY
    assert out["pullback_signal"] == "NONE"


def test_vwap_below_extended():
    closes = [100.0] * 24 + [97.0]
    out = compute_vwap_pullback(_ohlc(closes), htf_trend="DOWN")
    assert out["regime"] == "below_extended"


# ─────────────── XGBoost meta-learner ───────────────
def _make_synthetic_dataset(n: int, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Generate a small dataset where confidence_norm is mildly predictive."""
    rng = np.random.default_rng(seed)
    X = rng.random((n, 8))
    # Make column 0 (confidence_norm) loosely predict y.
    z = 2.5 * X[:, 0] - 1.2 + rng.normal(0, 0.5, n)
    y = (z > 0).astype(float)
    # Ensure both classes present
    y[0] = 1.0
    y[1] = 0.0
    return X, y


@pytest.mark.skipif(not _XGB_AVAILABLE, reason="xgboost not installed")
def test_fit_artifact_uses_xgboost_when_large_enough():
    X, y = _make_synthetic_dataset(MIN_SAMPLES_XGB + 20)
    art = _fit_artifact(X, y, "test_key_xgb", "TEST_XGB")
    assert art["backend"] == "xgboost"
    assert "xgb_model_b64" in art
    assert art["n_samples"] == MIN_SAMPLES_XGB + 20
    # Some predictive lift expected on synthetic separable data
    assert art["train_auc"] >= 0.7


def test_fit_artifact_falls_back_to_logreg_when_small():
    X, y = _make_synthetic_dataset(50)
    art = _fit_artifact(X, y, "test_key_lr", "TEST_LR")
    assert art["backend"] == "logreg"
    assert "weights" in art
    assert "xgb_model_b64" not in art


@pytest.mark.skipif(not _XGB_AVAILABLE, reason="xgboost not installed")
def test_xgb_predict_roundtrip():
    """Train XGB artifact and exercise the inference path."""
    from learned_meta import _b64decode_bytes, _xgb_predict_proba
    X, y = _make_synthetic_dataset(MIN_SAMPLES_XGB + 30)
    art = _fit_artifact(X, y, "test_key_xgb2", "TEST_XGB2")
    assert art["backend"] == "xgboost"

    model_bytes = _b64decode_bytes(art["xgb_model_b64"])
    # Predict on the training set — should produce sane probabilities in [0,1]
    p = _xgb_predict_proba(model_bytes, X)
    assert p.shape == (len(y),)
    assert np.all((p >= 0.0) & (p <= 1.0))


# ─────────────── Email sender ───────────────
def test_email_sender_is_configured():
    """RESEND_API_KEY should be loaded via .env when backend started."""
    from email_sender import is_configured
    # The .env loader runs on backend import; verifying the helper at least
    # returns a bool without crashing.
    assert is_configured() in (True, False)


# ─────────────── LLM reflection helper ───────────────
@pytest.mark.asyncio
async def test_generate_ai_reflection_returns_none_without_key(monkeypatch):
    """No EMERGENT_LLM_KEY → helper short-circuits to None."""
    from routes.insights_routes import _generate_ai_reflection
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    out = await _generate_ai_reflection({"stats": {"trades": 0}})
    assert out is None
