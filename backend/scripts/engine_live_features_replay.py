"""#55 — engine replay: old (closed-bar) vs new (live-feature) engine output on XAUUSD M1.

  python scripts/engine_live_features_replay.py --csv XAUUSD_M1.csv --from 2026-10-16T13:00 --to 2026-10-16T14:30
  python scripts/engine_live_features_replay.py --csv XAUUSD_M1.csv --days 30
Per M1 bar: M15 bars closed so far + the bar close as the LIVE price → hf_scalp / hf_scalp_fast / breakout_m15 signals
from both feature packs. Outcomes use the engines' own geometry (scalp SL 0.8×ATR15 min 20 pips, TP 2×SL; breakout
SL 1.0×ATR15 min 25 pips, TP 2×SL) with spread 0.25 + commission, so choppy-day losses are visible."""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENGINES = ("hf_scalp", "hf_scalp_fast", "breakout_m15")
PIP = 0.10


def replay_engines(m1: list, *, spread: float = 0.25, commission_price: float = 0.07, start: float | None = None, end: float | None = None,
                   max_hold_min: int = 90) -> dict:
    from gold_trend_pulse.rules import resample
    from intraday_features import compute_intraday_features
    from strategy_engines import run_engine
    m15_all = resample(m1, 900)
    signals = {"old": [], "new": []}
    open_pos = {"old": {}, "new": {}}
    for i, bar in enumerate(m1):
        t = int(bar["t"]) + 60
        now = datetime.fromtimestamp(t, tz=timezone.utc)
        closed15 = [b for b in m15_all if b["t"] + 900 <= t][-120:]
        if len(closed15) < 60:
            continue
        # manage open positions (one per engine per arm)
        for arm in ("old", "new"):
            for eng, pos in list(open_pos[arm].items()):
                sgn = 1 if pos["action"] == "BUY" else -1
                hit_sl = (bar["l"] <= pos["sl"]) if sgn > 0 else (bar["h"] >= pos["sl"])
                hit_tp = (bar["h"] >= pos["tp"]) if sgn > 0 else (bar["l"] <= pos["tp"])
                px = pos["sl"] if hit_sl else pos["tp"] if hit_tp else (bar["c"] if (t - pos["t"]) >= max_hold_min * 60 else None)
                if px is not None:
                    net = (px - pos["entry"]) * sgn - spread - commission_price
                    pos.update({"exit": px, "net": round(net, 2), "r": round(net / pos["sl_dist"], 3), "exit_at": now.isoformat()})
                    del open_pos[arm][eng]
        if start and t < start or end and t > end:
            continue
        if now.weekday() >= 5 or not (7 <= now.hour < 20):
            continue
        m5 = resample([b for b in m1[max(0, i - 20):i + 1]], 300)
        packs = {"old": compute_intraday_features(closed15, as_of=t), "new": compute_intraday_features(closed15, live_price=bar["c"], m5_bars=m5, as_of=t)}
        for arm, feats in packs.items():
            if not feats:
                continue
            for eng in ENGINES:
                if eng in open_pos[arm]:
                    continue
                sig, note = run_engine(eng, feats)
                if not sig:
                    continue
                atr = float(feats["atr15"])
                sl_dist = max(0.8 * atr, 2.0) if eng != "breakout_m15" else max(1.0 * atr, 2.5)
                sgn = 1 if sig == "BUY" else -1
                pos = {"engine": eng, "action": sig, "note": note[:120], "t": t, "at": now.isoformat(), "entry": bar["c"], "sl_dist": round(sl_dist, 2),
                       "sl": round(bar["c"] - sgn * sl_dist, 2), "tp": round(bar["c"] + sgn * 2 * sl_dist, 2), "trend": feats.get("trend"),
                       "gap": feats.get("feature_price_gap"), "stale": feats.get("stale_feature")}
                open_pos[arm][eng] = pos
                signals[arm].append(pos)
    return summarize(signals)


def summarize(signals: dict) -> dict:
    out = {}
    for arm, rows in signals.items():
        done = [r for r in rows if "r" in r]
        by_eng = defaultdict(list)
        for r in done:
            by_eng[r["engine"]].append(r)

        def _st(xs):
            rs = [x["r"] for x in xs]
            w = [r for r in rs if r > 0]; l = [r for r in rs if r <= 0]
            return {"n": len(rs), "win_rate": round(len(w) / len(rs), 3) if rs else None,
                    "pf": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None, "exp_r": round(sum(rs) / len(rs), 3) if rs else None,
                    "net_r": round(sum(rs), 2)}
        out[arm] = {"signals": len(rows), "buy": sum(1 for r in rows if r["action"] == "BUY"), "sell": sum(1 for r in rows if r["action"] == "SELL"),
                    "closed": _st(done), "by_engine": {k: _st(v) for k, v in sorted(by_eng.items())},
                    "stale_entries": sum(1 for r in rows if r.get("stale")), "list": rows}
    return out


def markdown(rep: dict, title: str) -> str:
    rows = [f"### {title}", "", "| arm | signals | BUY | SELL | closed n | win rate | PF | exp R | net R | stale entries |", "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in ("old", "new"):
        a = rep[arm]; c = a["closed"]
        rows.append(f"| {arm} | {a['signals']} | {a['buy']} | {a['sell']} | {c['n']} | {c['win_rate']} | {c['pf']} | {c['exp_r']} | {c['net_r']} | {a['stale_entries']} |")
    rows += ["", "| arm | engine | n | win rate | PF | exp R |", "|---|---|---|---|---|---|"]
    for arm in ("old", "new"):
        for eng, st in rep[arm]["by_engine"].items():
            rows.append(f"| {arm} | {eng} | {st['n']} | {st['win_rate']} | {st['pf']} | {st['exp_r']} |")
    rows += ["", "signals (new arm):"] + [f"- {r['at']} {r['engine']} {r['action']} @ {r['entry']} trend={r['trend']} gap={r['gap']} — {r['note']}" for r in rep["new"]["list"][:40]]
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True); ap.add_argument("--from", dest="start"); ap.add_argument("--to", dest="end")
    ap.add_argument("--days", type=int, default=0); ap.add_argument("--spread", type=float, default=0.25); ap.add_argument("--commission", type=float, default=0.07)
    a = ap.parse_args()
    from gold_trend_pulse.replay import load_csv
    bars = load_csv(a.csv)
    if a.days:
        bars = [b for b in bars if b["t"] >= bars[-1]["t"] - a.days * 86400]
    s = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc).timestamp() if a.start else None
    e = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc).timestamp() if a.end else None
    rep = replay_engines(bars, spread=a.spread, commission_price=a.commission, start=s, end=e)
    print(markdown(rep, f"#55 engine replay {a.start or ''}–{a.end or ''} ({len(bars)} M1 bars)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
