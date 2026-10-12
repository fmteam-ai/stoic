"""Funding-rate carry — scanner maths, risk verdicts and the shadow book (no network, no Mongo)."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit

from crypto_bridge import funding_carry as fc  # noqa: E402

H8 = 8 * 3600 * 1000


def _run(coro):
    return asyncio.run(coro)


def _snap(rate=0.0001, rates=None, spot=100.0, perp=100.02, start_ts=0, base="BTC"):
    rates = rates if rates is not None else [rate] * 21
    return fc.FundingSnapshot(base=base, exchange_id="binance", spot_symbol=f"{base}/USDT",
                              perp_symbol=f"{base}/USDT:USDT", spot_price=spot, perp_price=perp,
                              funding_rate=rates[-1] if rates else rate,
                              history=[{"ts": start_ts + i * H8, "rate": r} for i, r in enumerate(rates)])


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *a):
        return self

    async def to_list(self, n):
        return self.rows[:n]


class _Coll:
    def __init__(self):
        self.rows, self._n = [], 0

    def _match(self, r, q):
        return all(r.get(k) == v for k, v in q.items())

    def find(self, q):
        return _Cur([dict(r) for r in self.rows if self._match(r, q)])

    async def insert_one(self, doc):
        self._n += 1
        self.rows.append({**doc, "_id": self._n})

    async def update_one(self, q, upd):
        for r in self.rows:
            if self._match(r, q):
                r.update(upd["$set"])


class _DB(dict):
    def __missing__(self, k):
        self[k] = _Coll()
        return self[k]


def test_annualize_and_round_trip_cost():
    assert fc.annualize(0.0001, 8) == pytest.approx(0.1095)          # 0.01% × 3 × 365
    cfg = fc.CarryConfig()
    assert fc.round_trip_cost(cfg) == pytest.approx(2 * (0.001 + 0.0005 + 0.001))


def test_short_liquidation_price_and_distance():
    liq = fc.short_liquidation_price(100.0, 2.0, 0.004)
    assert liq == pytest.approx(150.0 / 1.004)
    assert fc.liquidation_distance(100.0, liq) == pytest.approx(liq / 100 - 1)
    assert fc.capital_required(1000.0, 2.0) == 1500.0


def test_evaluate_accepts_persistent_positive_funding():
    row = fc.evaluate(_snap(rate=0.0003), fc.CarryConfig())          # ~33% gross APR
    assert row["eligible"], row["reasons"]
    assert row["net_apr"] < row["mean_apr"]                          # fees are charged
    assert 0 < row["breakeven_days"] < 14


@pytest.mark.parametrize("snap,reason", [
    (_snap(rate=0.00002), "net_apr_below_min"),                      # too thin to repay fees
    (_snap(rates=[0.0003] * 20 + [-0.0001]), "current_funding_not_positive"),
    (_snap(rates=[0.0003, -0.0003] * 10 + [0.0003]), "funding_not_persistent"),
    (_snap(rate=0.0003, perp=101.0), "basis_dislocated"),
    (_snap(rates=[]), "no_funding_history"),
])
def test_evaluate_rejects_with_reason(snap, reason):
    row = fc.evaluate(snap, fc.CarryConfig())
    assert not row["eligible"] and reason in row["reasons"]


def test_perp_discount_counts_as_cost_premium_does_not_count_as_gain():
    cfg = fc.CarryConfig()
    flat = fc.evaluate(_snap(rate=0.0003, perp=100.0), cfg)["round_trip_cost"]
    assert fc.evaluate(_snap(rate=0.0003, perp=100.3), cfg)["round_trip_cost"] == pytest.approx(flat)
    assert fc.evaluate(_snap(rate=0.0003, perp=99.7), cfg)["round_trip_cost"] == pytest.approx(flat + 0.003)


def _pos(**kw):
    return {"spot_qty": 1.0, "perp_qty": 1.0, "entry_spot": 100.0, "entry_perp": 100.0, "leverage": 2.0, **kw}


@pytest.mark.parametrize("snap,action,reason", [
    (_snap(rate=0.0003), "hold", None),
    (_snap(rates=[0.0003] * 18 + [-0.0001] * 3), "exit", "funding_turned"),
    (_snap(rate=0.0003, spot=100.0, perp=103.0), "exit", "basis_blowout"),
    (_snap(rate=0.0003, spot=135.0, perp=135.0), "exit", "liquidation_risk_critical"),
    (_snap(rate=0.0003, spot=125.0, perp=125.0), "add_margin", "liquidation_distance_low"),
    (_snap(rate=0.0003, spot=160.0, perp=160.0), "liquidated", "perp_liquidated"),
])
def test_assess_actions(snap, action, reason):
    v = fc.assess(_pos(), snap, fc.CarryConfig())
    assert (v["action"], v["reason"]) == (action, reason)


def test_assess_flags_hedge_drift():
    v = fc.assess(_pos(spot_qty=1.05), _snap(rate=0.0003), fc.CarryConfig())
    assert v["action"] == "rebalance"


def test_config_from_dict_clamps_and_ignores_unknown():
    cfg = fc.CarryConfig.from_dict({"leverage": 0, "history_periods": 10**9, "max_positions": "4", "evil": 1})
    assert cfg.leverage == 1.0 and cfg.history_periods == 200 and cfg.max_positions == 4


class _FakeExchange:
    def __init__(self, fail=()):
        self.fail = fail

    async def fetch_funding_rate(self, sym):
        if sym.startswith(self.fail):
            raise ConnectionError("boom")
        return {"fundingRate": 0.0003, "markPrice": 100.02, "interval": "8h"}

    async def fetch_funding_rate_history(self, sym, since, limit):
        return [{"timestamp": i * H8, "fundingRate": 0.0003} for i in range(limit)][::-1]

    async def fetch_ticker(self, sym):
        return {"last": 100.0}


def test_scan_ranks_and_redacts_per_symbol_errors():
    res = _run(fc.scan("binance", ["BTC", "ETH"], exchange=_FakeExchange(fail=("ETH",))))
    assert [r["base"] for r in res["opportunities"]] == ["BTC"]
    assert res["errors"]["ETH"]["code"] == "exchange_network_error" and "boom" not in str(res["errors"])
    assert res["snapshots"]["BTC"].history[0]["ts"] < res["snapshots"]["BTC"].history[-1]["ts"]   # sorted


def test_shadow_cycle_opens_accrues_and_exits():
    db, cfg = _DB(), fc.CarryConfig(capital=3000.0, max_position_notional=1000.0)
    snaps = {b: _snap(rate=0.0003, base=b) for b in ("BTC", "ETH", "SOL")}
    scan = {"snapshots": snaps, "opportunities": [fc.evaluate(s, cfg) for s in snaps.values()]}
    out = _run(fc.run_shadow_cycle(db, "u1", scan, cfg, now_ms=21 * H8))
    opened = [e for e in out["events"] if e["action"] == "open"]
    assert len(opened) == 2                                          # 3000 capital / (1000 × 1.5)
    rows = db[fc.COLLECTION].rows
    assert all(r["mode"] == "shadow" and r["fees_paid"] > 0 for r in rows)

    # three new positive settlements → funding accrues to the short
    later = {b: _snap(rates=[0.0003] * 25, base=b) for b in ("BTC", "ETH")}
    _run(fc.run_shadow_cycle(db, "u1", {"snapshots": later, "opportunities": []}, cfg, now_ms=25 * H8))
    btc = next(r for r in rows if r["base"] == "BTC")
    assert btc["funding_accrued"] == pytest.approx(3 * 0.0003 * btc["perp_qty"] * 100.02)

    # funding flips → position closes with realized P&L recorded
    flipped = {"BTC": _snap(rates=[0.0003] * 25 + [-0.0002] * 3, base="BTC")}
    out = _run(fc.run_shadow_cycle(db, "u1", {"snapshots": flipped, "opportunities": []}, cfg, now_ms=28 * H8))
    assert {"base": "BTC", "action": "exit", "reason": "funding_turned"}.items() <= out["events"][0].items()
    assert btc["status"] == "closed" and "realized_pnl" in btc
    assert out["summary"]["closed_positions"] == 1 and out["summary"]["open_positions"] == 1


def test_shadow_cycle_records_liquidation():
    db, cfg = _DB(), fc.CarryConfig()
    s = _snap(rate=0.0003)
    _run(fc.run_shadow_cycle(db, "u1", {"snapshots": {"BTC": s}, "opportunities": [fc.evaluate(s, cfg)]},
                             cfg, now_ms=21 * H8))
    spike = _snap(rate=0.0003, spot=200.0, perp=200.0)
    out = _run(fc.run_shadow_cycle(db, "u1", {"snapshots": {"BTC": spike}, "opportunities": []}, cfg, now_ms=22 * H8))
    row = db[fc.COLLECTION].rows[0]
    assert row["status"] == "liquidated" and out["summary"]["liquidations"] == 1
    margin = row["perp_qty"] * row["entry_perp"] / 2.0
    assert row["realized_pnl"] < row["spot_qty"] * (200.0 - 100.0)  # margin loss is booked
    assert margin > 0
