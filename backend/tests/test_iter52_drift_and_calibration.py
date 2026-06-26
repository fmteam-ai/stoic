"""Tests for iter-52 — ADWIN drift detection + Platt calibration.

Covers:
  • probability_calibrator: fit_platt skips on tiny data, fits on enough,
    apply_platt is a no-op when calib is None/skipped
  • probability_calibrator: brier improves (or stays) after calibration on
    a synthetic mis-calibrated dataset
  • drift_detector: residual recording shape + idempotency on missing trade
  • drift_detector: ADWIN flags drift on a synthetic regime shift
  • drift_detector: cooldown prevents back-to-back retrains
  • learned_meta: artifact carries calibration block; predict_p_win returns
    both raw and calibrated probabilities
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock

from probability_calibrator import (
    fit_platt, apply_platt, brier_score, _logit, _sigmoid,
)
from drift_detector import (
    record_residual, _check_session, maybe_trigger_retrain,
    MIN_RESIDUALS_PER_SESSION,
)


# =========================== probability_calibrator ===========================
def test_platt_skipped_on_small_n():
    out = fit_platt(np.array([0.5, 0.6]), np.array([1, 0]))
    assert out["skipped"] is True
    assert out["A"] == 1.0 and out["B"] == 0.0


def test_platt_fits_when_enough_data():
    np.random.seed(0)
    # Synthetic: raw scores in [0.1, 0.9], labels follow a noisier rule
    scores = np.random.uniform(0.1, 0.9, 50)
    labels = (scores > 0.55).astype(int)
    out = fit_platt(scores, labels)
    assert out["skipped"] is False
    assert "A" in out and "B" in out


def test_apply_platt_is_noop_when_skipped():
    calib = {"skipped": True, "A": 1.0, "B": 0.0}
    assert apply_platt(0.42, calib) == pytest.approx(0.42)
    assert apply_platt(0.7, None) == pytest.approx(0.7)


def test_apply_platt_maps_through_logistic():
    # A=2, B=0 → strong rescaling. logit(0.5)=0 → output should be 0.5.
    calib = {"skipped": False, "A": 2.0, "B": 0.0}
    out = apply_platt(0.5, calib)
    assert out == pytest.approx(0.5)


def test_brier_score_basic():
    assert brier_score(np.array([0.5, 0.5]), np.array([1, 0])) == pytest.approx(0.25)
    assert brier_score(np.array([1.0, 0.0]), np.array([1, 0])) == 0.0


def test_platt_improves_brier_on_miscalibrated_data():
    """Synthetic over-confident classifier — Brier should drop after Platt."""
    np.random.seed(1)
    # 100 samples, true labels Bernoulli(0.5)
    n = 100
    labels = np.random.randint(0, 2, n).astype(float)
    # Make scores systematically over-confident (push closer to 0 / 1).
    raw = labels * 0.85 + (1 - labels) * 0.15
    raw = np.clip(raw + np.random.normal(0, 0.05, n), 0.01, 0.99)
    # Make the raw probabilities systematically wrong (push too extreme)
    raw_overconf = _sigmoid(2.0 * _logit(raw))

    calib = fit_platt(raw_overconf, labels)
    p_cal = np.array([apply_platt(float(p), calib) for p in raw_overconf])
    brier_raw = brier_score(raw_overconf, labels)
    brier_cal = brier_score(p_cal, labels)
    # Calibrated Brier should be ≤ raw (with a small slack for GD convergence)
    assert brier_cal <= brier_raw + 0.01


# =========================== drift_detector ===========================
@pytest.mark.asyncio
async def test_record_residual_persists_correct_shape(monkeypatch):
    monkeypatch.setenv("DRIFT_DETECTION_ENABLED", "true")
    # Re-import module to pick up env? No — DRIFT_ENABLED is read at import.
    # Patch the module-level flag directly.
    import drift_detector
    monkeypatch.setattr(drift_detector, "DRIFT_ENABLED", True)

    db = MagicMock()
    db.learned_meta_residuals.insert_one = AsyncMock()
    await record_residual(db, trade_id="t1", session="asia",
                          p_predicted=0.7, outcome=0)
    db.learned_meta_residuals.insert_one.assert_awaited_once()
    call_args = db.learned_meta_residuals.insert_one.await_args[0][0]
    assert call_args["session"] == "ASIA"
    assert call_args["p_predicted"] == 0.7
    assert call_args["outcome"] == 0
    assert call_args["residual"] == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_check_session_skips_when_insufficient(monkeypatch):
    import drift_detector
    monkeypatch.setattr(drift_detector, "DRIFT_ENABLED", True)
    db = MagicMock()
    sort_mock = MagicMock()
    sort_mock.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[])
    db.learned_meta_residuals.find = MagicMock(return_value=sort_mock.sort.return_value.limit.return_value)
    # easier: just stub the helper
    monkeypatch.setattr("drift_detector._fetch_residuals", AsyncMock(return_value=[]))
    out = await _check_session(db, "ASIA")
    assert out["drift_detected"] is False
    assert out["n"] == 0
    assert "need" in out["reason"]


@pytest.mark.asyncio
async def test_check_session_detects_drift_on_regime_shift(monkeypatch):
    import drift_detector
    monkeypatch.setattr(drift_detector, "DRIFT_ENABLED", True)
    # Construct 200 low-residual samples followed by 200 high-residual samples
    # (model degraded). ADWIN should flag drift somewhere in the second half.
    series = [0.05] * 200 + [0.85] * 200
    monkeypatch.setattr("drift_detector._fetch_residuals",
                        AsyncMock(return_value=series))
    monkeypatch.setattr("drift_detector.MIN_RESIDUALS_PER_SESSION", 30)
    out = await _check_session(MagicMock(), "ASIA")
    assert out["drift_detected"] is True
    assert out["drifts_at_indices"]


@pytest.mark.asyncio
async def test_maybe_trigger_retrain_skipped_when_disabled(monkeypatch):
    import drift_detector
    monkeypatch.setattr(drift_detector, "DRIFT_ENABLED", False)
    out = await maybe_trigger_retrain(MagicMock())
    assert out["checked"] is False


@pytest.mark.asyncio
async def test_maybe_trigger_retrain_cooldown_blocks(monkeypatch):
    """When cooldown is active, drift detection should NOT retrain."""
    import drift_detector
    from datetime import datetime, timezone
    monkeypatch.setattr(drift_detector, "DRIFT_ENABLED", True)
    # Drift detected for ASIA
    monkeypatch.setattr("drift_detector.check_drift",
        AsyncMock(return_value={"enabled": True, "sessions": {
            "ASIA": {"session": "ASIA", "n": 100, "drift_detected": True}
        }}))
    # Last retrain just now → cooldown active
    monkeypatch.setattr("drift_detector._cooldown_ok",
        AsyncMock(return_value=(False, datetime.now(timezone.utc))))

    out = await maybe_trigger_retrain(MagicMock())
    assert out["drift"] is True
    assert out["retrained"] is False
    assert "cooldown" in out["reason"]


@pytest.mark.asyncio
async def test_maybe_trigger_retrain_fires_when_drift_and_cooldown_clear(monkeypatch):
    import drift_detector
    monkeypatch.setattr(drift_detector, "DRIFT_ENABLED", True)
    monkeypatch.setattr("drift_detector.check_drift",
        AsyncMock(return_value={"enabled": True, "sessions": {
            "ASIA": {"session": "ASIA", "n": 100, "drift_detected": True}
        }}))
    monkeypatch.setattr("drift_detector._cooldown_ok",
        AsyncMock(return_value=(True, None)))
    # Stub the retrain entry-point we late-import.
    import learned_meta
    monkeypatch.setattr(learned_meta, "retrain",
        AsyncMock(return_value={"trained": True, "n_samples": 50}))

    db = MagicMock()
    db.drift_detector_state.update_one = AsyncMock()
    db.drift_detector_audit.insert_one = AsyncMock()

    out = await maybe_trigger_retrain(db)
    assert out["retrained"] is True
    assert out["retrain_result"]["trained"] is True
    db.drift_detector_state.update_one.assert_awaited_once()
    db.drift_detector_audit.insert_one.assert_awaited_once()


# =========================== learned_meta integration ===========================
def test_fit_artifact_includes_calibration_block():
    from learned_meta import _fit_artifact
    np.random.seed(2)
    # 50 samples, 8 features — meets calibration threshold
    X = np.random.uniform(-1, 1, (50, 8))
    y = (X[:, 0] > 0).astype(float)
    art = _fit_artifact(X, y, key="learned_meta_test", label="TEST")
    assert "calibration" in art
    assert "brier_raw" in art["calibration"]
    assert "brier_calibrated" in art["calibration"]


@pytest.mark.asyncio
async def test_predict_p_win_returns_calibrated_and_raw(monkeypatch):
    """Inference path should surface both `p_win_raw` and `p_win_calibrated`."""
    from learned_meta import predict_p_win
    fake_art = {
        "weights": [0.1] * 8 + [0.0],  # bias
        "mu": [0.0] * 8,
        "sd": [1.0] * 8,
        "threshold": 0.4,
        "n_samples": 50,
        "train_auc": 0.7,
        "label": "GLOBAL",
        "calibration": {"A": 1.0, "B": 0.0, "skipped": False,
                        "brier_raw": 0.25, "brier_calibrated": 0.22},
    }
    monkeypatch.setattr("learned_meta.get_artifact",
                        AsyncMock(return_value=fake_art))
    signal = {"confidence": 70, "action": "BUY",
              "entry_price": 2000.0,
              "kalman_filter": {"k_velocity": 0.5},
              "cot_positioning": {},
              "real_yield_10y": {"regime": "neutral"},
              "mtf_gate": {"aligned": True},
              "upcoming_macro": []}
    out = await predict_p_win(signal)
    assert out is not None
    assert "p_win" in out and "p_win_raw" in out and "p_win_calibrated" in out
    assert out["calibrated"] is True
    assert out["brier_raw"] == 0.25
