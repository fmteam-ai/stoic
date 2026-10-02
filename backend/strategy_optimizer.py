"""Strategy Optimizer — grid-search variants of a DSL and rank by backtest.

Honest about what it does: since the user doesn't have historical indicator
snapshots, the optimizer can't truthfully replay different RSI thresholds
against bars. Instead, it varies the **filter set** the existing backtest
engine can actually evaluate against closed-trade history:

  • session_preference     ∈ {london, ny, tokyo, any}
  • symbol subset          ∈ powerset({XAUUSD, BTCUSD}) minus empty
  • lookback days          ∈ {14, 30, 60}

For each combination it runs `strategy_backtest.run_backtest` and ranks by
the shared profit-tied objective (objective.stoic_score, iter-42):
    score = win_rate × avg_profit_per_trade × ln(1 + matched_trades)

Win rate and profit are TIED: a variant that wins often but loses money
scores negative and can never be selected. The optimizer returns the top
variant + the delta vs. the baseline so the UI can show "improvement found".

IMPORTANT — selection bias: `best` is the max over ~36+ variants scored on
the SAME trades, i.e. an IN-SAMPLE figure. A separate validation stage
(`validate_selection`) re-runs the selection on trades closed BEFORE a
chronological cutoff only, then scores the chosen variant and the baseline
on the trades closed AFTER it (OOS, purged of trades straddling the
cutoff). `accepted` is True only if the shared gate passes
(validation.selection_gate: OOS gain > 0, ≥ TUNER_MIN_OOS_TRADES OOS trades,
Deflated Sharpe ≥ TUNER_MIN_DSR, PBO ≤ TUNER_MAX_PBO with enough trials).
`recommended` is `best` when accepted, else the baseline. Realized broker
P&L already includes spread/commission/swap, so no extra cost is charged.

Output:
  {
    "baseline": {filters, win_rate, total_pnl_usd, matched_trades, score},
    "best":     {filters, win_rate, total_pnl_usd, matched_trades, score},
    "improvement_pct": float,   # 0 if best == baseline (IN-SAMPLE)
    "accepted": bool,           # passed the OOS/DSR/PBO selection gate
    "recommended": {...},       # best if accepted else baseline
    "validation": {...},        # OOS split, DSR, PBO, gate checks
    "tested_variants": int,
    "variants": [ {filters, score, win_rate, matched_trades, ...}, ... ],
    "notes": [str, ...],
  }
"""
import asyncio
import math
import logging
from datetime import datetime, timedelta, timezone
from itertools import product

import numpy as np

from strategy_backtest import run_backtest, _parse_iso, _trade_hour_utc, \
    _in_session
from objective import stoic_score
from validation import (dsr_from_trials, probability_of_backtest_overfitting,
                        selection_gate, sharpe)

logger = logging.getLogger("strategy-optimizer")


SESSION_VARIANTS  = ["london", "ny", "tokyo", "any"]
LOOKBACK_VARIANTS = [14, 30, 60]
MIN_IS_TRADES = 5          # in-sample ranking floor (OOS floor: env gate)
OOS_DAYS = 18              # last ~30% of the 60d horizon is held out
PBO_BLOCKS = 8
LOAD_TIMEOUT_S = 15.0


def _filter(trades: list, symbols: list, session: str) -> list:
    syms = {s.upper() for s in symbols}
    out = []
    for t in trades:
        if syms and str(t.get("symbol") or "").upper() not in syms:
            continue
        if session != "any":
            h = _trade_hour_utc(t)
            if h is None or not _in_session(h, session):
                continue
        out.append(t)
    return out


def _stats(trades: list) -> dict:
    pnls = [float(t.get("pnl") or 0) for t in trades]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    res = {"win_rate": round(wins / n, 4) if n else None,
           "total_pnl_usd": round(sum(pnls), 2), "matched_trades": n}
    res["score"] = _score(res)
    s = sharpe(pnls)
    res["sharpe"] = round(s, 4) if s is not None else None
    return res


def validate_selection(trades: list, symbols: list, baseline_session: str,
                       now: datetime | None = None,
                       oos_days: int = OOS_DAYS) -> dict:
    """Pure, honest re-run of the grid selection.

    trades  closed trades (dicts with closed_at, opened_at, symbol, pnl)
    The grid is ranked on trades closed BEFORE `now − oos_days` only; the
    winner and the baseline are then scored on trades closed after the
    cutoff (trades opened before the cutoff are purged from OOS: their
    outcome straddles the boundary). Returns DSR / PBO / gate verdict."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=oos_days)
    horizon = max(LOOKBACK_VARIANTS)
    rows = []
    for t in trades:
        c = _parse_iso(t.get("closed_at"))
        if c is None or t.get("pnl") is None:
            continue
        o = _parse_iso(t.get("opened_at")) or _parse_iso(t.get("created_at")) or c
        rows.append((c, o, t))
    rows.sort(key=lambda r: r[0])
    is_rows = [r for r in rows if r[0] < cutoff]
    oos_trades = [t for c, o, t in rows if c >= cutoff and o >= cutoff]

    def is_window(lookback):
        lo = cutoff - timedelta(days=lookback)
        return [t for c, _, t in is_rows if c >= lo]

    variants = []
    for session, subset, lookback in product(
            SESSION_VARIANTS, _symbol_subsets(symbols), LOOKBACK_VARIANTS):
        if not subset:
            continue
        sel = _filter(is_window(lookback), subset, session)
        st = _stats(sel)
        variants.append({"filters": {"symbols": subset,
                                     "session_preference": session,
                                     "lookback_days": lookback},
                         **st, "_pnls": [float(t["pnl"]) for t in sel]})
    base_f = {"symbols": symbols, "session_preference": baseline_session,
              "lookback_days": 30}
    qualified = [v for v in variants if v["matched_trades"] >= MIN_IS_TRADES]
    best = max(qualified, key=lambda v: v["score"]) if qualified else None
    out = {"cutoff": cutoff.isoformat(), "oos_days": oos_days,
           "is_trades": len(is_rows), "oos_trades_total": len(oos_trades),
           "tested_variants": len(variants)}
    if best is None:
        gate = selection_gate(oos_improvement=None, oos_trades=0, dsr=None,
                              n_trials=len(variants))
        return {**out, "selected": None, "gate": gate, "accepted": False,
                "reason": f"no variant had ≥{MIN_IS_TRADES} in-sample trades"}
    f = best["filters"]
    oos_best = _stats(_filter(oos_trades, f["symbols"],
                              f["session_preference"]))
    oos_base = _stats(_filter(oos_trades, base_f["symbols"],
                              base_f["session_preference"]))
    oos_improvement = round(oos_best["score"] - oos_base["score"], 4)
    dsr = dsr_from_trials(best["_pnls"], [v["sharpe"] for v in variants],
                          n_trials=len(variants) + 1)
    # PBO over unique (session, symbol-subset) configurations, per IS block
    cols = {}
    lo = cutoff - timedelta(days=horizon)
    span = (cutoff - lo).total_seconds()
    for session, subset in product(SESSION_VARIANTS, _symbol_subsets(symbols)):
        if not subset:
            continue
        vec = [0.0] * PBO_BLOCKS
        for t in _filter(is_window(horizon), subset, session):
            c = _parse_iso(t.get("closed_at"))
            k = int((c - lo).total_seconds() * PBO_BLOCKS / span)
            vec[min(max(k, 0), PBO_BLOCKS - 1)] += float(t["pnl"])
        cols[(session, tuple(subset))] = vec
    try:
        pbo = probability_of_backtest_overfitting(
            np.asarray(list(cols.values()), dtype=float).T,
            n_blocks=PBO_BLOCKS, metric="mean")
    except ValueError as e:
        pbo = {"pbo": None, "reason": str(e)}
    gate = selection_gate(oos_improvement=oos_improvement,
                          oos_trades=oos_best["matched_trades"],
                          dsr=dsr.get("dsr"), pbo=pbo.get("pbo"),
                          n_trials=len(cols))
    return {**out, "selected": {k: v for k, v in best.items() if k != "_pnls"},
            "oos_selected": oos_best, "oos_baseline": oos_base,
            "oos_improvement": oos_improvement, "dsr": dsr, "pbo": pbo,
            "gate": gate, "accepted": gate["accepted"]}


async def _load_trades(user_id: str, symbols: list, days: int) -> list:
    from database import get_db
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = {"user_id": user_id, "status": "closed", "closed_at": {"$gte": since}}
    if symbols:
        q["symbol"] = {"$in": symbols}
    return await get_db().trades.find(q).sort("closed_at", 1).to_list(5000)


def _score(result: dict) -> float:
    # iter-42 — profit-tied objective: win rate and P&L must move together.
    return stoic_score(
        result.get("win_rate") or 0.0,
        result.get("total_pnl_usd") or 0.0,
        result.get("matched_trades") or 0,
    )


def _symbol_subsets(symbols: list[str]) -> list[list[str]]:
    if len(symbols) <= 1:
        return [symbols] if symbols else [[]]
    # Powerset minus empty, deduped, sorted for determinism
    out: list[list[str]] = []
    n = len(symbols)
    for mask in range(1, 1 << n):
        subset = [symbols[i] for i in range(n) if (mask >> i) & 1]
        out.append(subset)
    return out


async def optimize(*, dsl: dict, user_id: str, trades: list | None = None,
                   validate: bool = True) -> dict:
    """Grid-search filter variants. Returns the best variant + delta
    (in-sample) plus an out-of-sample validation verdict (`accepted`).
    `trades` may be passed to validate without a DB round-trip."""
    symbols = [s.upper() for s in (dsl.get("symbols") or ["XAUUSD", "BTCUSD"])]
    baseline_session = (dsl.get("session_preference") or "any").lower()

    # Build the variant grid.
    symbol_choices = _symbol_subsets(symbols)
    grid = list(product(SESSION_VARIANTS, symbol_choices, LOOKBACK_VARIANTS))

    # 1. Run baseline exactly as the DSL specifies (using the user's lookback default of 30d)
    baseline_compiled = {
        "symbols": symbols,
        "session_preference": baseline_session,
    }
    baseline_result = await run_backtest(
        compiled=baseline_compiled, user_id=user_id, lookback_days=30,
    )
    baseline = {
        "filters": {"symbols": symbols, "session_preference": baseline_session,
                    "lookback_days": 30},
        "win_rate":      baseline_result.get("win_rate"),
        "total_pnl_usd": baseline_result.get("total_pnl_usd"),
        "matched_trades": baseline_result.get("matched_trades"),
        "score": _score(baseline_result),
    }

    # 2. Sweep the grid.
    variants: list[dict] = []
    for session, sym_subset, lookback in grid:
        if not sym_subset:
            continue
        compiled = {"symbols": sym_subset, "session_preference": session}
        res = await run_backtest(
            compiled=compiled, user_id=user_id, lookback_days=lookback,
        )
        variants.append({
            "filters": {"symbols": sym_subset, "session_preference": session,
                        "lookback_days": lookback},
            "win_rate":      res.get("win_rate"),
            "total_pnl_usd": res.get("total_pnl_usd"),
            "matched_trades": res.get("matched_trades"),
            "score": _score(res),
        })

    # 3. Rank — only consider variants with at least 5 matched trades to
    #    avoid declaring a "winner" off 1-2 lucky trades. (In-sample only;
    #    step 5 decides whether the pick survives out of sample.)
    qualified = [v for v in variants
                 if (v.get("matched_trades") or 0) >= MIN_IS_TRADES]
    qualified.sort(key=lambda v: v["score"], reverse=True)
    best = qualified[0] if qualified else baseline

    # 4. Improvement % — guard against zero/None baseline scores.
    base_score = baseline["score"] or 0.0
    best_score = best["score"] or 0.0
    if base_score > 0:
        improvement_pct = round(((best_score - base_score) / base_score) * 100.0, 2)
    elif best_score > 0:
        improvement_pct = 100.0
    else:
        improvement_pct = 0.0

    notes: list[str] = []
    if not qualified:
        notes.append("No variant had ≥5 matched trades — baseline kept. "
                     "Trade more in paper mode to enable real optimization.")
    elif best["filters"] == baseline["filters"]:
        notes.append("Baseline already optimal across the tested grid — no change suggested.")
    else:
        notes.append(
            f"Best variant: {','.join(best['filters']['symbols'])} "
            f"during {best['filters']['session_preference'].upper()} "
            f"({best['filters']['lookback_days']}d lookback). "
            f"Improvement +{improvement_pct}% over baseline."
        )
    # Always disclose what we DON'T optimize so the user isn't misled.
    notes.append(
        "Optimizer varies filter set only (session + symbol subset + "
        "lookback). It does NOT replay indicator-threshold changes — those "
        "require live indicator snapshots which start logging in iter-30."
    )

    # 5. Out-of-sample validation of the SELECTION PROCEDURE.
    validation: dict
    if not validate:
        validation = {"accepted": False, "reason": "validation disabled"}
    else:
        try:
            if trades is None:
                trades = await asyncio.wait_for(
                    _load_trades(user_id, symbols,
                                 max(LOOKBACK_VARIANTS) + OOS_DAYS),
                    timeout=LOAD_TIMEOUT_S)
            validation = validate_selection(trades, symbols, baseline_session)
        except Exception as e:  # noqa: BLE001 — validation never breaks the preview
            logger.warning("optimizer OOS validation unavailable: %s", e)
            validation = {"accepted": False,
                          "reason": f"validation unavailable: {type(e).__name__}"}
    accepted = bool(validation.get("accepted")) and best is not baseline
    if accepted:
        notes.append(
            f"Out-of-sample check PASSED: re-selected on trades before "
            f"{validation['cutoff'][:10]}, the winner beat the baseline on "
            f"the {validation['oos_days']} held-out days "
            f"(DSR {validation['dsr'].get('dsr')}, "
            f"PBO {validation['pbo'].get('pbo')}).")
    else:
        why = (validation.get("reason")
               or ", ".join((validation.get("gate") or {}).get("failed") or [])
               or "no change suggested")
        notes.append(
            "The 'best' variant above is an IN-SAMPLE maximum over "
            f"{len(variants)} variants on the same trades — NOT validated "
            f"out of sample ({why}). Recommendation: keep the baseline.")

    # Keep top-10 variants by score in the response so the UI can render them
    top_n = sorted(variants, key=lambda v: v["score"], reverse=True)[:10]

    return {
        "baseline": baseline,
        "best": best,
        "improvement_pct": improvement_pct,
        "improvement_basis": "in_sample",
        "accepted": accepted,
        "recommended": best if accepted else baseline,
        "validation": validation,
        "tested_variants": len(variants),
        "variants": top_n,
        "notes": notes,
    }
