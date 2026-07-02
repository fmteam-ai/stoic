"""AI Strategy Optimizer — reviews an account's closed trades over the last
24/48h and produces SUGGEST-ONLY recommendations to raise the win rate.

Pipeline (per account scope):
  1. Quantitative stats  — win rate, profit factor, P&L, breakdowns by
                           symbol / direction / session / close_reason,
                           worst losing streak. Pure math, no LLM.
  2. Claude analysis     — strict-JSON verdict + patterns + recommendations.
                           Tries `claude-fable-5` first (user's choice),
                           falls back to `claude-opus-4-8` if rejected.
  3. Validation          — every LLM recommendation is whitelisted against
                           ALLOWED_FIELDS with hard clamps; anything the
                           model hallucinates is dropped. `from` values are
                           recorded from the live bot_config for the diff UI.
  4. Storage             — one doc per run in `optimizer_reports`. Each
                           recommendation carries status pending/applied/
                           dismissed. NOTHING is auto-applied — the user
                           clicks Apply in the UI (suggest-only by design).

Scheduled sweep: `scheduled_sweep()` is called hourly from server.py and
re-analyzes each ACTIVE bot scope at most once per 24h, only when there are
enough fresh closed trades to say something meaningful.
"""
import os
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta

from database import get_db
from strategy_presets import PRESETS, get_preset
from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger("ai-optimizer")

MIN_TRADES = 3          # below this we store an insufficient_data report, no LLM
CACHE_MINUTES = 5       # manual re-analyze within this window returns cached report
SCHEDULED_EVERY_HOURS = 24

# LLM preference order — user asked for Claude Fable 5; Opus 4.8 is the
# guaranteed-supported fallback on the Emergent Universal Key.
MODEL_CANDIDATES = [("anthropic", "claude-fable-5"), ("anthropic", "claude-opus-4-8")]

# Whitelist of bot_config fields the optimizer may suggest changing.
# (kind, min, max) — bool kind ignores min/max. Values outside are clamped.
ALLOWED_FIELDS = {
    "min_confidence_override":      ("int",   0,    95),
    "trade_of_day_cap":             ("int",   1,    20),
    "max_concurrent_trades":        ("int",   1,    10),
    "trailing_enabled":             ("bool",  None, None),
    "trailing_start_r":             ("float", 0.1,  5.0),
    "trailing_distance_r":          ("float", 0.1,  5.0),
    "partial_close_enabled":        ("bool",  None, None),
    "partial_close_trigger_r":      ("float", 0.1,  5.0),
    "partial_close_fraction":       ("float", 0.1,  0.9),
    "breakeven_enabled":            ("bool",  None, None),
    "breakeven_trigger_r":          ("float", 0.1,  5.0),
    "sl_cooldown_enabled":          ("bool",  None, None),
    "sl_cooldown_minutes":          ("int",   5,    240),
    "anti_tilt_enabled":            ("bool",  None, None),
    "anti_tilt_consecutive_losses": ("int",   1,    10),
    "anti_tilt_freeze_hours":       ("int",   1,    48),
    "asia_session_skip_xau":        ("bool",  None, None),
    "aggressive_mode":              ("bool",  None, None),
    "pre_news_protect_enabled":     ("bool",  None, None),
    "pre_news_protect_minutes":     ("int",   1,    60),
    "profit_taking_mode":           ("enum",  None, ("expected_value", "win_rate", "trend_follow")),
    "daily_drawdown_pct":           ("float", 0.5,  20.0),
}

_SYSTEM_PROMPT = """You are STOIC's strategy-tuning analyst. You receive a
trading account's closed-trade statistics for the last {window}h plus its
current bot configuration. Return STRICT JSON ONLY (no markdown, no prose
outside the JSON) with this exact shape:

{{
  "verdict": "healthy" | "needs_tuning" | "underperforming" | "critical",
  "headline": "one punchy sentence on overall performance",
  "summary": "2-3 sentences: what is working, what is bleeding money",
  "patterns": [
    {{"title": "short pattern name", "detail": "1-2 sentence evidence-based finding", "severity": "info"|"warning"|"critical"}}
  ],
  "recommendations": [
    {{"type": "config_change", "field": "<one of the allowed fields>", "to": <new value>, "reason": "why, citing the stats", "expected_impact": "what should improve"}},
    {{"type": "preset_switch", "preset_key": "<one of the preset keys>", "reason": "...", "expected_impact": "..."}},
    {{"type": "pause_bot", "reason": "...", "expected_impact": "..."}}
  ]
}}

Rules:
- 1 to 4 recommendations max, ordered by impact. 1 to 4 patterns.
- Only use "pause_bot" when the account is consistently losing (win rate
  under ~35% on a meaningful sample, or heavy drawdown) — it stops trading.
- config_change "field" MUST be one of: {allowed_fields}
- preset_switch "preset_key" MUST be one of: {preset_keys}
- Never recommend the value a field already has, or the preset already active.
- Base every recommendation on the supplied stats. Be terse and specific."""


# ---------------------------------------------------------------- stats

_SESSIONS = (("asia", 0, 7), ("london", 7, 13), ("new_york", 13, 21), ("late_ny", 21, 24))


def _session_of(iso_ts) -> str:
    try:
        h = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).hour
    except Exception:
        return "unknown"
    for name, lo, hi in _SESSIONS:
        if lo <= h < hi:
            return name
    return "unknown"


def compute_trade_stats(trades: list[dict]) -> dict:
    """Pure quantitative digest of a list of closed trades."""
    total = len(trades)
    wins = [t for t in trades if float(t.get("pnl") or 0) > 0]
    losses = [t for t in trades if float(t.get("pnl") or 0) <= 0]
    gross_profit = sum(float(t.get("pnl") or 0) for t in wins)
    gross_loss = abs(sum(float(t.get("pnl") or 0) for t in losses))

    def _bucket(key_fn):
        out: dict = {}
        for t in trades:
            k = key_fn(t) or "unknown"
            b = out.setdefault(k, {"trades": 0, "wins": 0, "pnl": 0.0})
            b["trades"] += 1
            b["pnl"] = round(b["pnl"] + float(t.get("pnl") or 0), 2)
            if float(t.get("pnl") or 0) > 0:
                b["wins"] += 1
        for b in out.values():
            b["win_rate"] = round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0
        return out

    # Worst losing streak (chronological by closed_at)
    streak = worst_streak = 0
    for t in sorted(trades, key=lambda x: str(x.get("closed_at") or "")):
        if float(t.get("pnl") or 0) <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "total_pnl": round(gross_profit - gross_loss, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "worst_losing_streak": worst_streak,
        "by_symbol": _bucket(lambda t: t.get("base_symbol") or t.get("symbol")),
        "by_direction": _bucket(lambda t: t.get("action")),
        "by_session": _bucket(lambda t: _session_of(t.get("opened_at"))),
        "by_close_reason": _bucket(lambda t: (t.get("close_reason") or "unknown")[:40]),
    }


# ---------------------------------------------------------------- LLM

def _parse_llm_json(raw: str) -> dict:
    raw = str(raw).strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    # tolerate stray prose before/after the object
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)


async def _call_llm(window_hours: int, payload: dict) -> tuple[dict | None, str | None]:
    """Try Fable 5 first, fall back to Opus 4.8. Returns (analysis, model_used)."""
    system = _SYSTEM_PROMPT.format(
        window=window_hours,
        allowed_fields=", ".join(ALLOWED_FIELDS.keys()),
        preset_keys=", ".join(PRESETS.keys()),
    )
    text = json.dumps(payload, default=str)
    for provider, model in MODEL_CANDIDATES:
        try:
            chat = LlmChat(
                api_key=os.environ["EMERGENT_LLM_KEY"],
                session_id=f"optimizer-{uuid.uuid4().hex[:10]}",
                system_message=system,
            ).with_model(provider, model)
            response = await chat.send_message(UserMessage(text=text))
            return _parse_llm_json(response), model
        except Exception as e:  # noqa: BLE001
            logger.warning("optimizer LLM %s failed: %s", model, e)
    return None, None


# ---------------------------------------------------------------- validation

def _clamp(field: str, value):
    kind, lo, hi = ALLOWED_FIELDS[field]
    if kind == "bool":
        return bool(value)
    if kind == "enum":
        v = str(value).lower()
        return v if v in hi else None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    v = max(lo, min(hi, v))
    return int(v) if kind == "int" else round(v, 2)


def validate_recommendations(raw_recs, cfg: dict) -> list[dict]:
    """Whitelist + clamp every LLM recommendation. Drops anything invalid."""
    out = []
    active_preset = cfg.get("active_preset")
    for r in (raw_recs or []):
        if len(out) >= 4:
            break
        if not isinstance(r, dict):
            continue
        rtype = r.get("type")
        rec = {
            "id": uuid.uuid4().hex[:8],
            "type": rtype,
            "reason": str(r.get("reason") or "")[:400],
            "expected_impact": str(r.get("expected_impact") or "")[:300],
            "status": "pending",
        }
        if rtype == "config_change":
            field = r.get("field")
            if field not in ALLOWED_FIELDS:
                continue
            to_val = _clamp(field, r.get("to"))
            if to_val is None:
                continue
            from_val = cfg.get(field)
            if from_val == to_val:
                continue
            rec.update({"field": field, "from": from_val, "to": to_val})
        elif rtype == "preset_switch":
            key = r.get("preset_key")
            if key not in PRESETS or key == active_preset:
                continue
            rec.update({"preset_key": key, "preset_label": PRESETS[key]["label"],
                        "from_preset": active_preset})
        elif rtype == "pause_bot":
            if not cfg.get("active"):
                continue
        else:
            continue
        out.append(rec)
    return out


# ---------------------------------------------------------------- core

def _config_filter(user_id: str, account_id):
    if account_id:
        return {"user_id": user_id, "account_id": account_id}
    return {"user_id": user_id,
            "$or": [{"account_id": None}, {"account_id": {"$exists": False}}]}


def serialize_report(doc: dict) -> dict:
    d = {k: v for k, v in doc.items() if k != "_id"}
    d["id"] = str(doc["_id"])
    return d


async def analyze_account(user_id: str, account_id, window_hours: int = 24,
                          source: str = "manual") -> dict:
    """Run a full analysis for one scope and persist the report."""
    db = get_db()
    window_hours = 48 if int(window_hours) >= 48 else 24
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

    trade_q = {"user_id": user_id, "status": "closed", "closed_at": {"$gte": cutoff}}
    if account_id:
        trade_q["account_id"] = account_id
    trades = await db.trades.find(trade_q).sort("closed_at", -1).to_list(300)

    cfg = await db.bot_configs.find_one(_config_filter(user_id, account_id)) or {}
    stats = compute_trade_stats(trades)

    doc = {
        "user_id": user_id,
        "account_id": account_id,
        "window_hours": window_hours,
        "source": source,
        "stats": stats,
        "active_preset": cfg.get("active_preset"),
        "bot_active": bool(cfg.get("active")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if stats["total_trades"] < MIN_TRADES:
        doc.update({
            "insufficient_data": True,
            "verdict": "insufficient_data",
            "headline": f"Only {stats['total_trades']} closed trade(s) in the last "
                        f"{window_hours}h — need at least {MIN_TRADES} for a meaningful review.",
            "summary": "Let the bot trade a bit longer or widen the window to 48h.",
            "patterns": [], "recommendations": [], "model_used": None,
        })
        res = await db.optimizer_reports.insert_one(doc)
        doc["_id"] = res.inserted_id
        return serialize_report(doc)

    # Compact per-trade lines keep token cost low but give Claude sequence context.
    trade_lines = [
        {
            "symbol": t.get("base_symbol") or t.get("symbol"),
            "action": t.get("action"),
            "pnl": round(float(t.get("pnl") or 0), 2),
            "session": _session_of(t.get("opened_at")),
            "close_reason": (t.get("close_reason") or "")[:30],
            "lot": t.get("lot_size"),
        }
        for t in trades[:60]
    ]
    cfg_snapshot = {f: cfg.get(f) for f in ALLOWED_FIELDS}
    cfg_snapshot["active_preset"] = cfg.get("active_preset")
    cfg_snapshot["bot_active"] = bool(cfg.get("active"))
    cfg_snapshot["risk_level"] = cfg.get("risk_level")

    analysis, model_used = await _call_llm(window_hours, {
        "stats": stats,
        "recent_trades_newest_first": trade_lines,
        "current_bot_config": cfg_snapshot,
    })

    if not analysis:
        doc.update({
            "verdict": "unavailable",
            "headline": "AI analysis unavailable — LLM call failed. Stats below are still accurate.",
            "summary": "Retry in a few minutes. If this persists, check the Emergent LLM key balance.",
            "patterns": [], "recommendations": [], "model_used": None, "_llm_failed": True,
        })
    else:
        verdict = str(analysis.get("verdict") or "needs_tuning").lower()
        if verdict not in ("healthy", "needs_tuning", "underperforming", "critical"):
            verdict = "needs_tuning"
        doc.update({
            "verdict": verdict,
            "headline": str(analysis.get("headline") or "")[:300],
            "summary": str(analysis.get("summary") or "")[:800],
            "patterns": [
                {"title": str(p.get("title") or "")[:80],
                 "detail": str(p.get("detail") or "")[:400],
                 "severity": p.get("severity") if p.get("severity") in ("info", "warning", "critical") else "info"}
                for p in (analysis.get("patterns") or [])[:4] if isinstance(p, dict)
            ],
            "recommendations": validate_recommendations(analysis.get("recommendations"), cfg),
            "model_used": model_used,
        })

    res = await db.optimizer_reports.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize_report(doc)


async def get_latest_report(user_id: str, account_id) -> dict | None:
    db = get_db()
    doc = await db.optimizer_reports.find_one(
        {"user_id": user_id, "account_id": account_id}, sort=[("created_at", -1)]
    )
    return serialize_report(doc) if doc else None


# ---------------------------------------------------------------- apply

async def apply_recommendation(user_id: str, report: dict, rec: dict) -> dict:
    """Execute a single validated recommendation. Suggest-only flow — this
    only ever runs from an explicit user click. Returns an audit dict."""
    db = get_db()
    account_id = report.get("account_id")
    now = datetime.now(timezone.utc).isoformat()
    audit = {"applied_at": now}

    if rec["type"] == "config_change":
        field = rec["field"]
        value = _clamp(field, rec["to"])  # re-clamp — defense in depth
        await db.bot_configs.update_one(
            _config_filter(user_id, account_id),
            {"$set": {field: value, "updated_at": now}},
        )
        audit.update({"field": field, "to": value})
    elif rec["type"] == "preset_switch":
        preset = get_preset(rec["preset_key"])
        overlay = {**preset["config"], "active_preset": rec["preset_key"], "updated_at": now}
        await db.bot_configs.update_one(
            _config_filter(user_id, account_id), {"$set": overlay}
        )
        audit.update({"preset": rec["preset_key"]})
    elif rec["type"] == "pause_bot":
        await db.bot_configs.update_one(
            _config_filter(user_id, account_id),
            {"$set": {"active": False, "updated_at": now}},
        )
        audit.update({"paused": True})
    return audit


# ---------------------------------------------------------------- scheduled sweep

async def scheduled_sweep():
    """Hourly tick from server.py. For every ACTIVE bot scope, re-analyze at
    most once per 24h — and only when the window holds enough trades."""
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SCHEDULED_EVERY_HOURS)).isoformat()
    ran = 0
    async for cfg in db.bot_configs.find({"active": True}):
        user_id = cfg.get("user_id")
        account_id = cfg.get("account_id")
        if not user_id:
            continue
        recent = await db.optimizer_reports.find_one({
            "user_id": user_id, "account_id": account_id,
            "created_at": {"$gte": cutoff},
        })
        if recent:
            continue
        trade_cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        tq = {"user_id": user_id, "status": "closed", "closed_at": {"$gte": trade_cut}}
        if account_id:
            tq["account_id"] = account_id
        n = await db.trades.count_documents(tq)
        if n < MIN_TRADES:
            continue
        try:
            await analyze_account(user_id, account_id, 24, source="scheduled")
            ran += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduled optimizer failed user=%s acct=%s: %s",
                           user_id, account_id, e)
    if ran:
        logger.info("optimizer scheduled sweep: %d report(s) generated", ran)
