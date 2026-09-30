"""Portfolio Risk Manager — composes sectors + VaR + drawdown + correlation
into a single account-wide snapshot, and decides whether deleveraging is
warranted.

Decision logic (in priority order):
  1. HARD drawdown breach          → deleverage 50% of open notional
  2. Sector cap exceeded           → trim worst-offending sector to cap
  3. Correlation spike + over-cap   → reduce same-bucket exposure by 25%
  4. VaR > limit                   → trim largest contributor

Each trigger produces a list of `actions` the bot_runner can execute
(setting `close_requested=True` with `close_reason="auto_deleverage_*"`
on selected trades — actual broker close goes through the normal flow).
"""
import logging
import os
from collections import defaultdict

from portfolio.sectors import sector_for
from portfolio.var import calculate_var
from portfolio.drawdown import update_and_get as get_drawdown
import portfolio_risk as _pr

logger = logging.getLogger("portfolio.risk-manager")


def _f(k, d):
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return d


# Sector exposure caps (% of equity, applied to combined sector NOTIONAL).
# These are notional-to-equity ratios, which for leveraged products are
# routinely 100%+ even on a single sensible position. Defaults chosen for
# retail leveraged accounts (1:30–1:500); power users can override via env
# or PER-ACCOUNT via `cfg.sector_caps_pct_override = {"commodity": 800, …}`.
SECTOR_CAPS_PCT = {
    "crypto":       _f("PORTFOLIO_SECTOR_CAP_CRYPTO_PCT",   300.0),
    "commodity":    _f("PORTFOLIO_SECTOR_CAP_COMMODITY_PCT", 500.0),
    "equity_index": _f("PORTFOLIO_SECTOR_CAP_EQUITY_PCT",   300.0),
    "fx_major":     _f("PORTFOLIO_SECTOR_CAP_FX_MAJOR_PCT", 600.0),
    "fx_minor":     _f("PORTFOLIO_SECTOR_CAP_FX_MINOR_PCT", 300.0),
    "other":        _f("PORTFOLIO_SECTOR_CAP_OTHER_PCT",    100.0),
}


def _resolve_sector_caps(cfg_override: dict | None) -> dict:
    """Merge global SECTOR_CAPS_PCT with a per-account override dict.

    Override shape: {"commodity": 800.0, "crypto": 200.0, ...}. Unknown keys
    are ignored; missing keys fall back to the global default.
    """
    if not cfg_override or not isinstance(cfg_override, dict):
        return SECTOR_CAPS_PCT
    out = dict(SECTOR_CAPS_PCT)
    for k, v in cfg_override.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out

# Combined-sector cap (e.g., "crypto + equity_index ≤ 50%") when they're correlated.
# Triggered only if avg pairwise corr across the bucket ≥ HIGH_CORR_THRESHOLD.
COMBINED_RISK_BUCKET = {"crypto", "equity_index", "commodity"}
COMBINED_RISK_BUCKET_CAP_PCT = _f("PORTFOLIO_COMBINED_RISK_CAP_PCT", 50.0)
HIGH_CORR_THRESHOLD = _f("PORTFOLIO_HIGH_CORR_THRESHOLD", 0.70)

# VaR cap as % of equity (1-day, 95% confidence)
VAR_CAP_PCT = _f("PORTFOLIO_VAR_CAP_PCT", 5.0)


async def _sector_exposure(positions: list[dict], equity: float,
                           caps: dict | None = None) -> dict:
    """Group notional by sector. Returns {sector: {notional, pct_equity, positions: [...]}}

    `caps` may be a merged per-account override dict; defaults to module
    SECTOR_CAPS_PCT when None.
    """
    caps = caps or SECTOR_CAPS_PCT
    grouped: dict[str, dict] = defaultdict(lambda: {"notional": 0.0, "positions": []})
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        sec = sector_for(sym)
        lot = float(p.get("lot_size") or 0)
        entry = float(p.get("entry_price") or 0)
        notional = lot * entry * (100 if sym == "XAUUSD" else 1)
        grouped[sec]["notional"] += notional
        grouped[sec]["positions"].append({
            "trade_id": str(p.get("_id") or p.get("id") or ""),
            "symbol": sym,
            "lot": lot,
            "notional": notional,
            # Used by auto-deleverage to prefer culling losers — iter-48 fix.
            # Currently-broker-reported live P&L (may be None on paper trades).
            "live_pnl": float(p.get("live_pnl") if p.get("live_pnl") is not None else 0.0),
            "mt5_ticket": p.get("mt5_ticket"),
        })
    for sec, data in grouped.items():
        data["pct_equity"] = (data["notional"] / equity * 100.0) if equity > 0 else 0.0
        data["cap_pct"] = caps.get(sec, 100.0)
        data["over_cap"] = data["pct_equity"] > data["cap_pct"]
    return dict(grouped)


async def _avg_pairwise_corr(symbols: list[str]) -> float:
    """Mean |pairwise correlation| across the symbol set. 0 when <2 symbols.
    iter-142: timestamp-aligned daily returns; unknown pairs use the
    conservative UNKNOWN_RHO prior."""
    from portfolio.var import UNKNOWN_RHO, corr_returns
    unique = sorted(set(s.upper() for s in symbols if s))
    if len(unique) < 2:
        return 0.0
    pairs = 0
    total = 0.0
    for i, s1 in enumerate(unique):
        for s2 in unique[i + 1:]:
            c = await corr_returns(s1, s2)
            if c is None:
                c = UNKNOWN_RHO
            total += abs(c)
            pairs += 1
    return (total / pairs) if pairs else 0.0


async def build_snapshot(db, *, account: dict, open_positions: list[dict],
                         cfg: dict | None = None) -> dict:
    """Single dispatch — returns the full portfolio risk snapshot.

    `cfg` is the per-account bot_config; `cfg.sector_caps_pct_override`
    layers on top of SECTOR_CAPS_PCT.
    """
    equity = float(account.get("equity") or account.get("balance") or 0)
    account_id = str(account.get("_id") or "")
    caps = _resolve_sector_caps((cfg or {}).get("sector_caps_pct_override"))

    # 1. Drawdown
    dd = await get_drawdown(db, account_id, equity)

    # 2. Sectors
    sectors = await _sector_exposure(open_positions, equity, caps=caps)

    # 3. VaR + correlation matrix
    var = await calculate_var(open_positions, equity=equity)

    # 4. Combined-risk bucket check (crypto + equity_index + commodity correlated)
    bucket_symbols = [p["symbol"] for s in COMBINED_RISK_BUCKET
                      if s in sectors
                      for p in sectors[s]["positions"]]
    bucket_notional = sum(p["notional"] for s in COMBINED_RISK_BUCKET
                          if s in sectors
                          for p in sectors[s]["positions"])
    bucket_pct = (bucket_notional / equity * 100.0) if equity > 0 else 0.0
    bucket_corr = await _avg_pairwise_corr(bucket_symbols) if len(set(bucket_symbols)) >= 2 else 0.0
    bucket_breach = (bucket_corr >= HIGH_CORR_THRESHOLD
                     and bucket_pct > COMBINED_RISK_BUCKET_CAP_PCT)

    # 5. Aggregate VaR limit check
    var_breach = (var.get("var_95_pct_equity") or 0) > VAR_CAP_PCT

    # 6. Build actions list (no execution here — caller decides)
    actions: list[dict] = []
    triggers: list[str] = []

    if dd["hard_breach"]:
        triggers.append("hard_drawdown")
        # Target: reduce 50% of total notional. Pick the largest positions.
        target_close_pct = 0.5
        sorted_pos = sorted(open_positions,
                            key=lambda p: float(p.get("lot_size") or 0) * float(p.get("entry_price") or 0),
                            reverse=True)
        cum = 0.0
        target = sum(float(p.get("lot_size") or 0) * float(p.get("entry_price") or 0)
                     for p in open_positions) * target_close_pct
        for p in sorted_pos:
            n = float(p.get("lot_size") or 0) * float(p.get("entry_price") or 0)
            if cum >= target:
                break
            actions.append({
                "kind": "close_trade", "trade_id": str(p.get("_id") or p.get("id") or ""),
                "symbol": p.get("symbol"), "lot_size": p.get("lot_size"),
                "reason": "auto_deleverage_hard_drawdown",
            })
            cum += n

    for sec, data in sectors.items():
        if data["over_cap"]:
            triggers.append(f"sector_cap_{sec}")
            # iter-48 fix — prefer closing the WORST LOSER in the over-cap
            # sector rather than the largest position. Previous "largest"
            # rule kept culling underwater pyramids, locking in losses while
            # the bot opened more same-direction trades into the trap.
            # Fallback to largest by notional when live P&L is unavailable.
            if data["positions"]:
                has_pnl = any(p.get("live_pnl") for p in data["positions"])
                if has_pnl:
                    worst = min(data["positions"], key=lambda x: x.get("live_pnl") or 0)
                else:
                    worst = max(data["positions"], key=lambda x: x["notional"])
                actions.append({
                    "kind": "close_trade", "trade_id": worst["trade_id"],
                    "symbol": worst["symbol"], "lot_size": worst["lot"],
                    "reason": f"auto_deleverage_sector_cap_{sec}",
                })

    if bucket_breach:
        triggers.append("combined_corr_bucket")
        # 25% reduction of bucket exposure — close one (largest) trade from
        # whichever bucket sector contributes most.
        worst_sec = max(
            (s for s in COMBINED_RISK_BUCKET if s in sectors),
            key=lambda s: sectors[s]["notional"], default=None,
        )
        if worst_sec and sectors[worst_sec]["positions"]:
            worst = max(sectors[worst_sec]["positions"], key=lambda x: x["notional"])
            actions.append({
                "kind": "close_trade", "trade_id": worst["trade_id"],
                "symbol": worst["symbol"], "lot_size": worst["lot"],
                "reason": "auto_deleverage_combined_corr",
            })

    if var_breach:
        triggers.append("var_cap")
        # Close the symbol with the largest weight × atr% contribution.
        if var.get("positions"):
            contrib = sorted(
                var["positions"],
                key=lambda r: (r.get("weight") or 0) * (r.get("atr_pct") or 0),
                reverse=True,
            )
            for r in contrib[:1]:
                # iter-41 fix — among the top-risk symbol's positions prefer
                # culling the WORST LOSER (mirrors the iter-48 sector-cap
                # fix). The previous "first match" pick closed arbitrary
                # positions and showed up as -$362 avg late-loser closes.
                matches = [p for p in open_positions
                           if (p.get("symbol") or "").upper() == r["symbol"]]
                if not matches:
                    continue
                if any(p.get("live_pnl") for p in matches):
                    pick = min(matches, key=lambda p: float(p.get("live_pnl") or 0))
                else:
                    pick = max(matches, key=lambda p: float(p.get("lot_size") or 0)
                               * float(p.get("entry_price") or 0))
                actions.append({
                    "kind": "close_trade",
                    "trade_id": str(pick.get("_id") or pick.get("id") or ""),
                    "symbol": r["symbol"], "lot_size": pick.get("lot_size"),
                    "reason": "auto_deleverage_var_cap",
                })

    # Dedupe actions by trade_id (keep first occurrence so HIGHER-priority
    # triggers like hard_drawdown win)
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in actions:
        tid = a.get("trade_id") or ""
        if tid and tid in seen:
            continue
        seen.add(tid)
        deduped.append(a)

    return {
        "equity": equity,
        "drawdown": dd,
        "sectors": sectors,
        "var": var,
        # Phase D — currency-leg exposure + deterministic stress scenarios
        # (gap-multiplied worst single-leg shock) from portfolio_risk
        "currency_exposure": _pr.currency_exposure(open_positions),
        "stress": {**_pr.stress_loss_usd(open_positions,
                                         _pr.DEFAULTS["gap_mult"]),
                   "cap_usd": round(equity * _pr.DEFAULTS["stress_pct"]
                                    / 100.0, 2)},
        "position_correlations": _pr.correlation_matrix([
            {"symbol": p.get("symbol"), "action": p.get("action"),
             "lot": p.get("lot_size"), "entry_price": p.get("entry_price"),
             "stop_loss": p.get("stop_loss")} for p in open_positions]),
        "combined_risk_bucket": {
            "symbols": sorted(set(bucket_symbols)),
            "notional_pct_equity": round(bucket_pct, 3),
            "avg_corr": round(bucket_corr, 3),
            "cap_pct": COMBINED_RISK_BUCKET_CAP_PCT,
            "corr_threshold": HIGH_CORR_THRESHOLD,
            "breach": bucket_breach,
        },
        "limits": {
            "sector_caps_pct": caps,
            "var_cap_pct": VAR_CAP_PCT,
            "combined_risk_cap_pct": COMBINED_RISK_BUCKET_CAP_PCT,
            "corr_threshold": HIGH_CORR_THRESHOLD,
        },
        "triggers": triggers,
        "actions": deduped,
        "needs_deleveraging": bool(deduped),
    }


async def execute_deleveraging_actions(db, *, user_id: str, actions: list[dict]) -> dict:
    """Mark selected trades for close. The reconciler / EA picks them up.

    Behaviour differs by trade status:
      • status='open'    → set `close_requested=True` (EA closes via OrderClose).
      • status='pending' → set `status='cancelled'` directly. The EA polls
        only `pending` trades for OPEN, so cancelling removes the order
        before it's ever sent to the broker. Setting `close_requested` on a
        pending trade caused MT5 retcode 10013 INVALID_REQUEST because the
        EA received an OPEN+close instruction simultaneously.
    """
    from datetime import datetime, timezone
    from bson import ObjectId
    closed = 0
    cancelled = 0
    skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for a in actions:
        if a.get("kind") != "close_trade":
            continue
        tid = a.get("trade_id") or ""
        try:
            oid = ObjectId(tid)
        except Exception:
            skipped += 1
            continue
        # Close open trades (real broker position exists).
        from close_commands import request_close
        res_open = await request_close(db, {"_id": oid, "user_id": user_id},
                                       reason=a.get("reason") or "auto_deleverage",
                                       actor="portfolio_risk_manager", stamp={"close_requested_at": now_iso})
        if res_open["trades_marked_for_close"]:
            closed += 1
            continue
        # Cancel pending trades (not yet filled — no broker position).
        res_pending = await db.trades.update_one(
            {"_id": oid, "user_id": user_id, "status": "pending"},
            {"$set": {
                "status": "cancelled",
                "close_reason": a.get("reason") or "auto_deleverage_cancel_pending",
                "closed_at": now_iso,
                "error": "Pending order cancelled by auto-deleverage before fill.",
            }},
        )
        if res_pending.modified_count:
            cancelled += 1
        else:
            skipped += 1
    return {"closed": closed, "cancelled": cancelled,
            "skipped": skipped, "actions": len(actions)}
