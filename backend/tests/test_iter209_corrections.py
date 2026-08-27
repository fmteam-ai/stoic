"""iter-209 — six principal corrections to the v59/v60 brain:
1) immutable DecisionSnapshot + append-only DecisionEvents split,
2) distributed Degraded Intelligence health (Redis-shared counters),
3) broker/account-specific commission & swap costs,
4) CPCV metric renamed to oos_loss_rate (not formal PBO),
5) horizon-aware purging in walk-forward/CPCV,
6) hysteresis on Strategy Decay state transitions."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# ─────────── 1 · snapshot/events split (unit-level contract) ───────────

def test_snapshot_carries_no_stages_field():
    import inspect

    import decision_context
    src = inspect.getsource(decision_context.mint)
    assert '"stages"' not in src


class _InsertCapture:
    def __init__(self):
        self.docs = []

    async def count_documents(self, q, **k):
        return len([d for d in self.docs
                    if d["decision_id"] == q.get("decision_id")])

    async def insert_one(self, doc):
        self.docs.append(doc)


class _EventsDb:
    def __init__(self):
        self.decision_events = _InsertCapture()


def test_record_stage_appends_event_not_mutation():
    from decision_context import record_stage
    db = _EventsDb()
    asyncio.run(record_stage(db, "dec_abc", "meta_decision",
                             {"decision": "TRADE"}))
    assert len(db.decision_events.docs) == 1
    ev = db.decision_events.docs[0]
    assert ev["decision_id"] == "dec_abc" and ev["stage"] == "meta_decision"
    asyncio.run(record_stage(db, None, "x", {}))   # no id → no event
    assert len(db.decision_events.docs) == 1


# ─────────── 4 · CPCV metric honesty ───────────

def _series(n=120):
    return [1.6 if i % 20 < 11 else -1.0 for i in range(n)]


def test_cpcv_metric_renamed_no_pbo_claim():
    from champion_challenger2 import build_scorecard, cpcv
    cp = cpcv(_series())
    assert "oos_loss_rate" in cp and "pbo" not in cp
    sc = build_scorecard(_series())
    names = " ".join(c["name"] for c in sc["checks"])
    assert "OOS loss rate" in names and "overfitting probability" not in names


# ─────────── 5 · horizon-aware purging ───────────

def test_horizon_embargo_scales_with_holding_period():
    from champion_challenger2 import horizon_embargo
    # long holds (20 bars) at dense cadence (every 5 bars) → embargo 4
    log = [{"r": 1.0, "t": 100 + i * 5, "opened_t": 100 + i * 5 - 20}
           for i in range(40)]
    e = horizon_embargo(log)
    assert e["basis"] == "horizon" and e["trades"] == 4
    # short holds (2 bars) at sparse cadence (every 10 bars) → embargo 1
    log2 = [{"r": 1.0, "t": 100 + i * 10, "opened_t": 100 + i * 10 - 2}
            for i in range(40)]
    assert horizon_embargo(log2)["trades"] == 1


def test_horizon_embargo_fallback_fixed():
    from champion_challenger2 import EMBARGO_TRADES, horizon_embargo
    assert horizon_embargo(None) == {"trades": EMBARGO_TRADES,
                                     "basis": "fixed"}
    assert horizon_embargo([{"r": 1.0}] * 40)["basis"] == "fixed"


def test_walk_forward_uses_horizon_log():
    from champion_challenger2 import purged_walk_forward
    log = [{"r": r, "t": 100 + i * 5, "opened_t": 100 + i * 5 - 20}
           for i, r in enumerate(_series())]
    wf = purged_walk_forward(_series(), log=log)
    assert wf["embargo_basis"] == "horizon" and wf["embargo_trades"] == 4


# ─────────── 6 · decay hysteresis ───────────

def test_hysteresis_degradation_is_immediate():
    from strategy_decay import hysteresis_step
    state, streak = hysteresis_step("HEALTHY", "DECAYING", 0)
    assert state == "DECAYING" and streak == 0


def test_hysteresis_recovery_needs_streak_and_single_steps():
    from strategy_decay import hysteresis_step
    # first better reading → hold DECAYING
    state, streak = hysteresis_step("DECAYING", "HEALTHY", 0)
    assert state == "DECAYING" and streak == 1
    # second better reading → recover exactly ONE step
    state, streak = hysteresis_step("DECAYING", "HEALTHY", streak)
    assert state == "DEGRADED" and streak == 0
    # no previous state → raw applies
    assert hysteresis_step(None, "WATCH", 0) == ("WATCH", 0)


# ─────────── 3 · broker/account-specific costs ───────────

class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a):
        return self

    def limit(self, *a):
        return self

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _CostDb:
    def __init__(self, cfg=None, deals=None):
        self._cfg, self._deals = cfg, deals or []

    class _Coll:
        def __init__(self, rows=None, one=None):
            self._rows, self._one = rows or [], one

        async def find_one(self, *a, **k):
            return self._one

        def find(self, *a, **k):
            return _Cursor(self._rows)

    @property
    def bot_configs(self):
        return self._Coll(one=self._cfg)

    @property
    def broker_deals(self):
        return self._Coll(rows=self._deals)

    @property
    def trade_outcomes(self):
        return self._Coll()

    @property
    def trades(self):
        return self._Coll()


SIG = {"entry_price": 2000.0, "stop_loss": 1990.0, "spread": 0.5}


def test_costs_use_account_configured_commission():
    from transaction_costs import expected_cost_r
    db = _CostDb(cfg={"commission_usd_per_lot_side": 3.5})
    out = asyncio.run(expected_cost_r(db, "u1", "XAUUSD", signal=SIG,
                                      account_id="acc1"))
    assert out["basis"]["commission_source"] == "account_config"
    # 1 lot XAUUSD, 10.0 risk distance → $1000 risk/lot; $7 round-trip
    assert abs(out["components"]["commission_r"] - 0.007) < 1e-6


def test_costs_use_realized_deal_commission_and_swap():
    from transaction_costs import expected_cost_r
    deals = [{"commission": -7.0, "swap": -2.0, "lots": 1.0}] * 6
    db = _CostDb(cfg=None, deals=deals)
    out = asyncio.run(expected_cost_r(db, "u1", "XAUUSD", signal=SIG,
                                      scope="swing_trend",
                                      account_id="acc1"))
    assert out["basis"]["commission_source"] == "realized_deals"
    assert out["basis"]["swap_source"] == "realized_deals"
    assert out["components"]["swap_r"] > 0


def test_costs_default_without_account():
    from transaction_costs import COMMISSION_R, expected_cost_r
    out = asyncio.run(expected_cost_r(_CostDb(), "u1", "XAUUSD",
                                      signal=SIG))
    assert out["basis"]["commission_source"] == "default"
    assert out["components"]["commission_r"] == COMMISSION_R


# ─────────── 2 · distributed degraded intelligence ───────────

def test_degraded_report_consults_shared_store_on_unknown_ok():
    """A recovery observed in a fresh process (empty _mem) must read the
    shared store and write the recovery instead of silently returning."""
    from degraded_intelligence import _mem, report

    class _Coll:
        def __init__(self):
            self.updates = []

        async def find_one(self, *a, **k):
            return {"_id": "meta_decision", "ok": False,
                    "consecutive_failures": 3}

        async def update_one(self, q, u, **k):
            self.updates.append((q, u))

    class _Db:
        intelligence_health = _Coll()

    db = _Db()
    _mem.pop("meta_decision", None)
    os.environ.pop("REDIS_URL", None)
    asyncio.run(report(db, "meta_decision", ok=True))
    assert db.intelligence_health.updates, "recovery must be persisted"
    q, u = db.intelligence_health.updates[-1]
    assert u["$set"]["ok"] is True
    assert u["$set"]["consecutive_failures"] == 0
    _mem.pop("meta_decision", None)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
