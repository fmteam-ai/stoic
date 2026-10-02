"""Quant review fixes — replay-safe session anchor, entry-SL features,
Wilder RSI, Kalman covariance update. Pure Python, stubbed DB."""
import asyncio
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.unit


# ── 1 · intraday_features session anchor follows the evaluated bar ─────────
def _m15(n, start_ts, base=2000.0):
    return [{"t": start_ts + i * 900, "o": base + i * 0.1, "h": base + i * 0.1 + 1,
             "l": base + i * 0.1 - 1, "c": base + i * 0.1, "v": 10}
            for i in range(n)]


def test_intraday_features_historical_bars_have_session_vwap():
    from intraday_features import compute_intraday_features
    # 2024-03-05 00:00 UTC — a day that is definitely not "today"
    start = int(datetime(2024, 3, 5, tzinfo=timezone.utc).timestamp())
    bars = _m15(60, start)            # 15h of the same UTC day
    f = compute_intraday_features(bars)
    assert f["session_vwap"] is not None
    assert f["vwap_dist_pct"] is not None
    assert f["day_range_pct"] is not None
    assert f["session_high"] is not None and f["range_pos_pct"] is not None


def test_intraday_features_session_is_last_bars_day():
    from intraday_features import compute_intraday_features
    start = int(datetime(2024, 3, 4, 20, tzinfo=timezone.utc).timestamp())
    bars = _m15(48, start)            # 4h on 03-04, 8h on 03-05
    f = compute_intraday_features(bars)
    day2 = [b for b in bars if b["t"] >= start + 4 * 3600]
    assert f["session_high"] == round(max(b["h"] for b in day2), 2)
    assert f["session_low"] == round(min(b["l"] for b in day2), 2)
    # explicit now_ts on the previous day → that day's bars anchor the session
    f2 = compute_intraday_features(bars, now_ts=start)
    day1 = [b for b in bars if b["t"] < start + 4 * 3600]
    assert f2["session_high"] == round(max(b["h"] for b in day1), 2)


def test_intraday_features_live_today_unchanged():
    from intraday_features import compute_intraday_features
    now = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
    bars = _m15(45, now - 44 * 900)
    f = compute_intraday_features(bars)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_bars = [b for b in bars if datetime.fromtimestamp(
        b["t"], tz=timezone.utc).strftime("%Y-%m-%d") == today]
    assert f["session_high"] == round(max(b["h"] for b in today_bars), 2)


# ── 4 · training features use the entry SL, not the trailed SL ─────────────
class _AC:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class _Signals:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *a, **k):
        return _AC(self.docs)


class _DB:
    def __init__(self, sigs):
        self.signals = _Signals(sigs)


_SID = "65f000000000000000000001"


def _trade(**kw):
    t = {"signal_id": _SID, "action": "BUY", "symbol": "XAUUSD",
         "entry_price": 2000.0, "stop_loss": 1999.9,   # trailed to ~BE
         "original_stop_loss": 1990.0, "pnl": 50.0,
         "opened_at": "2024-03-05T10:00:00+00:00"}
    t.update(kw)
    return t


def _sl_pips(X):
    from ml_ensemble import FEATURE_NAMES
    return X[0][FEATURE_NAMES.index("sl_pips")]


def test_learning_pipeline_uses_entry_sl_from_signal():
    from bson import ObjectId
    from learning_pipeline import _build_xy
    from pip_utils import price_to_pips
    sig = {"_id": ObjectId(_SID), "stop_loss": 1990.0, "take_profit": 2020.0}
    X, y = asyncio.run(_build_xy(_DB([sig]), "u", [_trade()]))
    assert _sl_pips(X) == pytest.approx(abs(price_to_pips("XAUUSD", 10.0)), rel=1e-3)


def test_learning_pipeline_falls_back_to_original_stop_loss():
    from learning_pipeline import _build_xy
    from pip_utils import price_to_pips
    X, _ = asyncio.run(_build_xy(_DB([]), "u", [_trade()]))   # signal missing
    assert _sl_pips(X) == pytest.approx(abs(price_to_pips("XAUUSD", 10.0)), rel=1e-3)
    # legacy trade without original_stop_loss still featurizes
    X2, _ = asyncio.run(_build_xy(_DB([]), "u", [_trade(original_stop_loss=None)]))
    assert _sl_pips(X2) == pytest.approx(abs(price_to_pips("XAUUSD", 0.1)), rel=1e-3)


def test_ml_ensemble_training_uses_original_stop_loss(monkeypatch):
    import ml_ensemble

    captured = {}

    class _Stop(Exception):
        pass

    def fake_train(X, y, uid, window=None):
        captured["X"] = X
        raise _Stop

    class _Cur:
        def __init__(self, docs):
            self.docs = docs

        def sort(self, *a, **k):
            return self

        async def to_list(self, n):
            return self.docs

    class _Trades:
        def find(self, *a, **k):
            return _Cur([_trade(closed_at=f"2024-03-05T{10 + i % 10}:00:00")
                         for i in range(ml_ensemble.MIN_TRADES)])

    db = _DB([])
    db.trades = _Trades()
    monkeypatch.setattr(ml_ensemble, "ml_runtime_enabled", lambda: True)
    monkeypatch.setattr(ml_ensemble, "train_sync", fake_train)
    with pytest.raises(_Stop):
        asyncio.run(ml_ensemble.train_ensemble(db, "u"))
    from pip_utils import price_to_pips
    assert _sl_pips(captured["X"]) == pytest.approx(
        abs(price_to_pips("XAUUSD", 10.0)), rel=1e-3)


# ── 5 · Wilder RSI ──────────────────────────────────────────────────────────
def _wilder_reference(closes, p):
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = [max(x, 0) for x in d]
    lo = [max(-x, 0) for x in d]
    ag, al = sum(g[:p]) / p, sum(lo[:p]) / p
    for i in range(p, len(d)):
        ag = (ag * (p - 1) + g[i]) / p
        al = (al * (p - 1) + lo[i]) / p
    return 100 - 100 / (1 + ag / al)


def test_rsi_wilder_matches_reference():
    from indicators import rsi
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
              46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03]
    assert rsi(closes, 14) == pytest.approx(_wilder_reference(closes, 14), abs=1e-9)
    # Seed only (exactly period+1 closes) = simple averages
    assert rsi(closes[:15], 14) == pytest.approx(
        _wilder_reference(closes[:15], 14), abs=1e-9)
    # Classic Wilder/StockCharts sample (seed-only first value ≈ 70.5)
    assert rsi(closes[:15], 14) == pytest.approx(70.46, abs=0.1)


def test_rsi_edge_cases():
    from indicators import rsi
    assert rsi([1.0] * 10, 14) is None
    assert rsi([100.0] * 30, 14) == 50.0          # flat → neutral
    assert rsi([float(i) for i in range(30)], 14) == 100.0
    assert rsi([float(30 - i) for i in range(30)], 14) == pytest.approx(0.0)


# ── 6 · Kalman covariance update uses the prior P ───────────────────────────
def _kalman_numpy(prices, q=1e-3, r=1.0):
    import numpy as np
    F = np.array([[1.0, 1.0], [0.0, 1.0]])
    Qm = np.diag([q, q * 10])
    H = np.array([[1.0, 0.0]])
    s = np.array([prices[0], 0.0])
    P = np.eye(2)
    out = [(prices[0], 0.0)]
    for z in prices[1:]:
        s = F @ s
        P = F @ P @ F.T + Qm
        S = (H @ P @ H.T)[0, 0] + r
        K = (P @ H.T)[:, 0] / S
        s = s + K * (z - s[0])
        P = (np.eye(2) - np.outer(K, H[0])) @ P
        out.append((s[0], s[1]))
    return out


def test_kalman_matches_matrix_reference():
    from kalman import kalman_smooth
    prices = [100.0, 101.2, 100.8, 102.5, 103.1, 102.7, 104.0, 105.2,
              104.1, 106.3, 107.0, 106.2, 108.4, 109.1, 108.0]
    got = kalman_smooth(prices)
    ref = _kalman_numpy(prices)
    for g, (x, v) in zip(got, ref):
        assert g["k_price"] == pytest.approx(x, abs=1e-9)
        assert g["k_velocity"] == pytest.approx(v, abs=1e-9)
