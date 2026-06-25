"""Self-Improving Research Agent — daily orchestrator.

Pipeline:
  1. Analyze last-30d trades            → weakness report
  2. Read user's current bot config      → current strategy
  3. Call hypothesis_generator (Claude)  → 3-5 candidate strategies
  4. Backtest each candidate             → ranked by composite score
  5. Persist top proposals to db.improvement_proposals (status="pending")

Per-user 24h cooldown via `db.research_runs.last_run_at`. The bot_runner
cron calls `maybe_run(user_id)` each tick; cooldown logic decides if it's
time.

Surfaces:
  • `/api/research/proposals` — list pending proposals
  • `/api/research/run` — manual trigger (admin/user button)
  • `/api/research/proposals/{id}/accept` — apply to bot config
  • `/api/research/proposals/{id}/dismiss` — reject

Composite scoring (same as strategy_optimizer):
    score = win_rate × log(1+matched_trades) + 0.001×total_pnl_usd
Hypotheses below baseline are STILL persisted but flagged so the UI can show
"AI tried these but none beat your current setup — bot is well-tuned."
"""
import math
import logging
import os
from datetime import datetime, timezone, timedelta

from database import get_db
from research_agent.trade_analyzer import analyze as analyze_trades
from research_agent.hypothesis_generator import generate as generate_hypotheses
from strategy_backtest import run_backtest

logger = logging.getLogger("research.self-improver")


def _cooldown_hours() -> int:
    try:
        return int(os.environ.get("RESEARCH_COOLDOWN_HOURS", "24"))
    except Exception:
        return 24


def _score(b: dict) -> float:
    wr = b.get("win_rate") or 0.0
    n = b.get("matched_trades") or 0
    pnl = b.get("total_pnl_usd") or 0.0
    return (wr * math.log1p(n)) + (0.001 * pnl)


async def _on_cooldown(db, user_id: str) -> bool:
    doc = await db.research_runs.find_one({"user_id": user_id})
    if not doc or not doc.get("last_run_at"):
        return False
    try:
        ts = datetime.fromisoformat(str(doc["last_run_at"]).replace("Z", "+00:00"))
    except Exception:
        return False
    return (datetime.now(timezone.utc) - ts) < timedelta(hours=_cooldown_hours())


async def _user_current_strategy(db, user_id: str) -> tuple[dict, list[str]]:
    """Return (current_compiled, user_symbols)."""
    cfg = await db.bot_configs.find_one({"user_id": user_id, "active": True})
    if not cfg:
        return {}, ["XAUUSD", "BTCUSD"]
    compiled = {
        "symbols": cfg.get("symbols") or ["XAUUSD", "BTCUSD"],
        "session_preference": cfg.get("session_preference", "any"),
        "risk_level": cfg.get("risk_level", "medium"),
        "strategy_style": cfg.get("strategy_style", "trend_following"),
        "max_concurrent_trades": cfg.get("max_concurrent_trades", 2),
        "auto_execute": bool(cfg.get("auto_execute")),
    }
    return compiled, compiled["symbols"]


async def run_for_user(db, user_id: str, *, force: bool = False) -> dict:
    """Execute the full self-improvement loop for a single user."""
    if not force and await _on_cooldown(db, user_id):
        return {"status": "cooldown", "user_id": user_id,
                "message": f"Already ran in last {_cooldown_hours()}h."}

    # 1. Trade analyzer
    weaknesses = await analyze_trades(db, user_id=user_id, lookback_days=30)
    if (weaknesses.get("trade_count") or 0) < 5:
        await _stamp_run(db, user_id, status="skipped_insufficient_data",
                         weaknesses=weaknesses, proposals=[])
        return {"status": "skipped_insufficient_data",
                "user_id": user_id, "weaknesses": weaknesses,
                "proposals": []}

    # 2. Current strategy + universe
    current, user_symbols = await _user_current_strategy(db, user_id)

    # 3. Hypotheses
    hypo = await generate_hypotheses(
        weaknesses=weaknesses,
        current_strategy=current,
        user_symbols=user_symbols,
    )
    candidates = hypo.get("hypotheses") or []

    # 4. Baseline backtest
    baseline = await run_backtest(
        compiled=current, user_id=user_id, lookback_days=30,
    )
    baseline_score = _score(baseline)

    # 5. Auto-backtest each candidate, sort by score
    scored: list[dict] = []
    for c in candidates:
        try:
            bt = await run_backtest(
                compiled=c["compiled"], user_id=user_id, lookback_days=30,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("hypothesis backtest failed: %s", e)
            continue
        sc = _score(bt)
        scored.append({
            "name": c["name"],
            "rationale": c["rationale"],
            "compiled": c["compiled"],
            "backtest": bt,
            "score": sc,
            "delta_vs_baseline": round(sc - baseline_score, 4),
            "beats_baseline": sc > baseline_score and (bt.get("matched_trades") or 0) >= 5,
        })
    scored.sort(key=lambda r: r["score"], reverse=True)

    # 6. Persist top 5 as pending proposals
    now_iso = datetime.now(timezone.utc).isoformat()
    proposals_to_save = []
    for s in scored[:5]:
        proposals_to_save.append({
            "user_id": user_id,
            "name": s["name"],
            "rationale": s["rationale"],
            "compiled": s["compiled"],
            "backtest_summary": {
                "win_rate": s["backtest"].get("win_rate"),
                "total_pnl_usd": s["backtest"].get("total_pnl_usd"),
                "matched_trades": s["backtest"].get("matched_trades"),
            },
            "score": s["score"],
            "delta_vs_baseline": s["delta_vs_baseline"],
            "beats_baseline": s["beats_baseline"],
            "status": "pending",
            "created_at": now_iso,
        })
    if proposals_to_save:
        inserted = await db.improvement_proposals.insert_many(proposals_to_save)
        # Auto-accept opt-in: if user has flagged it AND top proposal beats
        # the configured threshold, apply it immediately + dismiss the rest.
        auto_applied = await _maybe_auto_accept(
            db, user_id=user_id, proposals=proposals_to_save,
            inserted_ids=[str(i) for i in inserted.inserted_ids],
        )
        if auto_applied:
            scored[0]["auto_applied"] = True

    await _stamp_run(db, user_id,
                    status="ran",
                    weaknesses=weaknesses,
                    proposals=[p["name"] for p in proposals_to_save],
                    baseline_score=baseline_score)

    return {
        "status": "ran",
        "user_id": user_id,
        "weaknesses": weaknesses,
        "current_strategy": current,
        "baseline": {
            "win_rate": baseline.get("win_rate"),
            "total_pnl_usd": baseline.get("total_pnl_usd"),
            "matched_trades": baseline.get("matched_trades"),
            "score": round(baseline_score, 4),
        },
        "proposals": scored[:5],
        "hypothesis_notes": hypo.get("notes") or [],
    }


async def _maybe_auto_accept(db, *, user_id: str, proposals: list[dict],
                              inserted_ids: list[str]) -> bool:
    """Opt-in auto-accept: if user enabled it AND top proposal's delta vs
    baseline ≥ threshold AND beats_baseline, apply it to bot_configs and
    mark the rest dismissed.

    Reads settings from db.users.research_auto_accept:
      {"enabled": bool, "min_delta_pct": float (default 10.0)}
    """
    user = await db.users.find_one({"_id": user_id}) or \
           await db.users.find_one({"id": user_id})
    if not user:
        return False
    settings = (user.get("research_auto_accept") or {})
    if not settings.get("enabled"):
        return False
    min_delta = float(settings.get("min_delta_pct") or 10.0)

    top = proposals[0]
    if not top.get("beats_baseline"):
        return False
    # delta_vs_baseline is in absolute composite-score units; convert to %
    # relative to score 1.0 floor. For a 10% delta criterion we just check
    # the raw delta value crossed (≥ 0.10) since scores are typically 0..2.
    if (top.get("delta_vs_baseline") or 0) < (min_delta / 100.0):
        return False

    # Apply via the shared targeting helper — auto-accept uses the
    # "matching" target mode (only bots whose symbols overlap the
    # proposal's symbols; falls back to all if the proposal has no scope).
    from datetime import datetime, timezone
    from research_agent.proposal_targeting import (
        resolve_target_configs, apply_proposal_to_configs,
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    compiled = top.get("compiled") or {}
    proposal_symbols = compiled.get("symbols") or []
    update_fields = {k: v for k, v in {
        "symbols": compiled.get("symbols"),
        "session_preference": compiled.get("session_preference"),
        "risk_level": compiled.get("risk_level"),
        "strategy_style": compiled.get("strategy_style"),
        "max_concurrent_trades": compiled.get("max_concurrent_trades"),
    }.items() if v is not None}

    top_id = inserted_ids[0] if inserted_ids else None
    configs, resolved_mode = await resolve_target_configs(
        db, user_id, proposal_symbols, "matching",
    )
    audit: list = []
    if configs and top_id:
        audit = await apply_proposal_to_configs(
            db, configs,
            update_fields=update_fields,
            proposal_id=str(top_id),
            target_mode=resolved_mode,
            auto=True,
        )

    # Mark the accepted proposal + dismiss the rest
    if inserted_ids:
        from bson import ObjectId
        try:
            await db.improvement_proposals.update_one(
                {"_id": ObjectId(top_id)},
                {"$set": {"status": "auto_accepted",
                          "accepted_at": now_iso,
                          "auto_accepted": True,
                          "applied_target_mode": resolved_mode,
                          "applied_to_count": len(audit),
                          "applied_audit": audit}},
            )
            other_ids = [ObjectId(x) for x in inserted_ids[1:]]
            if other_ids:
                await db.improvement_proposals.update_many(
                    {"_id": {"$in": other_ids}},
                    {"$set": {"status": "dismissed_auto",
                              "dismissed_at": now_iso}},
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("auto-accept persist failed: %s", e)

    logger.warning("AUTO-ACCEPT applied for user=%s name=%s delta=%.3f mode=%s touched=%d",
                   user_id, top.get("name"), top.get("delta_vs_baseline"),
                   resolved_mode, len(audit))
    return True


async def _stamp_run(db, user_id: str, *, status: str, weaknesses: dict,
                    proposals: list, baseline_score: float | None = None):
    await db.research_runs.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "last_status": status,
            "last_trade_count": weaknesses.get("trade_count"),
            "last_proposal_count": len(proposals),
            "last_baseline_score": baseline_score,
        }},
        upsert=True,
    )


async def daily_sweep(db) -> dict:
    """Cron-friendly sweep — run for every active user past their cooldown."""
    users = await db.bot_configs.distinct("user_id", {"active": True})
    summary = {"checked": 0, "ran": 0, "skipped": 0,
                "proposals_total": 0, "errors": 0}
    for uid in users:
        summary["checked"] += 1
        try:
            res = await run_for_user(db, uid)
            if res.get("status") == "ran":
                summary["ran"] += 1
                summary["proposals_total"] += len(res.get("proposals") or [])
            else:
                summary["skipped"] += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("Self-improve failed for user %s: %s", uid, e)
            summary["errors"] += 1
    return summary


def get_db_for_cron():
    return get_db()
