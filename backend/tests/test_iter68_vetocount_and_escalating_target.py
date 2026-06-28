"""Tests for iter-68 — Per-veto reject counter on BotWatching +
auto-escalating profit target."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

import pytest

from profit_target import evaluate_profit_target
from routes.signal_routes import _classify_veto, _compute_veto_counts


# ─────────────────────── Veto classifier ───────────────────────
@pytest.mark.parametrize("text,expected_tag", [
    ("Market closed (reopens in 8h)", "market_closed"),
    ("VETO (entropy): Noise filter says NOISY (entropy=0.94)", "entropy"),
    ("Noise filter: market entropy=0.92 (NOISY). Random walk regime — trade vetoed.",
        "entropy"),
    ("VETO (regime): Regime CHOP detected — high vol without direction. Trade vetoed.",
        "regime_chop"),
    ("VETO (self-contradiction): model emitted BUY but reasoning blocks", "self_contra"),
    ("VETO (news): News sentiment is strongly bearish (-0.85); chart BUY vetoed.",
        "news"),
    ("VETO (meta-labeler): Meta-Labeler classified this as FAKE_OUT", "meta_label"),
    ("VETO (multi-timeframe): MTF tier check: 3/3 tiers bearish (...). BUY counter-trend",
        "mtf"),
    ("VETO (learned-meta): Learned classifier: p_win=0.32 < 0.55", "learned_meta"),
    ("VETO (A+ confluence): Only 2/4 confluences detected", "a_plus"),
    ("VETO (R:R): Weighted R:R 1.6 < min 2.0", "rr_ratio"),
    ("VETO (DXY gate): Bullish USD regime blocks long XAU", "dxy"),
    ("Sector cap reached: commodity 52% > limit 50%", "sector_cap"),
    ("Anti-pyramid: 2 BUY XAUUSD already open", "anti_pyramid"),
    ("Loss-streak circuit-breaker: 2 losses on XAUUSD BUY in 4h window",
        "loss_streak"),
    ("Signal cooldown active · NEXT IN 10M", "cooldown"),
])
def test_classify_veto_recognises_each_tag(text, expected_tag):
    result = _classify_veto(text)
    assert result is not None, f"Pattern not matched: {text!r}"
    tag, label = result
    assert tag == expected_tag


def test_classify_veto_unrecognised_returns_none():
    assert _classify_veto("Just a regular HOLD with no specific veto") is None
    assert _classify_veto("") is None
    assert _classify_veto(None) is None


# ─────────────────────── Veto counts aggregation ───────────────────────
class _FakeAsyncCursor:
    def __init__(self, docs):
        self._docs = docs
    def limit(self, _n):
        return self
    def __aiter__(self):
        self._iter = iter(self._docs)
        return self
    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeSignalsColl:
    def __init__(self, docs):
        self._docs = docs
    def find(self, _query, projection=None):
        return _FakeAsyncCursor(self._docs)


class _FakeDB:
    def __init__(self, signals_docs):
        self.signals = _FakeSignalsColl(signals_docs)


@pytest.mark.asyncio
async def test_compute_veto_counts_aggregates_by_tag():
    docs = [
        {"reasoning": "VETO (entropy): Noise filter NOISY"},
        {"reasoning": "Noise filter: market entropy=0.93 NOISY — vetoed"},
        {"reasoning": "Market closed (reopens 8h)"},
        {"reasoning": "VETO (R:R): R:R 1.2 < 2.0"},
        {"reasoning": "Some random reason that doesn't match"},
    ]
    db = _FakeDB(docs)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    out = await _compute_veto_counts(db, "user_x", since)
    assert out["total_holds"] == 5
    assert out["classified"] == 4
    by_tag = {row["tag"]: row["count"] for row in out["by_tag"]}
    assert by_tag.get("entropy") == 2
    assert by_tag.get("market_closed") == 1
    assert by_tag.get("rr_ratio") == 1


@pytest.mark.asyncio
async def test_compute_veto_counts_empty():
    db = _FakeDB([])
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    out = await _compute_veto_counts(db, "user_x", since)
    assert out["total_holds"] == 0
    assert out["classified"] == 0
    assert out["by_tag"] == []


# ─────────────────────── Profit target escalation ───────────────────────
class _FakePnlAware:
    """Stand-in DB letting us inject a today-PnL for evaluate_profit_target."""
    def __init__(self, pnl_today: float):
        self.pnl_today = pnl_today
        self.trades = self  # accessed by realised_pnl_since via db.trades
        self.tasks = self
    def find(self, *a, **kw):
        return _FakeAsyncCursor([{"pnl": self.pnl_today}])


@pytest.fixture
def patch_realised_pnl(monkeypatch):
    def _patch(amount: float):
        async def fake_pnl(_db, _user_id, _since, account_id=None):
            return amount
        monkeypatch.setattr("profit_target.realised_pnl_since", fake_pnl)
    return _patch


@pytest.mark.asyncio
async def test_profit_target_disabled(patch_realised_pnl):
    patch_realised_pnl(500.0)
    cfg = {"risk_level": "middle", "daily_profit_target_r": 0}
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["enabled"] is False
    assert out["hit"] is False


@pytest.mark.asyncio
async def test_profit_target_below_target(patch_realised_pnl):
    patch_realised_pnl(100.0)  # 1R = 100 (1% of 10k), target=2R=200, below
    cfg = {"risk_level": "middle", "daily_profit_target_r": 2.0}
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["enabled"] is True
    assert out["hit"] is False
    assert out["target_amount"] == 200.0  # 2R × $100
    assert out["escalation_steps"] == 0
    assert out["effective_target_r"] == 2.0


@pytest.mark.asyncio
async def test_profit_target_hit_no_escalate(patch_realised_pnl):
    """Legacy single-shot — hits target, no escalation."""
    patch_realised_pnl(250.0)
    cfg = {"risk_level": "middle", "daily_profit_target_r": 2.0,
           "daily_profit_target_action": "lock"}
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["hit"] is True
    assert out["locked_amount"] == 250.0
    assert out["escalation_steps"] == 0
    assert out["effective_target_r"] == 2.0
    assert out["next_target_r"] == 2.0  # no escalation → same as effective


@pytest.mark.asyncio
async def test_profit_target_escalate_one_step(patch_realised_pnl):
    """Base 2R + step 1R, P&L = 2.5R → step 1 absorbed (effective = 3R)."""
    # equity=10k, risk_pct=1% → 1R=$100
    # base 2R=$200, step 1R=$100. pnl=$250 overshoots by $50 (< 1 full step)
    # → steps_absorbed = (50 // 100) + 1 = 1
    patch_realised_pnl(250.0)
    cfg = {
        "risk_level": "middle",
        "daily_profit_target_r": 2.0,
        "daily_profit_target_escalate": True,
        "daily_profit_target_escalate_step_r": 1.0,
    }
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["hit"] is True
    assert out["escalation_steps"] == 1
    assert out["effective_target_r"] == 3.0
    assert out["target_amount"] == 300.0   # 3R × $100
    assert out["next_target_r"] == 4.0
    assert out["next_target_amount"] == 400.0
    assert out["locked_amount"] == 250.0


@pytest.mark.asyncio
async def test_profit_target_escalate_two_full_steps(patch_realised_pnl):
    """P&L $320 with base 2R + step 1R → 2 steps (crossed both 2R and 3R)."""
    patch_realised_pnl(320.0)
    cfg = {
        "risk_level": "middle",
        "daily_profit_target_r": 2.0,
        "daily_profit_target_escalate": True,
        "daily_profit_target_escalate_step_r": 1.0,
    }
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["escalation_steps"] == 2
    assert out["effective_target_r"] == 4.0
    assert out["next_target_r"] == 5.0


@pytest.mark.asyncio
async def test_profit_target_escalate_multiple_steps(patch_realised_pnl):
    """Base 2R + step 0.5R, P&L = 5R → many steps absorbed."""
    # 1R = $100. base 2R=$200, step 0.5R=$50. pnl=$500.
    # overshoot=$300 → 300/50 = 6 → steps = 6+1 = 7
    # effective = 2 + 7×0.5 = 5.5R
    patch_realised_pnl(500.0)
    cfg = {
        "risk_level": "middle",
        "daily_profit_target_r": 2.0,
        "daily_profit_target_escalate": True,
        "daily_profit_target_escalate_step_r": 0.5,
    }
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["hit"] is True
    assert out["escalation_steps"] == 7
    assert out["effective_target_r"] == 5.5
    assert out["next_target_r"] == 6.0


@pytest.mark.asyncio
async def test_profit_target_escalate_just_at_boundary(patch_realised_pnl):
    """P&L exactly equal to base target should trigger first escalation step."""
    patch_realised_pnl(200.01)  # tiny overshoot
    cfg = {
        "risk_level": "middle",
        "daily_profit_target_r": 2.0,
        "daily_profit_target_escalate": True,
        "daily_profit_target_escalate_step_r": 1.0,
    }
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["hit"] is True
    assert out["escalation_steps"] == 1
    assert out["effective_target_r"] == 3.0


@pytest.mark.asyncio
async def test_profit_target_escalate_disabled_does_not_ratchet(patch_realised_pnl):
    """With escalate=false, even huge P&L stays at base target."""
    patch_realised_pnl(2000.0)
    cfg = {
        "risk_level": "middle",
        "daily_profit_target_r": 2.0,
        "daily_profit_target_escalate": False,
    }
    accts = [{"_id": "a1", "equity": 10000}]
    out = await evaluate_profit_target(None, "u1", cfg, accts)
    assert out["hit"] is True
    assert out["escalation_steps"] == 0
    assert out["effective_target_r"] == 2.0
