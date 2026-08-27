"""iter-97 — Autopilot #11/#12/#15: operational modes, trend score,
mode governance ranking."""
import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from change_governance import classify_change
from operational_modes import DEFAULT_MODE, MODES, mode_gate, record_intercept
from trend_score import exhaustion_signals, trend_quality, trend_report


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter97-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------ operational modes
def test_mode_gate_defaults_to_observe():
    """Safety review — a missing/unknown config must NEVER default to live."""
    mg = mode_gate({}, {})
    assert mg["mode"] == DEFAULT_MODE == "observe"
    assert not mg["allow_new"] and mg["lot_scale"] == 0.0


def test_mode_gate_non_executing_modes():
    for m in ("observe", "shadow", "defensive", "panic"):
        mg = mode_gate({"operational_mode": m}, {})
        assert not mg["allow_new"], m
        assert mg["lot_scale"] == 0.0


def test_mode_gate_supervised_halves_size():
    mg = mode_gate({"operational_mode": "supervised_live"}, {})
    assert mg["allow_new"] and mg["lot_scale"] == 0.5


def test_mode_gate_demo_only_blocks_live_accounts():
    cfg = {"operational_mode": "demo_autopilot"}
    assert mode_gate(cfg, {"mode": "demo"})["allow_new"]
    assert not mode_gate(cfg, {"mode": "live"})["allow_new"]


def test_mode_gate_unknown_fails_closed():
    mg = mode_gate({"operational_mode": "yolo"}, {})
    assert not mg["allow_new"] and mg["mode"] == "observe"


def test_record_intercept(db):
    async def go():
        sig = {"symbol": "XAUUSD", "action": "BUY", "confidence": 70,
               "entry_price": 4000, "stop_loss": 3990, "take_profit": 4020,
               "scope": "hf_scalp", "consensus": {"score": 62},
               "monte_carlo": {"ev_r": 0.3}}
        await record_intercept(db, UID, {"_id": "a1"}, sig, 0.1,
                               mode_gate({"operational_mode": "shadow"}, {}))
        doc = await db.mode_intercepts.find_one({"user_id": UID})
        assert doc["mode"] == "shadow" and doc["consensus"] == 62
        assert doc["monte_carlo_ev_r"] == 0.3
        await db.mode_intercepts.delete_many({"user_id": UID})
    _run(go())


def test_mode_governance_ranking():
    # safer direction (toward observe/defensive) = conservative
    assert classify_change("operational_mode", "autonomous_live",
                           "defensive") == "conservative"
    assert classify_change("operational_mode", "autonomous_live",
                           "observe") == "conservative"
    assert classify_change("operational_mode", None,
                           "supervised_live") == "conservative"
    # toward live = aggressive
    assert classify_change("operational_mode", "observe",
                           "autonomous_live") == "aggressive"
    assert classify_change("operational_mode", "supervised_live",
                           "autonomous_live") == "aggressive"
    assert classify_change("operational_mode", "observe", "junk") == "aggressive"
    assert set(MODES) == {"observe", "shadow", "demo_autopilot",
                          "supervised_live", "autonomous_live",
                          "defensive", "panic"}


# ------------------------------------------------------------ trend score
def _trend_bars(n=96, step=1.0, rng=2.0, noise=0.0):
    out, px = [], 4000.0
    for i in range(n):
        px += step + (noise if i % 2 else -noise)
        out.append({"o": px - step * 0.8, "h": px + rng / 2, "l": px - rng / 2,
                    "c": px, "t": i * 900})
    return out


def test_trend_quality_clean_uptrend():
    q = trend_quality(_trend_bars(step=1.2), spread_pips=2.0, symbol="XAUUSD")
    assert q["direction"] == "UP"
    assert q["score"] >= 65
    assert set(q["components"]) == {"structure_alignment",
                                    "momentum_persistence",
                                    "volatility_support", "cross_timeframe",
                                    "spread_suitability"}


def test_trend_quality_flat_capped():
    q = trend_quality(_trend_bars(step=0.0, noise=1.0))
    assert q["direction"] == "FLAT"
    assert q["score"] <= 40


def test_trend_quality_wide_spread_degrades():
    tight = trend_quality(_trend_bars(step=1.2), 2.0, "XAUUSD")
    wide = trend_quality(_trend_bars(step=1.2), 15.0, "XAUUSD")
    assert wide["score"] < tight["score"]


def test_exhaustion_clean_trend_low():
    ex = exhaustion_signals(_trend_bars(step=1.2), "UP")
    assert ex["score"] <= 20


def test_exhaustion_signals_fire():
    # uptrend that stalls: tiny bodies + big upper wicks at the extreme
    bars = _trend_bars(step=1.2, n=90)
    top = bars[-1]["c"]
    for i in range(4):
        px = top + 0.1 * (i + 1)
        bars.append({"o": px - 0.05, "h": px + 3.0, "l": px - 0.4,
                     "c": px, "t": (90 + i) * 900})
    ex = exhaustion_signals(bars, "UP")
    assert "rejection_wicks" in ex["signals"]
    assert "weakening_follow_through" in ex["signals"]
    assert "momentum_divergence" in ex["signals"]
    assert ex["score"] >= 60
    r = trend_report(bars, symbol="XAUUSD")
    assert r["exhausted"] is True


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
