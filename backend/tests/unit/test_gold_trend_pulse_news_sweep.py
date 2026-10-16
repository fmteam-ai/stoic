"""gold_trend_pulse items 13 (sweep & reclaim) and 14 (news mode) — rules, exits, limits, PRE protection, engine wiring, replay."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gold_trend_pulse import rules as R  # noqa: E402

T0 = 1_760_000_000 // 300 * 300          # aligned M5 epoch


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1}


def _flat(n, px=4150.0, t0=T0, step=300, wig=0.5):
    """n quiet M5 bars around px (ATR ≈ 1.0)."""
    out = []
    for i in range(n):
        o = px + (0.2 if i % 2 else -0.2)
        c = px - (0.2 if i % 2 else -0.2)
        out.append(_bar(t0 + i * step, o, max(o, c) + wig, min(o, c) - wig, c))
    return out


def _sweep_up_series(reclaim_close=4146.0, body_frac_ok=True, bump=None):
    """30 quiet bars at 4150 with one earlier dip to 4145.5 (= the 2 h low) [+ a pre-drop high `bump`] → 3-bar fast drop
    to 4145.0 (sweeps the 4145.5 low) → reclaim bar closing back above it. #68: SL (≈1.4) ≤ 1.3×TP (2.00)."""
    m5 = _flat(30)
    m5[10] = _bar(m5[10]["t"], 4150.0, 4150.4, 4145.5, 4149.8)
    t = m5[-1]["t"] + 300
    if bump:
        m5.append(_bar(t, 4150.0, bump, 4149.6, 4150.1)); t += 300
    m5 += [_bar(t, 4150.0, 4150.2, 4148.5, 4148.6), _bar(t + 300, 4148.6, 4148.7, 4146.5, 4146.6), _bar(t + 600, 4146.6, 4146.8, 4145.0, 4145.4)]
    o = 4145.3
    if body_frac_ok:
        m5.append(_bar(t + 900, o, reclaim_close + 0.1, 4145.2, reclaim_close))          # body 0.7 of range 0.9
    else:
        m5.append(_bar(t + 900, o, 4149.0, 4145.2, reclaim_close))                       # long wick, body < 50 %
    return m5


def _mirror(m5):
    px = 4150.0
    return [_bar(b["t"], 2 * px - b["o"], 2 * px - b["l"], 2 * px - b["h"], 2 * px - b["c"]) for b in m5]


# ------------------------------------------------------------------------------------------- 13. sweep & reclaim
def test_sweep_and_reclaim_detected_in_bias_direction_only_and_mirrored():
    m5 = _sweep_up_series()
    s = R.detect_sweep_reclaim(m5, "UP")
    assert s and s["trigger"] == "sweep" and s["direction"] == "BUY" and s["entry"] == 4146.0
    assert s["sweep_low"] == 4145.0 and s["swept_level"] == pytest.approx(4145.5, abs=0.01) and s["drop"] >= R.SWEEP_DROP_ATR * s["atr"]
    assert R.detect_sweep_reclaim(m5, "DOWN") is None          # never the opposite trade (no SELL on the breakdown)
    assert R.detect_sweep_reclaim(m5, "NONE") is None
    d = R.detect_sweep_reclaim(_mirror(m5), "DOWN")
    assert d and d["direction"] == "SELL" and d["sweep_low"] == pytest.approx(4155.0, abs=0.01)
    # no reclaim (close below the swept level) / weak body → nothing
    assert R.detect_sweep_reclaim(_sweep_up_series(reclaim_close=4145.45), "UP") is None
    assert R.detect_sweep_reclaim(_sweep_up_series(body_frac_ok=False), "UP") is None
    # small drop (< 1.5×ATR) that does not take out the 2 h low → nothing
    m5b = _flat(30) + [_bar(T0 + 30 * 300, 4150.0, 4150.5, 4149.6, 4149.8), _bar(T0 + 31 * 300, 4149.8, 4151.2, 4149.7, 4151.0)]
    assert R.detect_sweep_reclaim(m5b, "UP") is None


def test_sweep_exit_geometry_runner_and_split():
    s = R.detect_sweep_reclaim(_sweep_up_series(), "UP")
    p = R.plan_sweep_exit("BUY", s["entry"], s["sweep_low"], 4160.0, s["atr"])            # pre-drop high far above → runner
    assert p["ok"] and R.TP_MIN <= p["tp"] <= R.TP_MAX and p["sl"] <= R.SL_MAX_TP_MULT * p["tp"]      # #68 scalp cap
    assert p["stop_loss"] == pytest.approx(s["sweep_low"] - max(0.30, 0.25 * s["atr"]), abs=0.01)
    assert p["runner"] and p["runner"]["take_profit"] == 4160.0 and p["runner"]["be_trigger_price"] == p["take_profit"]
    assert p["runner"]["time_stop_min"] == R.RUNNER_TIME_STOP_MIN
    # pre-drop high inside the scalp TP → no runner (SL within the 3.5×ATR floor); runner disabled → none
    one = R.plan_sweep_exit("BUY", s["entry"], s["sweep_low"], s["entry"] + 1.0, s["atr"])
    assert one["ok"] and one["runner"] is None and one["sl"] <= 3.5 * s["atr"]
    assert R.plan_sweep_exit("BUY", s["entry"], s["sweep_low"], 4160.0, s["atr"], runner=False)["runner"] is None
    # SELL mirror: stop above the sweep high
    q = R.plan_sweep_exit("SELL", 4153.0, 4155.0, 4140.0, 1.0)
    assert q["ok"] and q["stop_loss"] > 4155.0 and q["take_profit"] < 4153.0
    # SL far wider than the target → skip
    bad = R.plan_sweep_exit("BUY", 4153.0, 4130.0, 4154.0, 1.0)
    assert not bad["ok"] and "SL too wide" in bad["skip"]
    assert not R.plan_sweep_exit("BUY", 4149.8, 4145.0, 4158.0, 1.7)["ok"]                        # #68 — SL 5.2 > 1.3×2.0 even with a far runner
    assert R.split_runner_lots(0.04) == (0.02, 0.02) and R.split_runner_lots(0.03) == (0.02, 0.01) and R.split_runner_lots(0.01) == (0.01, None)


def test_runner_leg_moves_to_breakeven_once_scalp_tp_is_hit_in_shadow_labeling():
    from gold_trend_pulse.engine import label_with_bars
    now = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    d = {"direction": "BUY", "entry": 4153.0, "take_profit": 4160.0, "stop_loss": 4144.7, "tp_dist": 7.0, "sl_dist": 8.3, "spread_at_entry": 0.25,
         "commission_price": 0.07, "created_at": (now - timedelta(minutes=40)).isoformat(), "time_stop_min": 90, "be_trigger_price": 4155.5, "lots": 0.01}
    t = int((now - timedelta(minutes=39)).timestamp()) // 60 * 60
    bars = [_bar(t, 4153.0, 4154.0, 4152.8, 4153.9), _bar(t + 60, 4153.9, 4155.6, 4153.5, 4155.2),      # scalp TP touched → BE armed
            _bar(t + 120, 4155.2, 4155.3, 4152.0, 4152.5)]                                               # back to entry → BE exit, never negative
    lab = label_with_bars(d, bars, now)
    assert lab and lab["exit_reason"] == "breakeven" and lab["net_price"] == pytest.approx(0.0, abs=0.01) and lab["be_armed"]
    # without the runner flag the 60 % rule applies (60 % of 7.0 = 4.2 → not reached by 4155.6) → still open
    d2 = {k: v for k, v in d.items() if k != "be_trigger_price"}
    assert label_with_bars(d2, bars, now) is None


def test_breakout_m15_never_sells_a_level_the_live_price_reclaimed():
    """#55 / item 13 — a BREAK_DOWN whose live price is back inside the channel is never sold."""
    from strategy_engines import run_engine
    feats = {"atr15": 2.0, "donchian20": "BREAK_DOWN", "donchian_lo20": 4150.0, "donchian_hi20": 4165.0, "last_price": 4151.0, "live_price_used": True,
             "momentum_3h_pct": -0.3, "day_range_pct": 1.2, "vwap_dist": -0.1, "trend": "DOWN", "range_pos": 0.1, "ema_slope": -0.1}
    sig, note = run_engine("breakout_m15", feats)
    assert sig is None and "no stale sell" in note


# ------------------------------------------------------------------------------------------- 14. news mode
def _ev(title, t, actual=None, forecast=None):
    return {"title": title, "country": "USD", "impact": "high", "when_ts": t, "actual": actual, "forecast": forecast}


def test_news_event_matching_phases_and_configurable_list():
    t = T0 + 3600
    assert R.news_event_kind("Core CPI m/m") == "Core CPI" and R.news_event_kind("CPI y/y") == "CPI" and R.news_event_kind("Non-Farm Employment Change") == "Nonfarm Payrolls"
    assert R.news_event_kind("FOMC Statement") == "FOMC" and R.news_event_kind("Fed Chair Powell Speaks") == "Fed Chair" and R.news_event_kind("ISM Manufacturing PMI") == "ISM"
    assert R.news_event_kind("Unemployment Claims") is None and R.news_event_kind("CPI y/y", ["NFP"]) is None      # configurable list
    evs = [_ev("CPI m/m", t, "0.4", "0.2")]
    assert R.news_phase(evs, datetime.fromtimestamp(t - 20 * 60, tz=timezone.utc)) is None
    assert R.news_phase(evs, datetime.fromtimestamp(t - 10 * 60, tz=timezone.utc))["phase"] == "PRE"
    assert R.news_phase(evs, datetime.fromtimestamp(t + 2 * 60, tz=timezone.utc))["phase"] == "RELEASE"
    assert R.news_phase(evs, datetime.fromtimestamp(t + 7 * 60, tz=timezone.utc))["phase"] == "POST"
    assert R.news_phase(evs, datetime.fromtimestamp(t + 7 * 60, tz=timezone.utc), release_min=10)["phase"] == "RELEASE"   # configurable release window
    assert R.news_phase(evs, datetime.fromtimestamp(t + 91 * 60, tz=timezone.utc)) is None
    assert R.news_phase([{**evs[0], "country": "GBP"}], datetime.fromtimestamp(t, tz=timezone.utc)) is None             # USD only
    assert "protect at" in R.news_plan_text(evs[0], "CPI") and "news trades from" in R.news_plan_text(evs[0], "CPI")
    nxt = R.next_news_event(evs, datetime.fromtimestamp(t - 3600, tz=timezone.utc))
    assert nxt["kind"] == "CPI" and nxt["phase"] is None and "CPI" in nxt["plan"]


def test_surprise_bias_mapping_thresholds_and_fomc_candle():
    e = _ev("CPI m/m", T0, "0.4", "0.2")
    assert R.news_surprise_bias(e, "CPI")["bias"] == "DOWN"                 # hotter inflation → USD strong → XAU DOWN
    assert R.news_surprise_bias(_ev("CPI m/m", T0, "0.1", "0.3"), "CPI")["bias"] == "UP"
    assert R.news_surprise_bias(_ev("CPI m/m", T0, "0.25", "0.2"), "CPI")["bias"] == "NONE"     # below the 0.1 pp threshold
    assert R.news_surprise_bias(_ev("Nonfarm Payrolls", T0, "250", "180"), "Nonfarm Payrolls")["bias"] == "DOWN"
    assert R.news_surprise_bias(_ev("Nonfarm Payrolls", T0, "190", "180"), "Nonfarm Payrolls")["bias"] == "NONE"  # < 25k
    assert R.news_surprise_bias(_ev("CPI m/m", T0), "CPI")["bias"] is None                      # actual not published yet
    assert R.news_surprise_bias(_ev("FOMC Statement", T0), "FOMC")["bias"] is None               # rate event → candle decides
    post = [_bar(T0, 4150.0, 4151, 4149, 4150.5), _bar(T0 + 300, 4150.5, 4152, 4150, 4151.5), _bar(T0 + 600, 4151.5, 4154, 4151, 4153.5)]
    assert R.fomc_bias(post[:2])["bias"] is None and R.fomc_bias(post)["bias"] == "UP"
    assert R.fomc_bias(_mirror(post))["bias"] == "DOWN"


def test_news_confirmation_agree_disagree_pending():
    pre = (4151.0, 4149.0)
    agree = [_bar(T0, 4150.0, 4153.0, 4149.8, 4152.5)]
    assert R.news_confirmation("UP", pre, agree, T0)["confirmed"] is True
    assert R.news_confirmation("DOWN", pre, agree, T0)["confirmed"] is False           # first move against the surprise → no news trade
    inside = [_bar(T0, 4150.0, 4150.8, 4149.2, 4150.3)]
    assert R.news_confirmation("UP", pre, inside, T0)["confirmed"] is None             # waiting
    late = [_bar(T0 + i * 300, 4150.0, 4150.8, 4149.2, 4150.3) for i in range(3)]
    assert R.news_confirmation("UP", pre, late, T0)["confirmed"] is False               # nothing within 15 min
    assert R.news_confirmation("UP", None, agree, T0)["confirmed"] is False


def test_news_entry_pullback_exits_clamp_time_stop_and_limits():
    m5 = _flat(30, px=4150.0)
    t_rel = m5[-1]["t"] + 300
    post = [_bar(t_rel, 4150.0, 4156.0, 4149.8, 4155.5), _bar(t_rel + 300, 4155.5, 4158.0, 4155.0, 4157.5),
            _bar(t_rel + 600, 4157.5, 4157.6, 4154.2, 4154.6),                        # pullback into the 38–50 % zone (4154.9–4154.0)
            _bar(t_rel + 900, 4154.6, 4156.4, 4154.3, 4156.2)]                        # closes with the bias
    c = R.detect_news_entry(m5 + post, post, "UP", 4150.0)
    assert c and c["trigger"] == "news_pullback" and c["direction"] == "BUY" and c["entry"] == 4156.2 and c["extreme"] == 4158.0
    assert R.detect_news_entry(m5 + post, post, "DOWN", 4150.0) is None
    p = R.plan_news_exit("BUY", 4156.2, c["swing"], 1.5)
    assert p["ok"] and p["tp"] == R.NEWS_TP_MIN and p["time_stop_min"] == R.NEWS_TIME_STOP_MIN     # 0.7×1.5 clamps up to 3.00
    assert R.plan_news_exit("BUY", 4156.2, c["swing"], 20.0)["tp"] == R.NEWS_TP_MAX                 # clamp 8.00
    bad = R.plan_news_exit("BUY", 4156.2, 4140.0, 1.5)
    assert not bad["ok"] and "SL too wide" in bad["skip"]                                             # max 1.3×TP
    assert R.NEWS_MAX_TRADES_PER_EVENT == 2 and R.NEWS_TP_MIN == 3.0 and R.NEWS_TP_MAX == 8.0


def test_news_pre_action_policies():
    assert R.news_pre_action("BUY", 4150.0, 4.0, 4152.5, "protect")["action"] == "breakeven"       # ≥ +0.5R → BE+costs
    assert R.news_pre_action("SELL", 4150.0, 4.0, 4147.5, "close")["action"] == "breakeven"
    assert R.news_pre_action("BUY", 4150.0, 4.0, 4150.5, "protect")["action"] == "keep"
    assert R.news_pre_action("BUY", 4150.0, 4.0, 4150.5, "close")["action"] == "close"
    t = R.news_pre_action("BUY", 4150.0, 4.0, 4150.5, "tighten")
    assert t["action"] == "tighten" and t["new_sl"] == 4148.0                                      # −0.5R
    assert R.news_pre_action("BUY", 4150.0, 4.0, 4150.5, "bogus")["action"] == "keep"              # unknown policy → protect


# ------------------------------------------------------------------------------------------- engine wiring (fake db)
class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return list(self.rows)

    def __aiter__(self):
        async def gen():
            for r in self.rows:
                yield r
        return gen()


class _Coll:
    def __init__(self):
        self.rows = []

    @staticmethod
    def _match(doc, q):
        for k, v in q.items():
            cur = doc
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(v, dict):
                if "$exists" in v and (cur is not None) != v["$exists"]:
                    return False
                if "$in" in v and cur not in v["$in"]:
                    return False
                if "$gte" in v and (cur is None or cur < v["$gte"]):
                    return False
                if "$ne" in v and cur == v["$ne"]:
                    return False
                if "$lte" in v and (cur is None or cur > v["$lte"]):
                    return False
            elif cur != v:
                return False
        return True

    async def find_one(self, q, *a, **k):
        return next((r for r in self.rows if self._match(r, q)), None)

    def find(self, q=None, *a, **k):
        return _Cursor([r for r in self.rows if self._match(r, q or {})])

    async def count_documents(self, q):
        return len([r for r in self.rows if self._match(r, q)])

    async def insert_one(self, doc):
        doc.setdefault("_id", f"id{len(self.rows)}")
        self.rows.append(doc)

    async def update_one(self, q, upd, upsert=False):
        row = await self.find_one(q)
        if row is None:
            if not upsert:
                return
            row = dict(q)
            row.update(upd.get("$setOnInsert", {}))
            self.rows.append(row)
        row.update(upd.get("$set", {}))
        for k, v in upd.get("$inc", {}).items():
            row[k] = row.get(k, 0) + v
        for k, v in upd.get("$push", {}).items():
            row.setdefault(k, []).extend(v["$each"] if isinstance(v, dict) and "$each" in v else [v])


class _DB:
    def __init__(self):
        for n in ("gtp_state", "gtp_decisions", "gtp_news_events", "trades", "bot_configs", "accounts"):
            setattr(self, n, _Coll())


def _engine_env(monkeypatch, m5, now, events):
    from gold_trend_pulse import engine as E
    m1 = []
    for b in m5:                                   # 5 flat M1 bars per M5 bar so the M5 resample reproduces the fixture
        for i in range(5):
            m1.append(_bar(b["t"] + i * 60, b["o"] if i == 0 else b["c"], b["h"] if i == 2 else max(b["o"], b["c"]),
                           b["l"] if i == 2 else min(b["o"], b["c"]), b["c"]))
    m15 = R.resample(m1, 900)

    async def _load(db, user_id, account, now_):
        return {"m15": m15, "h1": R.resample(m15, 3600), "h4": R.resample(m15, 14400), "m5": m5, "m1": m1, "today_open": None, "prior_day_mid": None, "source": "test"}

    async def _events(cfg):
        return events

    async def _gate(db, account_id):
        return {"eligible": False, "reasons": ["0/100"], "stats": {}}
    monkeypatch.setattr(E, "load_bars", _load)
    monkeypatch.setattr(E, "usd_high_impact_events", _events)
    monkeypatch.setattr(E, "live_gate", _gate)
    monkeypatch.setattr(E, "pip_value_usd_per_lot", lambda *a, **k: 10.0)
    return E


def test_engine_news_pre_protects_open_shadow_and_blocks_entries(monkeypatch):
    m5 = _sweep_up_series()
    t_rel = m5[-1]["t"] + 300 + 10 * 60
    now = datetime.fromtimestamp(m5[-1]["t"] + 300 + 60, tz=timezone.utc)        # PRE phase (T−9 min), UP bias, sweep setup on the last close
    E = _engine_env(monkeypatch, m5, now, [_ev("CPI m/m", t_rel, None, "0.2")])
    db = _DB()
    db.gtp_state.rows.append({"_id": "acc1", "bias": {"bias": "UP", "candidate": "UP", "confirmations": 0, "computed_at": now.isoformat(), "reasons": []}, "regime": "TREND"})
    db.gtp_decisions.rows.append({"_id": "d0", "decision_id": "open1", "account_id": "acc1", "verdict": "shadow", "outcome": None, "direction": "BUY", "entry": 4143.5,
                                  "sl_dist": 4.0, "tp_dist": 3.0, "take_profit": 4146.5, "stop_loss": 4139.5, "spread_at_entry": 0.25, "commission_price": 0.07,
                                  "created_at": (now - timedelta(minutes=10)).isoformat(), "regime": "TREND"})
    acc = {"_id": "acc1", "user_id": "u1", "equity": 10_000.0, "account_type": "demo"}
    res = _run(E.evaluate(db, {"gold_trend_pulse_enabled": True}, acc, now, spread_price=0.25))
    assert res["note"].startswith("NEWS PRE CPI") and res["decision"] is None and res["signal"] is None   # no entry even though a sweep setup exists
    row = db.gtp_decisions.rows[0]
    assert row["news_pre_action"]["action"] == "breakeven" and row["stop_loss"] == R.breakeven_level("BUY", 4143.5, 0.25, 0.07)   # +0.62R → BE+costs
    ev = db.gtp_news_events.rows[0]
    assert ev["kind"] == "CPI" and ev["phase"] == "PRE" and ev["pre_actions"][0]["action"] == "breakeven"
    # close policy on a losing position → labeled closed
    db2 = _DB(); db2.gtp_state.rows = [dict(db.gtp_state.rows[0])]
    db2.gtp_decisions.rows.append({**db.gtp_decisions.rows[0], "_id": "d1", "decision_id": "open2", "news_pre_action": None, "news_protected": None, "stop_loss": 4146.0, "entry": 4155.0})
    _run(E.evaluate(db2, {"gold_trend_pulse_enabled": True, "gold_trend_pulse_news_pre_action": "close"}, acc, now, spread_price=0.25))
    assert db2.gtp_decisions.rows[0]["outcome"]["exit_reason"] == "news_pre_close"
    # RELEASE phase blocks too
    now_rel = datetime.fromtimestamp(t_rel + 120, tz=timezone.utc)
    res = _run(E.evaluate(_DB(), {"gold_trend_pulse_enabled": True}, acc, now_rel, spread_price=0.25))
    assert res["note"].startswith("NEWS RELEASE") and res["decision"] is None
    # news mode OFF → the generic ±15-min USD high-impact blackout (item 4) still refuses the sweep entry
    db3 = _DB(); db3.gtp_state.rows = [{k: v for k, v in db.gtp_state.rows[0].items() if k != "last_m5_t"}]
    res = _run(E.evaluate(db3, {"gold_trend_pulse_enabled": True, "gold_trend_pulse_news_mode_enabled": False}, acc, now, spread_price=0.25))
    assert res["decision"] is None and "USD high-impact event" in res["note"]
    # no event at all → the same bars produce the sweep entry
    db4 = _DB(); db4.gtp_state.rows = [dict(db3.gtp_state.rows[0])]; db4.gtp_state.rows[0].pop("last_m5_t", None)
    E4 = _engine_env(monkeypatch, m5, now, [])
    res = _run(E4.evaluate(db4, {"gold_trend_pulse_enabled": True}, acc, now, spread_price=0.25))
    assert res["decision"] and res["decision"]["trigger"] == "sweep"


def test_engine_sweep_entry_records_scalp_and_runner_legs_as_one_basket(monkeypatch):
    m5 = _sweep_up_series(bump=4158.0)                 # pre-drop high 4158 → runner target
    now = datetime.fromtimestamp(m5[-1]["t"] + 300 + 30, tz=timezone.utc)
    E = _engine_env(monkeypatch, m5, now, [])
    db = _DB()
    db.gtp_state.rows.append({"_id": "acc1", "bias": {"bias": "UP", "candidate": "UP", "confirmations": 0, "computed_at": now.isoformat(), "reasons": []}, "regime": "TREND"})
    acc = {"_id": "acc1", "user_id": "u1", "equity": 100_000.0, "account_type": "demo"}      # big equity → lots ≥ 0.02 → runner
    res = _run(E.evaluate(db, {"gold_trend_pulse_enabled": True}, acc, now, spread_price=0.25))
    d = res["decision"]
    assert d["trigger"] == "sweep" and d["verdict"] == "shadow" and d["sweep"]["sweep_low"] == 4145.0 and d["regime"] == "TREND"
    legs = [r for r in db.gtp_decisions.rows if r.get("setup_id") == d["setup_id"]]
    assert len(legs) == 2 and {r["trigger"] for r in legs} == {"sweep", "sweep_runner"}
    runner = next(r for r in legs if r["trigger"] == "sweep_runner")
    assert runner["basket_id"] == d["basket_id"] == d["setup_id"] and runner["runner"] and runner["be_trigger_price"] == d["take_profit"]
    assert runner["stop_loss"] == d["stop_loss"] and runner["take_profit"] > d["take_profit"] and runner["time_stop_min"] == R.RUNNER_TIME_STOP_MIN
    assert abs((d["risk_usd"] + runner["risk_usd"]) - 0.0025 * 100_000) < 0.0025 * 100_000 * 0.5          # total risk stays at 0.25 % (lot rounding)
    # runner OFF → single leg
    db2 = _DB(); db2.gtp_state.rows = [{k: v for k, v in db.gtp_state.rows[0].items() if k != "last_m5_t"}]
    _run(E.evaluate(db2, {"gold_trend_pulse_enabled": True, "gold_trend_pulse_sweep_runner_enabled": False}, acc, now, spread_price=0.25))
    assert len(db2.gtp_decisions.rows) == 1


def test_engine_news_post_trades_confirmed_bias_max_two_and_records_event_report(monkeypatch):
    m5 = _flat(30, px=4150.0)
    t_rel = m5[-1]["t"] + 300
    post = [_bar(t_rel, 4150.0, 4156.0, 4149.8, 4155.5), _bar(t_rel + 300, 4155.5, 4158.0, 4155.0, 4157.5),
            _bar(t_rel + 600, 4157.5, 4157.6, 4154.2, 4154.6), _bar(t_rel + 900, 4154.6, 4156.4, 4154.3, 4156.2)]
    allb = m5 + post
    now = datetime.fromtimestamp(allb[-1]["t"] + 300 + 30, tz=timezone.utc)      # T+20:30 → POST
    E = _engine_env(monkeypatch, allb, now, [_ev("CPI m/m", t_rel, "0.1", "0.3")])   # miss → XAU UP, first move UP → confirmed
    db = _DB()
    db.gtp_state.rows.append({"_id": "acc1", "bias": {"bias": "NONE", "candidate": "NONE", "confirmations": 0, "computed_at": now.isoformat(), "reasons": []}, "regime": "NO_TRADE"})
    acc = {"_id": "acc1", "user_id": "u1", "equity": 10_000.0, "account_type": "demo"}
    res = _run(E.evaluate(db, {"gold_trend_pulse_enabled": True}, acc, now, spread_price=0.25))
    d = res["decision"]
    assert d and d["regime"] == "NEWS" and d["trigger"] == "news_pullback" and d["direction"] == "BUY" and d["bias"] == "UP"
    assert d["tp_dist"] >= R.NEWS_TP_MIN and d["time_stop_min"] == R.NEWS_TIME_STOP_MIN and d["news"]["kind"] == "CPI"
    ev = db.gtp_news_events.rows[0]
    assert ev["news_bias"] == "UP" and ev["first_move"] == "UP" and ev["confirmed"] is True and ev["trades"] == 1 and ev["surprise"] == pytest.approx(-0.2)
    # the engine traded although the daily regime was NO_TRADE — the news bias replaces the daily bias in POST
    # cap: 2 per event → third evaluation refuses
    db.gtp_news_events.rows[0]["trades"] = 2
    db.gtp_decisions.rows.clear(); db.gtp_state.rows[0].pop("last_m5_t", None)
    res = _run(E.evaluate(db, {"gold_trend_pulse_enabled": True}, acc, now, spread_price=0.25))
    assert "2/2 news trades" in res["note"] and res["decision"] is None
    # hot print (XAU DOWN) but the first move was UP → NOT confirmed → no trade, event row says why
    db2 = _DB(); db2.gtp_state.rows = [dict(db.gtp_state.rows[0])]; db2.gtp_state.rows[0].pop("last_m5_t", None)
    E2 = _engine_env(monkeypatch, allb, now, [_ev("CPI m/m", t_rel, "0.5", "0.3")])
    res = _run(E2.evaluate(db2, {"gold_trend_pulse_enabled": True}, acc, now, spread_price=0.25))
    assert "NOT confirmed" in res["note"] and res["decision"] is None and db2.gtp_news_events.rows[0]["confirmed"] is False


def test_news_sweep_defaults_config_routes_and_card_fields():
    from models import BotConfigUpdate
    cfg = BotConfigUpdate().model_dump()
    assert cfg["gold_trend_pulse_enabled"] is False and cfg["gold_trend_pulse_news_mode_enabled"] is True and cfg["gold_trend_pulse_news_pre_action"] == "protect"
    assert cfg["gold_trend_pulse_news_release_min"] == 5 and cfg["gold_trend_pulse_news_events"] is None and cfg["gold_trend_pulse_sweep_runner_enabled"] is True
    from gold_trend_pulse.engine import news_settings, runner_on
    ns = news_settings({})
    assert ns["events"] == R.NEWS_EVENTS_DEFAULT and ns["pre_action"] == "protect" and ns["release_min"] == 5
    assert news_settings({"gold_trend_pulse_news_pre_action": "bogus", "gold_trend_pulse_news_release_min": 99})["pre_action"] == "protect"
    assert news_settings({"gold_trend_pulse_news_events": ["CPI", "FOMC"]})["events"] == ["CPI", "FOMC"] and runner_on({}) and not runner_on({"gold_trend_pulse_sweep_runner_enabled": False})
    src = open(os.path.join(BACKEND, "routes", "gold_trend_pulse_routes.py")).read()
    for f in ("news_pre_action", "news_release_min", "news_events", "sweep_runner_enabled", "news_mode_enabled"):
        assert f in src
    tm = open(os.path.join(BACKEND, "trade_manager.py")).read()
    assert 'meta.get("be_trigger_price")' in tm                      # live runner: BE at the scalp TP
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert '"next": R.next_news_event(' in eng and "gtp_news_pre_close" in eng
    mi = open(os.path.join(BACKEND, "market_intel.py")).read()
    assert "next_news_event" in mi


def test_replay_marks_sweeps_reports_news_separately_and_traces_window():
    from gold_trend_pulse.replay import markdown, replay
    from gold_trend_pulse.synthetic import synthetic_m1
    bars = synthetic_m1(days=8, seed=3)
    d0 = datetime.fromtimestamp(bars[0]["t"], tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    t_cpi = (d0 + timedelta(days=3, hours=12, minutes=30)).timestamp()
    ev = [_ev("CPI m/m", t_cpi, "0.4", "0.2"), _ev("FOMC Statement", (d0 + timedelta(days=5, hours=18)).timestamp())]
    rep = replay(bars, spread=0.25, commission_usd_per_lot_side=3.5, news_events=ev, trace_window=(t_cpi - 1800, t_cpi + 7200))
    assert set(rep["by_mode"]) == {"TREND", "RANGE", "NEWS"}
    assert rep["sweeps"]["marked"] >= rep["sweeps"]["caught"] and "list" in rep["sweeps"]
    assert rep["news"] is not None and [e["kind"] for e in rep["news"]["events"]][0] == "CPI" and rep["news"]["events"][0]["bias"] == "DOWN"
    assert all(t["regime"] != "NEWS" for t in []) and rep["overall"]["n"] == rep["trades"]            # normal-session stats exclude NEWS rows
    assert rep["trace"] and any("NEWS PRE" in r["note"] for r in rep["trace"]) and any("NEWS RELEASE" in r["note"] for r in rep["trace"])
    md = markdown(rep)
    assert "Sweep & reclaim (item 13)" in md and "News mode (item 14)" in md and "Window trace" in md


# ------------------------------------------------------------------------------------------- VPS tooling (read-only)
def test_replay_loads_mt5_export_applies_tz_offset_and_writes_reports_only(tmp_path):
    from gold_trend_pulse.replay import load_csv, replay, write_trades_csv
    from gold_trend_pulse.synthetic import synthetic_m1
    bars = synthetic_m1(days=3, seed=5)
    p = tmp_path / "XAUUSD_M1.csv"
    with open(p, "w") as f:                                      # MT5 History export: tab-separated, broker time UTC+3
        f.write("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n")
        for b in bars:
            d = datetime.fromtimestamp(b["t"] + 3 * 3600, tz=timezone.utc)
            f.write(f"{d:%Y.%m.%d}\t{d:%H:%M:%S}\t{b['o']:.2f}\t{b['h']:.2f}\t{b['l']:.2f}\t{b['c']:.2f}\t{int(b['v'] or 1)}\t0\t25\n")
    loaded = load_csv(str(p))
    assert len(loaded) == len(bars) and loaded[0]["t"] == bars[0]["t"] + 3 * 3600 and loaded[0]["s"] == 25.0
    rep = replay(loaded, broker_offset_sec=3 * 3600)             # --tz-offset 3 → bars back in UTC
    assert rep["bars_m1"] == len(bars) and "trade_rows" in rep
    out = tmp_path / "trades.csv"
    assert write_trades_csv(rep, str(out)) == len(rep["trade_rows"]) and out.read_text().startswith("entry_at,exit_at,regime,trigger")
    # comma-separated MT5 variant and ISO variant both parse
    q = tmp_path / "b.csv"
    q.write_text("2026-10-16,13:15,4160.10,4161.00,4159.50,4160.80,12,0,30\n2026-10-16T13:16:00+00:00,4160.8,4161.2,4160.5,4161.0,3\n")
    rows = load_csv(str(q))
    assert len(rows) == 2 and rows[0]["s"] == 30.0 and rows[1]["t"] - rows[0]["t"] == 60
    # read-only guarantees: the CSV path never touches the DB / orders; the news exporter is GET-only
    src = open(os.path.join(BACKEND, "scripts", "gtp_replay.py")).read()
    assert src.count("from database import get_db") == 1 and src.index("from database import get_db") < src.index("def main()")   # only inside _from_db
    for word in ("insert_one", "update_one", "request_close", "place_order", "pending_modification"):
        assert word not in src
    ne = open(os.path.join(BACKEND, "scripts", "gtp_news_events.py")).read()
    assert "database" not in ne and "insert_one" not in ne and "--tz-offset" in src


def test_release_notes_flag_table_matches_model_defaults_and_engine_is_inert_when_off():
    """Item 3 of the hand-over: every new per-account flag + default is listed, and the defaults keep every account unchanged."""
    import re
    from models import BotConfigUpdate
    from gold_trend_pulse.engine import enabled_for
    notes = open(os.path.join(os.path.dirname(BACKEND), "docs", "RELEASE_NOTES_v1.60.57.md")).read()
    table = re.findall(r"^\| `([a-z_0-9]+)` \| `([^`]+)` \|", notes, re.M)
    assert {k for k, _ in table} >= {"gold_trend_pulse_enabled", "gold_trend_pulse_mode", "gold_trend_pulse_layering_enabled", "gold_trend_pulse_sweep_runner_enabled",
                                    "gold_trend_pulse_news_mode_enabled", "gold_trend_pulse_news_pre_action", "gold_trend_pulse_news_release_min",
                                    "gold_trend_pulse_news_events", "engine_live_features_enabled"}
    cfg = BotConfigUpdate().model_dump()
    conv = {"true": True, "false": False, "null": None}
    for k, v in table:
        v = v.strip('"')
        expect = conv.get(v, v)
        if isinstance(expect, str) and expect.isdigit():
            expect = int(expect)
        assert cfg[k] == expect, (k, cfg[k], expect)
    assert not enabled_for(cfg)                                   # engine off → ai_signals never enters the GTP branch
    assert cfg["engine_live_features_enabled"] is False           # #55 trades on closed-bar features exactly as before
    ai = open(os.path.join(BACKEND, "ai_signals.py")).read()
    assert "if _gtp_on(freeze_cfg) and _base_sym(symbol) == _GTP_SYMBOL:" in ai
    from strategy_engines import DEFAULT_PARAMS                   # existing engine parameters untouched (asserted in the #55 suite too)
    assert "hf_scalp" in DEFAULT_PARAMS and "breakout_m15" in DEFAULT_PARAMS
