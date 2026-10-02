"""Meta-labelling (López de Prado, AFML ch. 3-4) for primary BUY/SELL signals.

The primary engine decides the SIDE; the meta-model estimates
P(the primary signal succeeds) so gates/sizing can act on it.

Pipeline
========
1. **Events** — every primary signal the bot produced, INCLUDING ones a
   gate vetoed (db.trade_decisions, status ``rejected``) as well as those
   executed (``executed``; db.signals as a fallback source). Vetoed signals
   are labelled counterfactually from the candle path that followed — the
   only way the model ever learns whether a veto was right.
2. **Triple-barrier labels** — walk M15 candles (db.intraday_candles) from
   the entry bar: profit-taking barrier (TP), stop-loss barrier (SL), and a
   vertical time barrier (max_bars). Label 1 = TP first, 0 = SL first;
   on the time barrier the label is the sign of the R-return (configurable).
   Same-bar TP/SL collisions count as SL (conservative, as in monte_carlo).
3. **Sample weights by uniqueness** — labels overlapping in time share
   information; each sample is weighted by its average uniqueness
   ū_i = mean_{t∈[t0_i,t1_i]} 1/c_t  (c_t = #labels alive at t), in (0, 1].
4. **Model** — small L2 logistic regression on candle-derived features
   (available identically for executed and vetoed signals), purged
   walk-forward OOS predictions, then calibration chosen between Platt and
   isotonic by held-out Brier (probability_calibrator.select_calibrator).
   No class re-weighting: outputs are used as probabilities.

Everything above the async loaders is pure and unit-tested.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger("meta_labeling")

DEFAULT_MAX_BARS = 96           # 24h of M15 — same horizon as monte_carlo
BAR_SECONDS = 900
MIN_TRAIN = 40
ARTIFACT_COLLECTION = "meta_label_artifacts"
FEATURE_NAMES = ["confidence", "is_buy", "rr_norm", "sl_atr", "momentum_dir",
                 "vol_ratio", "sess_asia", "sess_london", "sess_ny",
                 "mc_ev_r_net", "has_mc"]
L2 = 0.5
EPOCHS = 500
LR = 0.1


# ─────────────────────────────────────────────────────── triple barrier
def triple_barrier_label(action: str, entry: float, sl: float, tp: float,
                         bars: list, max_bars: int = DEFAULT_MAX_BARS,
                         time_label: str = "sign") -> dict | None:
    """Label one event from the bars FOLLOWING entry (bars[0] = first bar
    after the entry bar). Returns {label, barrier, bars, ret_r} or None
    when the path is too short to resolve any barrier.

    time_label: "sign" → 1 if the time-barrier R-return > 0 else 0;
                "loss" → time barrier counts as 0; "drop" → None."""
    if action not in ("BUY", "SELL"):
        return None
    try:
        entry, sl, tp = float(entry), float(sl), float(tp)
    except (TypeError, ValueError):
        return None
    risk = abs(entry - sl)
    if risk <= 0 or abs(tp - entry) <= 0:
        return None
    is_buy = action == "BUY"
    if is_buy and not (sl < entry < tp):
        return None
    if not is_buy and not (tp < entry < sl):
        return None
    path = list(bars or [])[:max_bars]
    for i, b in enumerate(path):
        h, lo = float(b["h"]), float(b["l"])
        hit_sl = lo <= sl if is_buy else h >= sl
        hit_tp = h >= tp if is_buy else lo <= tp
        if hit_sl:                                   # collision → SL first
            return {"label": 0, "barrier": "sl", "bars": i + 1,
                    "ret_r": -1.0}
        if hit_tp:
            return {"label": 1, "barrier": "pt", "bars": i + 1,
                    "ret_r": round(abs(tp - entry) / risk, 4)}
    if len(path) < max_bars:
        return None                                  # unresolved yet
    last = float(path[-1]["c"])
    ret_r = ((last - entry) if is_buy else (entry - last)) / risk
    if time_label == "drop":
        return None
    lab = (1 if ret_r > 0 else 0) if time_label == "sign" else 0
    return {"label": lab, "barrier": "time", "bars": len(path),
            "ret_r": round(ret_r, 4)}


def entry_index(bars: list, ts: float) -> int | None:
    """Index of the bar that CONTAINS ts (last bar with t ≤ ts)."""
    idx = None
    for i, b in enumerate(bars or []):
        t = float(b.get("t") or 0)
        if t <= ts:
            idx = i
        else:
            break
    return idx


# ──────────────────────────────────────────────────────────── uniqueness
def average_uniqueness(spans) -> np.ndarray:
    """Discrete-grid average uniqueness. spans = [(start_idx, end_idx)]
    inclusive bar indices on a common grid. Returns values in (0, 1]."""
    spans = [(int(s), int(max(e, s))) for s, e in spans]
    if not spans:
        return np.array([])
    lo = min(s for s, _ in spans)
    hi = max(e for _, e in spans)
    conc = np.zeros(hi - lo + 2)
    for s, e in spans:
        conc[s - lo] += 1
        conc[e - lo + 1] -= 1
    conc = np.cumsum(conc)[:-1]
    out = np.empty(len(spans))
    for i, (s, e) in enumerate(spans):
        out[i] = float(np.mean(1.0 / conc[s - lo:e - lo + 1]))
    return out


def average_uniqueness_times(starts, ends) -> np.ndarray:
    """Continuous-time average uniqueness for intervals [start, end]
    (e.g. trade entered_at → closed_at as epoch seconds). A zero-length
    interval gets 1 / (#intervals alive at that instant)."""
    st = np.asarray(starts, float)
    en = np.maximum(np.asarray(ends, float), st)
    n = len(st)
    if n == 0:
        return np.array([])
    pts = np.unique(np.concatenate([st, en]))
    out = np.empty(n)
    # concurrency on each elementary segment [pts[j], pts[j+1])
    seg_len = np.diff(pts)
    # concurrency on segment j = #starts ≤ pts[j] − #ends ≤ pts[j]
    delta = np.zeros(len(pts))
    np.add.at(delta, np.searchsorted(pts, st), 1)
    np.add.at(delta, np.searchsorted(pts, en), -1)
    conc_seg = np.cumsum(delta)[:-1]
    # inverse-concurrency × length, prefix-summed for O(1) interval lookups
    inv = np.where(conc_seg > 0, seg_len / np.maximum(conc_seg, 1), 0.0)
    cum = np.concatenate([[0.0], np.cumsum(inv)])
    i_st = np.searchsorted(pts, st)
    i_en = np.searchsorted(pts, en)
    for i in range(n):
        if en[i] <= st[i]:
            c = int(((st <= st[i]) & (en >= st[i])).sum())
            out[i] = 1.0 / max(c, 1)
            continue
        out[i] = float((cum[i_en[i]] - cum[i_st[i]]) / (en[i] - st[i]))
    return out


# ─────────────────────────────────────────────────────────────── features
def _atr(bars: list, n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(max(1, len(bars) - n), len(bars)):
        h, lo, pc = float(bars[i]["h"]), float(bars[i]["l"]), float(bars[i - 1]["c"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def _session(ts: float) -> str:
    h = datetime.fromtimestamp(ts, tz=timezone.utc).hour
    if h < 7:
        return "ASIA"
    if h < 13:
        return "LONDON"
    if h < 22:
        return "NY"
    return "OFF"


def meta_features(sig: dict, bars_before: list, ts: float) -> list | None:
    """Feature row from a signal (full doc OR decision-ledger snapshot) and
    the candles up to and including the entry bar — identical for executed
    and vetoed signals, so the model never learns 'was it vetoed'."""
    action = sig.get("action")
    if action not in ("BUY", "SELL") or len(bars_before) < 20:
        return None
    try:
        entry = float(sig.get("entry_price") or bars_before[-1]["c"])
        sl = float(sig.get("stop_loss"))
    except (TypeError, ValueError):
        return None
    is_buy = 1.0 if action == "BUY" else 0.0
    sign = 1.0 if is_buy else -1.0
    atr = _atr(bars_before) or 1e-9
    closes = [float(b["c"]) for b in bars_before]
    mom = sign * (closes[-1] - closes[-17 if len(closes) >= 17 else 0]) / (atr * 4.0)
    atrs = [_atr(bars_before[max(0, i - 15):i + 1])
            for i in range(15, len(bars_before), 4)]
    med = float(np.median(atrs)) if atrs else atr
    vol_ratio = atr / med if med > 0 else 1.0
    rr = float(sig.get("rr_ratio") or 0.0)
    if rr <= 0:
        tp = sig.get("tp1") or sig.get("take_profit")
        try:
            rr = abs(float(tp) - entry) / max(abs(entry - sl), 1e-9)
        except (TypeError, ValueError):
            rr = 0.0
    mc = sig.get("mc") or sig.get("monte_carlo") or {}
    ev = mc.get("ev_r_net", mc.get("ev_r"))
    sess = _session(ts)
    return [float(sig.get("confidence") or 50.0) / 100.0, is_buy,
            min(max(rr, 0.0), 6.0) / 3.0,
            min(abs(entry - sl) / atr, 10.0),
            max(-3.0, min(3.0, mom)),
            max(0.0, min(4.0, vol_ratio)),
            1.0 if sess == "ASIA" else 0.0,
            1.0 if sess == "LONDON" else 0.0,
            1.0 if sess == "NY" else 0.0,
            max(-2.0, min(2.0, float(ev))) if ev is not None else 0.0,
            1.0 if ev is not None else 0.0]


def _ts(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).timestamp()
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return None


def label_events(events: list, candles: dict, max_bars: int = DEFAULT_MAX_BARS,
                 time_label: str = "sign") -> list:
    """events: [{symbol, ts, signal, executed}] · candles: {symbol: bars}.
    Returns labelled rows with features, label, span (bar indices) and
    times, sorted by event time. Unresolvable events are skipped."""
    rows = []
    for ev in events:
        bars = candles.get(ev["symbol"]) or []
        if not bars:
            continue
        i0 = entry_index(bars, ev["ts"])
        if i0 is None:
            continue
        sig = ev["signal"]
        tp = sig.get("tp1") or sig.get("take_profit")
        lab = triple_barrier_label(sig.get("action"), sig.get("entry_price"),
                                   sig.get("stop_loss"), tp,
                                   bars[i0 + 1:], max_bars=max_bars,
                                   time_label=time_label)
        if lab is None:
            continue
        feats = meta_features(sig, bars[max(0, i0 - 120):i0 + 1], ev["ts"])
        if feats is None:
            continue
        i1 = i0 + lab["bars"]
        rows.append({"x": feats, "y": lab["label"], "barrier": lab["barrier"],
                     "ret_r": lab["ret_r"], "symbol": ev["symbol"],
                     "t0": float(bars[i0]["t"]),
                     "t1": float(bars[min(i1, len(bars) - 1)]["t"]),
                     "executed": bool(ev.get("executed")), "ts": ev["ts"]})
    rows.sort(key=lambda r: r["ts"])
    return rows


# ──────────────────────────────────────────────────────────────── model
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logreg(X, y, w=None, l2: float = L2, epochs: int = EPOCHS,
               lr: float = LR) -> dict:
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    n, d = X.shape
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-9
    Xb = np.hstack([(X - mu) / sd, np.ones((n, 1))])
    sw = np.ones(n) if w is None else np.asarray(w, float)
    sw = sw / max(float(sw.mean()), 1e-12)
    beta = np.zeros(d + 1)
    for _ in range(epochs):
        p = _sigmoid(Xb @ beta)
        g = (Xb.T @ ((p - y) * sw)) / n + l2 * np.concatenate([beta[:-1], [0]]) / n
        beta -= lr * g
    return {"weights": beta.tolist(), "mu": mu.tolist(), "sd": sd.tolist()}


def predict_logreg(model: dict, X) -> np.ndarray:
    X = np.atleast_2d(np.asarray(X, float))
    Xb = np.hstack([(X - np.asarray(model["mu"])) / np.asarray(model["sd"]),
                    np.ones((len(X), 1))])
    return _sigmoid(Xb @ np.asarray(model["weights"]))


def purged_walk_forward(X, y, w, t0, t1, n_folds: int = 4,
                        min_train: int = 20):
    """Expanding-window OOS predictions with PURGING: a training sample is
    dropped when its label window [t0, t1] reaches into the test fold
    (t1 ≥ first test t0) — prevents leakage from overlapping labels."""
    n = len(y)
    start = max(min_train, int(n * 0.4))
    if n - start < 10:
        return None, None
    bounds = np.linspace(start, n, n_folds + 1).astype(int)
    idx, preds = [], []
    for k in range(n_folds):
        lo, hi = int(bounds[k]), int(bounds[k + 1])
        if hi <= lo:
            continue
        keep = np.arange(lo)[t1[:lo] < t0[lo]]
        if len(keep) < min_train or len(set(y[keep].tolist())) < 2:
            continue
        m = fit_logreg(X[keep], y[keep], w[keep])
        idx.extend(range(lo, hi))
        preds.extend(predict_logreg(m, X[lo:hi]).tolist())
    if len(preds) < 10:
        return None, None
    return np.asarray(idx), np.asarray(preds)


def train_meta_model(rows: list) -> dict:
    """Fit the meta-model on labelled rows (chronological)."""
    from probability_calibrator import (apply_calibration, brier_score,
                                        expected_calibration_error,
                                        select_calibrator)
    n = len(rows)
    if n < MIN_TRAIN:
        return {"trained": False, "reason": f"need ≥{MIN_TRAIN} labelled "
                                             f"events (have {n})", "n": n}
    X = np.array([r["x"] for r in rows], float)
    y = np.array([r["y"] for r in rows], float)
    if len(set(y.tolist())) < 2:
        return {"trained": False, "reason": "single-class labels", "n": n}
    t0 = np.array([r["t0"] for r in rows])
    t1 = np.array([r["t1"] for r in rows])
    order = np.argsort(t0, kind="mergesort")
    X, y, t0, t1 = X[order], y[order], t0[order], t1[order]
    spans = list(zip(t0, t1))
    w = average_uniqueness_times([s for s, _ in spans], [e for _, e in spans])
    model = fit_logreg(X, y, w)
    oos_idx, oos_p = purged_walk_forward(X, y, w, t0, t1)
    if oos_p is not None and len(set(y[oos_idx].tolist())) == 2:
        calib = select_calibrator(oos_p, y[oos_idx])
        p_cal = np.array([apply_calibration(float(v), calib) for v in oos_p])
        diag = {"source": "purged_walk_forward_oos", "n_oos": int(len(oos_p)),
                "brier_raw": round(brier_score(oos_p, y[oos_idx]), 4),
                "brier_calibrated": round(brier_score(p_cal, y[oos_idx]), 4),
                "ece_raw": expected_calibration_error(oos_p, y[oos_idx]),
                "ece_calibrated": expected_calibration_error(p_cal, y[oos_idx])}
    else:
        calib = {"skipped": True, "reason": "too few OOS predictions"}
        diag = {"source": "none"}
    return {"trained": True, "n": n, "n_pos": int(y.sum()),
            "n_vetoed": int(sum(1 for r in rows if not r["executed"])),
            "mean_uniqueness": round(float(w.mean()), 4),
            "model": model, "calibration": calib, "diagnostics": diag,
            "feature_names": FEATURE_NAMES,
            "trained_at": datetime.now(timezone.utc).isoformat()}


def predict_meta(art: dict | None, feats: list | None) -> dict | None:
    if not art or not art.get("trained") or feats is None:
        return None
    from probability_calibrator import apply_calibration
    p = float(predict_logreg(art["model"], [feats])[0])
    pc = apply_calibration(p, art.get("calibration"))
    return {"p_success": round(pc, 4), "p_raw": round(p, 4),
            "calibration": (art.get("calibration") or {}).get("method",
                                                               "platt"),
            "n_train": art.get("n")}


# ───────────────────────────────────────────────────────── async loaders
async def load_events(db, user_id: str, account_id: str | None = None,
                      limit: int = 5000) -> list:
    """Primary signals for one user (optionally one broker account):
    executed AND vetoed decisions from db.trade_decisions, plus db.signals
    not already covered (deduped on symbol+action+minute)."""
    from pip_utils import base_symbol
    q = {"user_id": user_id, "status": {"$in": ["rejected", "executed"]},
         "signal.action": {"$in": ["BUY", "SELL"]}}
    if account_id:
        q["account_id"] = account_id
    events, seen = [], set()
    async for d in db.trade_decisions.find(q).sort("ts", -1).limit(limit):
        ts = _ts(d.get("ts"))
        sig = d.get("signal") or {}
        if ts is None or not sig.get("stop_loss"):
            continue
        sym = base_symbol(d.get("symbol") or "")
        key = (sym, sig.get("action"), int(ts // 60))
        seen.add(key)
        events.append({"symbol": sym, "ts": ts, "signal": sig,
                       "executed": d.get("status") == "executed"})
    sq = {"user_id": user_id, "action": {"$in": ["BUY", "SELL"]}}
    if account_id:
        sq["account_id"] = account_id
    async for s in db.signals.find(sq).sort("created_at", -1).limit(limit):
        ts = _ts(s.get("created_at"))
        if ts is None or not s.get("stop_loss"):
            continue
        sym = base_symbol(s.get("symbol") or "")
        key = (sym, s.get("action"), int(ts // 60))
        if key in seen:
            continue
        seen.add(key)
        events.append({"symbol": sym, "ts": ts, "signal": s,
                       "executed": bool(s.get("consumed"))})
    return events


async def load_candles(db, user_id: str, symbols) -> dict:
    out = {}
    for sym in set(symbols):
        doc = await db.intraday_candles.find_one(
            {"user_id": user_id, "symbol": sym, "timeframe": "M15"},
            {"bars": 1}) or await db.intraday_candles.find_one(
            {"user_id": user_id, "symbol": sym}, {"bars": 1})
        bars = (doc or {}).get("bars") or []
        out[sym] = sorted(bars, key=lambda b: float(b.get("t") or 0))
    return out


async def retrain(db, user_id: str, account_id: str | None = None,
                  max_bars: int = DEFAULT_MAX_BARS) -> dict:
    events = await load_events(db, user_id, account_id)
    candles = await load_candles(db, user_id, [e["symbol"] for e in events])
    rows = label_events(events, candles, max_bars=max_bars)
    art = train_meta_model(rows)
    art["user_id"], art["account_id"] = user_id, account_id
    art["n_events"] = len(events)
    if art.get("trained"):
        await db[ARTIFACT_COLLECTION].update_one(
            {"_id": f"{user_id}:{account_id or '*'}"}, {"$set": art},
            upsert=True)
    return {k: v for k, v in art.items() if k != "model"}


async def predict(db, user_id: str, signal: dict, bars: list,
                  account_id: str | None = None,
                  ts: float | None = None) -> dict | None:
    """Calibrated P(success) for a live signal, or None (fail-open)."""
    try:
        art = await db[ARTIFACT_COLLECTION].find_one(
            {"_id": f"{user_id}:{account_id or '*'}"}) or (
            await db[ARTIFACT_COLLECTION].find_one({"_id": f"{user_id}:*"})
            if account_id else None)
        ts = ts if ts is not None else (
            float(bars[-1]["t"]) if bars else datetime.now(timezone.utc).timestamp())
        return predict_meta(art, meta_features(signal, bars[-121:], ts))
    except Exception as e:  # noqa: BLE001
        logger.debug("meta-label predict failed: %s", e)
        return None

