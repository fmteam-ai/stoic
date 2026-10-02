"""Uncertainty-model upgrade — conformal (ACI), block bootstrap MC, Gaussian
HMM regimes, hierarchical edge shrinkage, triple-barrier meta-labelling,
uniqueness weights and Platt-vs-isotonic selection. Seeded and pure (a tiny
in-memory async collection stub stands in for Mongo)."""
import asyncio
import importlib
import inspect
import sys

import numpy as np
import pytest

pytestmark = pytest.mark.unit


# ───────────────────────────────────────────────────────── fake async db
class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: d.get(key) or "", reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _match(doc, q):
    for k, v in q.items():
        cur = doc
        for part in k.split("."):
            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
        if isinstance(v, dict):
            if "$ne" in v and cur == v["$ne"]:
                return False
            if "$gte" in v and not (cur is not None and cur >= v["$gte"]):
                return False
            if "$in" in v and cur not in v["$in"]:
                return False
        elif cur != v:
            return False
    return True


class _Coll:
    def __init__(self):
        self.docs = []

    def find(self, q=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, q or {})])

    async def find_one(self, q, proj=None):
        for d in self.docs:
            if _match(d, q):
                return d
        return None

    async def update_one(self, q, upd, upsert=False):
        d = await self.find_one(q)
        if d is None:
            d = dict(q)
            self.docs.append(d)
        d.update(upd.get("$set", {}))


class _DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return self._c.setdefault(name, _Coll())


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════ 1 · conformal
def _aci_run(scale_claim, n=4000, seed=0, gamma=0.01):
    from conformal import ACIState
    rng = np.random.default_rng(seed)
    st = ACIState(alpha=0.2, gamma=gamma, alpha_t=0.2, window=250)
    sig = np.where((np.arange(n) // 500) % 2 == 0, 1.0, 2.5)  # vol regimes
    for t in range(n):
        y = rng.normal(0, sig[t])
        half = 1.2816 * sig[t] * scale_claim
        st.update(-half, half, y)
    return st


def test_aci_coverage_converges_for_overconfident_forecaster():
    st = _aci_run(scale_claim=0.5)          # raw band far too narrow
    cov = st.coverage()
    assert cov["long_run_raw"] < 0.6        # raw deciles under-cover
    assert abs(cov["long_run_conformal"] - 0.8) < 0.03   # ACI → 80% target
    assert abs(cov["conformal"] - 0.8) < 0.08            # recent window too
    assert st.margin() > 0                  # band widened


def test_aci_narrows_an_overwide_band():
    st = _aci_run(scale_claim=2.0, seed=1)
    cov = st.coverage()
    assert cov["long_run_raw"] > 0.95
    assert abs(cov["long_run_conformal"] - 0.8) < 0.03
    assert abs(cov["conformal"] - 0.8) < 0.08
    assert st.margin() < 0                  # band narrowed


def test_aci_long_run_bound_full_sequence():
    """Gibbs & Candès: |mean err − α| ≤ (1+γ)/(γT) for the WHOLE run."""
    from conformal import ACIState
    rng = np.random.default_rng(7)
    st = ACIState(alpha=0.2, gamma=0.02, alpha_t=0.2, window=250)
    T = 3000
    for t in range(T):
        y = rng.standard_t(3)
        st.update(-0.5, 0.5, y)
    err = 1 - st.n_cov_conf / st.n_updates
    assert abs(err - 0.2) <= (1 + 0.02) / (0.02 * T) + 1e-9


def test_split_conformal_margin_gives_finite_sample_coverage():
    from conformal import split_conformal_margin
    rng = np.random.default_rng(3)
    y_cal = rng.normal(0, 1, 2000)
    m = split_conformal_margin(np.full(2000, -0.5), np.full(2000, 0.5), y_cal, 0.2)
    y_test = rng.normal(0, 1, 20000)
    cov = np.mean((y_test >= -0.5 - m) & (y_test <= 0.5 + m))
    assert abs(cov - 0.8) < 0.02


def test_aci_state_roundtrip_and_quantile_conformalisation():
    from conformal import ACIState, conformalize_quantiles
    st = _aci_run(scale_claim=0.5, n=300)
    st2 = ACIState.from_doc(st.to_doc())
    assert st2.margin() == pytest.approx(st.margin())
    lv = [0.1, 0.3, 0.5, 0.7, 0.9]
    v = conformalize_quantiles(lv, [98, 99, 100, 101, 102], 1.0)
    assert v[0] == pytest.approx(97) and v[-1] == pytest.approx(103)
    assert v[2] == pytest.approx(100)
    v2 = conformalize_quantiles(lv, [98, 99, 100, 101, 102], -5.0)
    assert all(a <= b for a, b in zip(v2, v2[1:]))     # stays monotone


def _fc(last=100.0, q10=99.0, q90=101.0):
    return {"last": last, "q10": q10, "q50": last, "q90": q90,
            "horizon": 8, "source": "M15", "band_low_pct": -1.0,
            "band_high_pct": 1.0,
            "quantile_levels": [0.1, 0.5, 0.9],
            "quantile_values": [q10, last, q90]}


def test_apply_to_forecast_without_state_behaves_as_before(monkeypatch):
    import conformal
    from forecast_agent import forecast_gate
    monkeypatch.setenv("FORECAST_CONFORMAL_ENABLED", "true")
    db = _DB()
    fc = _fc(q10=100.2, q90=101.0)          # whole band above → SELL veto
    out = _run(conformal.apply_to_forecast(db, "u1", "XAUUSD", fc, [],
                                           now_ts=1_000_000))
    assert out["conformal"]["active"] is False
    assert forecast_gate("SELL", out) == forecast_gate("SELL", fc)
    # one pending forecast persisted for later scoring
    doc = db.forecast_conformal_state.docs[0]
    assert doc["_id"] == "u1:XAUUSD" and len(doc["pending"]) == 1


def test_mature_state_gates_on_conformal_band(monkeypatch):
    import conformal
    from forecast_agent import forecast_gate
    from prob_forecast import trade_eval
    monkeypatch.setenv("CONFORMAL_MIN_UPDATES", "20")
    st = conformal.ACIState(alpha=0.2, gamma=0.01, alpha_t=0.2)
    for _ in range(50):                      # realised far outside the band
        st.update(99.9, 100.1, 103.0)
    fc = _fc(q10=100.2, q90=101.0)           # raw: whole band above
    assert forecast_gate("SELL", fc) is not None
    fc2 = dict(fc, conformal=conformal.conformal_block(fc, st))
    assert fc2["conformal"]["active"] is True
    assert fc2["conformal"]["q10"] < fc["q10"]
    # widened band now straddles the price → no veto
    assert conformal.effective_band(fc2)[0] < 100.0
    assert forecast_gate("SELL", fc2) is None
    pe = trade_eval("XAUUSD", "BUY", 100.0, 99.0, 101.0, fc2)
    assert pe["conformal"] is True


def test_resolve_pending_scores_matured_forecasts():
    import conformal
    st = conformal.ACIState()
    pend = conformal.register_pending([], _fc(), 0.0, 0.0)
    pend = conformal.register_pending(pend, _fc(), 60.0, 0.0)   # same step
    assert len(pend) == 1
    bars = [{"t": 8 * 900 + 1, "c": 105.0}]
    left = conformal.resolve_pending(st, pend, bars, now_ts=10_000)
    assert left == [] and st.n_updates == 1 and st.raw_hits == [0]


# ═════════════════════════════════════════════════ 2 · block bootstrap
def _garch(n=3000, seed=11):
    rng = np.random.default_rng(seed)
    w, a, b = 0.05, 0.12, 0.85
    h, r = 1.0, np.empty(n)
    for t in range(n):
        r[t] = np.sqrt(h) * rng.normal()
        h = w + a * r[t] ** 2 + b * h
    return r


def _acf1(x):
    x = x - x.mean()
    return float((x[1:] * x[:-1]).sum() / (x * x).sum())


def test_block_bootstrap_preserves_volatility_clustering():
    from monte_carlo import bootstrap_indices
    r = _garch()
    target = _acf1(np.abs(r))
    assert target > 0.1                      # clustered by construction
    rng = np.random.default_rng(0)
    out = {}
    for m in ("iid", "block"):
        idx = bootstrap_indices(len(r), 400, 96, rng, method=m, mean_block=12)
        paths = np.abs(r[idx])               # (horizon, paths)
        out[m] = np.mean([_acf1(paths[:, j]) for j in range(paths.shape[1])])
    assert abs(out["block"] - target) < abs(out["iid"] - target)
    assert abs(out["iid"]) < 0.05            # IID destroys clustering
    assert out["block"] > 0.5 * target


def test_simulate_trade_bootstrap_param_and_backcompat():
    from monte_carlo import simulate_trade
    rng = np.random.default_rng(5)
    c = 100 + np.cumsum(_garch(900, seed=3) * 0.1)
    bars = [{"t": i * 900, "o": float(c[i - 1] if i else c[0]),
             "h": float(c[i]) + 0.05, "l": float(c[i]) - 0.05,
             "c": float(c[i])} for i in range(len(c))]
    del rng
    blk = simulate_trade("BUY", c[-1], c[-1] - 1, c[-1] + 1, bars,
                         n_paths=2000, seed=1)
    iid = simulate_trade("BUY", c[-1], c[-1] - 1, c[-1] + 1, bars,
                         n_paths=2000, seed=1, bootstrap="iid")
    assert blk["bootstrap"] == "block" and iid["bootstrap"] == "iid"
    for r in (blk, iid):
        assert abs(r["p_tp_first"] + r["p_sl_first"] + r["p_timeout"] - 1) < 0.01
    # short history → automatic IID fallback, flagged
    short = simulate_trade("BUY", c[-1], c[-1] - 1, c[-1] + 1, bars[-200:],
                           n_paths=500, seed=1)
    assert short["bootstrap"] == "iid" and short["bootstrap_fallback"]


# ═════════════════════════════════════════════════════════ 3 · regimes
def _two_regime_bars(n=600, seed=4):
    rng = np.random.default_rng(seed)
    state = np.zeros(n, dtype=int)
    s = 0
    for t in range(n):
        if rng.random() < 0.015:
            s = 1 - s
        state[t] = s
    sd = np.where(state == 1, 3.0, 0.6)
    c = 2000 + np.cumsum(rng.normal(0, sd))
    bars = []
    for i in range(n):
        o = c[i - 1] if i else c[0]
        bars.append({"t": i * 900, "o": float(o), "c": float(c[i]),
                     "h": float(max(o, c[i]) + 0.2 * sd[i]),
                     "l": float(min(o, c[i]) - 0.2 * sd[i])})
    return bars, state


def test_hmm_recovers_two_separated_regimes():
    from regime_hmm import fit_hmm, forward_filter
    rng = np.random.default_rng(2)
    st = np.repeat([0, 1, 0, 1, 0, 1], 150)
    x = rng.normal(0, np.where(st == 1, 3.0, 0.5))
    p, info = fit_hmm(x, n_states=2)
    assert p.var[1] / p.var[0] > 10
    assert p.A[0, 0] > 0.9 and p.A[1, 1] > 0.9           # sticky
    filt, _ = forward_filter(x, p)
    acc = np.mean((filt[:, 1] > 0.5) == (st == 1))
    assert acc > 0.93
    assert np.allclose(filt.sum(axis=1), 1.0)


def test_regime_probabilities_uses_hmm_with_enough_bars():
    from market_regime import regime_probabilities
    bars, state = _two_regime_bars()
    vol = {"axis": "normal", "ratio": 1.0}
    r = regime_probabilities(bars, "ranging", 0.0, vol, False)
    assert r["source"] == "hmm" and "hmm" in r
    assert set(r["hmm"]["probs"]) == {"calm", "turbulent"}
    assert abs(sum(r["classes"].values()) - 1) < 0.01
    short = regime_probabilities(bars[:60], "ranging", 0.0, vol, False)
    assert short["source"] == "heuristic" and "hmm" not in short


def test_hmm_filtered_prob_tracks_latest_regime():
    from regime_hmm import regime_hmm_probabilities
    bars, state = _two_regime_bars(seed=9)
    out = regime_hmm_probabilities(bars)
    want = "turbulent" if state[-1] == 1 else "calm"
    assert out["state"] == want


def test_shrinkage_pulls_small_cells_toward_strategy_mean():
    from market_regime import estimate_shrink_k, shrunk_edge
    strat = [10.0] * 60 + [-3.0, -4.0]
    small = shrunk_edge([-3.0, -4.0], strat, k=10)
    assert small["weight"] == pytest.approx(2 / 12)
    assert small["shrunk_mean"] > 0 > small["cell_mean"]
    big = shrunk_edge([-3.0] * 200, strat + [-3.0] * 200, k=10)
    assert big["weight"] > 0.95
    assert abs(big["shrunk_mean"] - big["cell_mean"]) < abs(
        small["shrunk_mean"] - small["cell_mean"])
    lo, hi = 2.0, 100.0
    assert lo <= estimate_shrink_k({"a": [1, 2, 3], "b": [1, 2]}) <= hi
    k = estimate_shrink_k({"a": [1.0, 1.1, 0.9] * 5, "b": [-5.0, -5.1, -4.9] * 5,
                           "c": [9.0, 9.1, 8.9] * 5})
    assert k == pytest.approx(lo)            # strongly separated cells


def _seed(db, cls, key, pnls):
    for p in pnls:
        db.trades.docs.append({
            "user_id": "u", "origin": "auto", "status": "closed", "pnl": p,
            "strategy_class": cls, "market_regime": {"key": key},
            "closed_at": "2999-01-01T00:00:00+00:00"})


def test_edge_gate_shrinkage_blocks_only_on_negative_upper_bound():
    from market_regime import strategy_edge
    db = _DB()
    # proven loser across the board → blocked
    _seed(db, "scalp", "ranging|high",
          [-30, -25, -40, -22, -35, -28, -31, -26, -38, -20])
    # strong strategy with a FEW (10) bad trades in one regime: shrunk
    # toward its positive strategy mean → NOT blocked by noise
    _seed(db, "trend", "trending_up|low", [50.0, 60, 45, 70, 55] * 12)
    _seed(db, "trend", "ranging|high", [-10, -12, 5, -8, -9, -11, 4, -7, -10, -6])
    edge = _run(strategy_edge(db, "u", "ranging|high"))
    assert edge["scalp"]["allowed"] is False
    assert "negative edge" in edge["scalp"]["reason"]
    assert edge["scalp"]["upper_bound"] < 0
    tr = edge["trend"]
    assert tr["expectancy"] < 0 and tr["allowed"] is True
    assert tr["shrunk_expectancy"] > tr["expectancy"]
    # few trades → never blocked (min-trades floor), configurable
    db2 = _DB()
    _seed(db2, "scalp", "ranging|low", [-30, -25, -40])
    e2 = _run(strategy_edge(db2, "u", "ranging|low"))
    assert e2["scalp"]["allowed"] is True
    e3 = _run(strategy_edge(db2, "u", "ranging|low", min_trades=3))
    assert e3["scalp"]["allowed"] is False


# ═════════════════════════════════════════════════ 4 · meta-labelling
def _bar(h, low, c=None):
    return {"h": h, "l": low, "c": (h + low) / 2 if c is None else c}


def test_triple_barrier_labels_on_handcrafted_paths():
    from meta_labeling import triple_barrier_label as tb
    # BUY 100, SL 99, TP 102
    assert tb("BUY", 100, 99, 102, [_bar(101, 99.5), _bar(102.1, 100.5)]) == \
        {"label": 1, "barrier": "pt", "bars": 2, "ret_r": 2.0}
    assert tb("BUY", 100, 99, 102, [_bar(100.5, 98.9)])["barrier"] == "sl"
    # same-bar collision → SL (conservative)
    col = tb("BUY", 100, 99, 102, [_bar(102.5, 98.5)])
    assert col["label"] == 0 and col["barrier"] == "sl"
    # time barrier: sign of the R-return
    up = tb("BUY", 100, 99, 102, [_bar(100.8, 99.5, 100.6)] * 3, max_bars=3)
    assert up["barrier"] == "time" and up["label"] == 1 and up["ret_r"] == pytest.approx(0.6)
    dn = tb("BUY", 100, 99, 102, [_bar(100.2, 99.4, 99.6)] * 3, max_bars=3)
    assert dn["label"] == 0
    assert tb("BUY", 100, 99, 102, [_bar(100.2, 99.4, 99.6)] * 3, max_bars=3,
              time_label="drop") is None
    # unresolved (path shorter than the vertical barrier) → None
    assert tb("BUY", 100, 99, 102, [_bar(100.5, 99.5)], max_bars=5) is None
    # SELL mirror
    assert tb("SELL", 100, 101, 98, [_bar(100.5, 97.9)])["label"] == 1
    assert tb("SELL", 100, 101, 98, [_bar(101.2, 99.5)])["label"] == 0
    # invalid geometry
    assert tb("BUY", 100, 101, 102, [_bar(103, 99)]) is None


def test_uniqueness_weights_in_unit_interval():
    from meta_labeling import average_uniqueness, average_uniqueness_times
    rng = np.random.default_rng(0)
    s = rng.integers(0, 500, 300)
    spans = [(int(a), int(a + rng.integers(0, 40))) for a in s]
    u = average_uniqueness(spans)
    assert np.all(u > 0) and np.all(u <= 1)
    ut = average_uniqueness_times([a for a, _ in spans], [b for _, b in spans])
    assert np.all(ut > 0) and np.all(ut <= 1)
    assert np.allclose(average_uniqueness([(0, 4), (5, 9)]), 1.0)
    assert np.allclose(average_uniqueness([(0, 9), (0, 9)]), 0.5)
    assert np.allclose(average_uniqueness_times([0, 0, 5], [10, 10, 15]),
                       [5 / 12, 5 / 12, 2 / 3])


def _calib_data(n, seed, shape="step"):
    rng = np.random.default_rng(seed)
    s = rng.random(n)
    if shape == "step":       # non-sigmoid miscalibration
        p_true = np.where(s > 0.6, 0.85, np.where(s > 0.3, 0.5, 0.15))
    else:                     # already calibrated
        p_true = s
    y = (rng.random(n) < p_true).astype(float)
    return s, y


def test_isotonic_chosen_only_when_heldout_brier_improves():
    from probability_calibrator import apply_calibration, select_calibrator
    s, y = _calib_data(3000, 1, "step")
    c = select_calibrator(s, y)
    sel = c["selection"]
    assert sel["chosen"] == "isotonic" and c["method"] == "isotonic"
    bs = sel["brier_select"]
    assert bs["isotonic"] < bs["platt"] and bs["isotonic"] < bs["raw"]
    assert 0 <= apply_calibration(0.7, c) <= 1
    # whenever isotonic is NOT strictly better, Platt ships
    for seed in range(5):
        s2, y2 = _calib_data(600, 10 + seed, "calibrated")
        c2 = select_calibrator(s2, y2)
        b2 = c2["selection"]["brier_select"]
        if c2["method"] == "isotonic":
            assert b2["isotonic"] < min(b2["platt"], b2["raw"])
        else:
            assert c2["selection"]["chosen"] == "platt"
    # too few points → isotonic never considered
    s3, y3 = _calib_data(150, 3, "step")
    c3 = select_calibrator(s3, y3)
    assert c3["method"] == "platt"
    assert c3["selection"]["isotonic_eligible"] is False


def test_numpy_pav_matches_isotonic_definition():
    from probability_calibrator import _pav
    x = np.array([0.1, 0.2, 0.3, 0.4, 0.5])
    y = np.array([0, 1, 0, 1, 1], float)
    xt, yt = _pav(x, y)
    fitted = np.interp(x, xt, yt)
    assert np.all(np.diff(fitted) >= -1e-12)
    assert fitted == pytest.approx([0, 0.5, 0.5, 1, 1])


def test_xgb_trained_without_scale_pos_weight():
    import learned_meta
    src = inspect.getsource(learned_meta._train_xgb)
    assert '"scale_pos_weight"' not in src


def test_learned_meta_dataset_query_scoped():
    from learned_meta import _dataset_query, uniqueness_weights
    assert "user_id" not in _dataset_query()
    q = _dataset_query("u1", "acc9")
    assert q["user_id"] == "u1" and q["account_id"] == "acc9"
    w = uniqueness_weights(["2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00", None],
                           ["2026-01-01T01:00:00+00:00",
                            "2026-01-01T01:00:00+00:00", None])
    assert w.tolist() == [0.5, 0.5, 1.0]


def _meta_world(n_events=160, seed=5):
    rng = np.random.default_rng(seed)
    n = 4000
    c = 2000 + np.cumsum(rng.normal(0, 1.0, n))
    bars = [{"t": 1_700_000_000 + i * 900, "o": float(c[i - 1] if i else c[0]),
             "h": float(c[i] + abs(rng.normal(0, 0.4))),
             "l": float(c[i] - abs(rng.normal(0, 0.4))), "c": float(c[i])}
            for i in range(n)]
    events = []
    for k, i in enumerate(sorted(rng.choice(np.arange(150, n - 200),
                                            n_events, replace=False))):
        act = "BUY" if rng.random() < 0.5 else "SELL"
        e = c[i]
        sl, tp = (e - 3, e + 4.5) if act == "BUY" else (e + 3, e - 4.5)
        events.append({"symbol": "XAUUSD", "ts": bars[i]["t"] + 1,
                       "executed": k % 3 != 0,
                       "signal": {"action": act, "entry_price": float(e),
                                  "stop_loss": float(sl), "tp1": float(tp),
                                  "confidence": float(rng.uniform(55, 90))}})
    return events, {"XAUUSD": bars}


def test_meta_labeling_pipeline_includes_vetoed_and_trains():
    import meta_labeling as ml
    events, candles = _meta_world()
    rows = ml.label_events(events, candles)
    assert len(rows) > 100
    assert any(not r["executed"] for r in rows)          # vetoed included
    assert {r["barrier"] for r in rows} <= {"pt", "sl", "time"}
    art = ml.train_meta_model(rows)
    assert art["trained"] and art["n_vetoed"] > 0
    assert 0 < art["mean_uniqueness"] <= 1
    feats = ml.meta_features(events[-1]["signal"],
                             candles["XAUUSD"][-130:], events[-1]["ts"])
    out = ml.predict_meta(art, feats)
    assert 0 <= out["p_success"] <= 1


def test_meta_labeler_learned_entrypoint_falls_back():
    import meta_labeler
    res = _run(meta_labeler.predict_true_signal_probability_learned(
        _DB(), "u", {"action": "BUY", "confidence": 70}, [],
        sentiment={"score": 0.0}))
    assert res["source"] == "hand_weighted_deprecated"
    none = _run(meta_labeler.predict_true_signal_probability_learned(
        _DB(), "u", {"action": "BUY"}, []))
    assert none["p_true"] is None


# ═════════════════════════════════════════════ 5 · forecaster evaluation
def test_eval_script_importable_without_running_or_torch(capsys):
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(path))
    try:
        had_torch = "torch" in sys.modules
        ef = importlib.import_module("eval_forecasters")
        assert capsys.readouterr().out == ""                  # no run on import
        assert had_torch or "torch" not in sys.modules
        rng = np.random.default_rng(0)
        closes = 100 + np.cumsum(rng.normal(0, 0.2, 700))
        r = ef.evaluate(closes, "naive", horizon=4, min_context=200, step=5)
        assert r["origins"] > 50 and r["crps_bp"] > 0
        assert 0.6 < r["coverage_raw"] < 0.95
        assert abs(r["coverage_aci"] - 0.8) < 0.1
        # pinball / CRPS sanity: a sharp correct forecast beats a wide one
        lv = ef.QUANTILE_LEVELS
        sharp = ef.crps_from_quantiles([1.0] * len(lv), lv, 1.0)
        wide = ef.crps_from_quantiles(np.linspace(0, 2, len(lv)), lv, 1.0)
        assert sharp == 0 and wide > 0
    finally:
        sys.path.remove(str(path))
