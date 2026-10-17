"""v1.60.52 — #37 var_cap deleverage never closes a protected scratch; #38 Scalp page symbol selector + per-runner
toggles, scalp fast path honours user_direction_cap."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend", "src")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------------------------------------- #37 VaR deleverage
_INCIDENT = {"_id": ObjectId(), "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.04, "entry_price": 4180.0,
             "stop_loss": 4181.2, "status": "open", "live_pnl": -1.41}     # stop already ABOVE entry (breakeven+)


def test_open_risk_is_distance_to_stop_and_zero_at_or_beyond_breakeven():
    from portfolio.var import open_risk_usd, _stop_bounded_loss_usd, STOP_GAP_FACTOR
    assert open_risk_usd(_INCIDENT) == 0.0
    assert open_risk_usd({**_INCIDENT, "stop_loss": 4180.0}) == 0.0                     # exactly at entry
    sell_be = {**_INCIDENT, "action": "SELL", "stop_loss": 4179.5}
    assert open_risk_usd(sell_be) == 0.0
    risky = {**_INCIDENT, "stop_loss": 4170.0}                                           # 10 pts × 0.04 × 100 oz = $40
    assert abs(open_risk_usd(risky) - 40.0) < 1e-6
    assert open_risk_usd({**_INCIDENT, "stop_loss": None}) is None                      # no stop → parametric only
    rows = [{"symbol": "XAUUSD", "notional": 16720.0}]
    assert _stop_bounded_loss_usd([_INCIDENT], rows) == 0.0, "BE stop bounds the loss at zero (was None → unbounded)"
    assert abs(_stop_bounded_loss_usd([risky], rows) - 40.0 * STOP_GAP_FACTOR) < 1e-6


def test_var_cap_never_deleverages_a_breakeven_or_single_in_budget_position():
    import portfolio.risk_manager as rm
    db = FakeDb()
    acct = {"_id": ObjectId(), "equity": 10_000.0}      # notional 167 % — under the 500 % commodity cap

    async def dd(_db, _aid, _eq):
        return {"hard_breach": False, "soft_breach": False, "dd_pct": 0.0}

    async def huge_var(positions, *, equity, **_):            # parametric figure far above the 5 % cap
        return {"var_95_pct_equity": 12.0, "positions": [{"symbol": "XAUUSD", "weight": 1.0, "atr_pct": 0.02}], "notes": []}
    with patch.object(rm, "get_drawdown", dd), patch.object(rm, "calculate_var", huge_var):
        # (a) the 16 Oct incident: lone BUY, stop above entry → no action
        snap = _run(rm.build_snapshot(db, account=acct, open_positions=[_INCIDENT], cfg={"risk_level": "low"}))
        assert snap["actions"] == [] and "var_cap" not in snap["triggers"]
        assert any("zero open risk" in n for n in snap["var"]["notes"])
        # (b) lone position inside the per-trade cap (risk $40 = 0.4 % of $10k, cap low→adaptive 2 %) → no action
        risky = {**_INCIDENT, "stop_loss": 4170.0}
        snap = _run(rm.build_snapshot(db, account=acct, open_positions=[risky], cfg={"risk_level": "low"}))
        assert snap["actions"] == [] and any("within the per-trade cap" in n for n in snap["var"]["notes"])
        # (c) lone position OUTSIDE the cap ($400 = 4 % > 2 %) → var_cap still acts
        big = {**_INCIDENT, "stop_loss": 4080.0}
        snap = _run(rm.build_snapshot(db, account=acct, open_positions=[big], cfg={"risk_level": "low"}))
        assert "var_cap" in snap["triggers"] and snap["actions"][0]["reason"] == "auto_deleverage_var_cap"
        # (d) two positions, one at breakeven: the BE one is never the pick even though it is the worst loser
        other = {**_INCIDENT, "_id": ObjectId(), "stop_loss": 4080.0, "live_pnl": 3.0}
        snap = _run(rm.build_snapshot(db, account=acct, open_positions=[_INCIDENT, other], cfg={"risk_level": "low"}))
        assert [a["trade_id"] for a in snap["actions"]] == [str(other["_id"])]


# ------------------------------------------------------------------------------------------------- #38 scalp page + cap
def test_scalp_page_has_symbol_selector_and_per_runner_toggles_without_hardcoded_eurusd():
    page = open(os.path.join(FRONTEND, "pages", "Scalp.jsx")).read()
    assert 'const SCALP_SYMBOLS = ["EURUSD", "XAUUSD"];' in page
    assert 'data-testid="scalp-symbol-select"' in page
    assert 'symbol: "EURUSD"' not in page and 'symbol=EURUSD' not in page
    assert "const setConfig = async (enabled, mode, accId = accountId, sym = symbol)" in page
    assert "setConfig(false, \"shadow\", r.account_id, r.symbol)" in page
    assert "setConfig(true, \"shadow\", r.account_id, r.symbol)" in page
    assert "data-testid={`scalp-runner-disable-btn-${r.symbol}`}" in page
    assert "data-testid={`scalp-runner-enable-btn-${r.symbol}`}" in page
    assert "/scalp/session-window?symbol=${symbol}" in page
    route = open(os.path.join(BACKEND, "routes", "scalp_routes.py")).read()
    assert "(EURUSD only)" not in route and "', '.join(sorted(APPROVED))" in route


def test_scalp_fast_path_honours_user_direction_cap_across_engines():
    import user_direction_cap as udc
    from tests.unit.test_v1_60_50_pr100_review import _UniqueColl
    db = FakeDb()
    db.user_direction_reservations = _UniqueColl(db.user_direction_reservations)
    db.bot_configs.rows.append({"user_id": "u1", "account_id": "A", "user_direction_cap": 1})
    db.bot_configs.rows.append({"user_id": "u1", "account_id": None, "user_direction_cap": 3})
    assert _run(udc.user_direction_cap_for(db, "u1", "A")) == 1
    assert _run(udc.user_direction_cap_for(db, "u1", "B")) == 3           # falls back to the default config
    assert _run(udc.user_direction_cap_for(db, "u2", None)) == 1          # model default
    # a MAIN-BOT XAUUSD BUY is open → the scalp XAUUSD BUY (same user, other account) is refused
    db.trades.rows.append({"user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "status": "open", "origin": "auto",
                           "mode": "live", "scope": "hf_scalp"})
    rid, occ = _run(udc.reserve_user_direction(db, user_id="u1", base_symbol="XAUUSD", action="BUY", cap=1,
                                               account_id="B", symbol_match_q="XAUUSD"))
    assert rid is None and occ == 1
    rid, _ = _run(udc.reserve_user_direction(db, user_id="u1", base_symbol="XAUUSD", action="SELL", cap=1,
                                             account_id="B", symbol_match_q="XAUUSD"))
    assert rid is not None, "opposite direction is unaffected"
    eng = open(os.path.join(BACKEND, "scalp", "engine.py")).read()
    blk = eng[eng.index("v1.60.52 (#38)"):eng.index("# round 15 items 2/9")]
    assert "_udc_cap = await user_direction_cap_for(db, self.user_id, self.account_id)" in blk
    assert blk.index("reserve_user_direction(") < blk.index("_got = await _ar.reserve_entry(") < blk.index("trade = await engine.execute(")
    assert blk.count("await release_user_direction(db, _udc_res)") == 3     # DB refusal, caps refusal, finally
    assert '"refusal": "user_direction_cap"' in blk and '"user_direction_cap_block"' in blk
