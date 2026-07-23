from datetime import datetime, timezone, timedelta
from typing import Optional
import re
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from ai_signals import analyze_symbol
from route_utils import parse_object_id
from routes.bot_routes import _config_filter

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


# Entropy threshold above which the bot pre-filters HOLDs to save tokens.
# Mirrors the ENTROPY_NOISY_THRESHOLD constant in ai_signals.py — kept here
# as a local copy so the dashboard tile renders identical context.
_ENTROPY_NOISY_THRESHOLD = 0.90


def _extract_entropy(reasoning: str) -> Optional[float]:
    """Parse `entropy=0.9441` out of bot_runner HOLD reasoning strings."""
    if not reasoning:
        return None
    m = re.search(r"entropy=([\d.]+)", reasoning)
    return float(m.group(1)) if m else None


# ─── Veto classifier (iter-68) ─────────────────────────────────────────────
# Each veto type maps a regex against the signal's `reasoning` field. Order
# matters — the first match wins (most specific tags appear first). All
# patterns are evaluated case-insensitively.
_VETO_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    # tag,            label,                pattern
    ("market_closed", "Market closed",     re.compile(r"market[\s_-]*close", re.I)),
    ("macro_freeze",  "Macro freeze",      re.compile(r"\bVETO \(macro\)|macro freeze", re.I)),
    ("entropy",       "Entropy / noise",   re.compile(r"\bVETO \(entropy\)|noise filter|entropy=", re.I)),
    ("regime_chop",   "Regime CHOP",       re.compile(r"\bVETO \(regime\)|regime chop", re.I)),
    ("self_contra",   "Self-contradiction", re.compile(r"\bVETO \(self-contradiction\)", re.I)),
    ("news",          "News disagreement", re.compile(r"\bVETO \(news\)", re.I)),
    ("meta_label",    "Meta-Labeler",      re.compile(r"\bVETO \(meta-labeler\)|fake_out", re.I)),
    ("mtf",           "Multi-timeframe",   re.compile(r"\bVETO \(multi-timeframe\)|counter-trend", re.I)),
    ("learned_meta",  "Learned classifier", re.compile(r"\bVETO \(learned-meta\)|learned classifier", re.I)),
    ("a_plus",        "A+ confluence",     re.compile(r"\bVETO \(A\+ confluence\)|a\+ confluence", re.I)),
    ("rr_ratio",      "R:R too low",       re.compile(r"\bVETO \(R:R\)|R:R", re.I)),
    ("dxy",           "DXY headwind",      re.compile(r"\bVETO \(DXY gate\)|dxy", re.I)),
    ("sector_cap",    "Sector cap",        re.compile(r"sector[\s_-]*cap|sector exposure", re.I)),
    ("anti_pyramid",  "Anti-pyramid",      re.compile(r"anti[\s_-]*pyramid", re.I)),
    ("loss_streak",   "Loss-streak cooldown", re.compile(r"loss[\s_-]*streak", re.I)),
    ("cooldown",      "Cooldown",          re.compile(r"signal cooldown|cooldown active", re.I)),
    ("low_confidence", "Low confidence",   re.compile(r"confidence below|conf=\d+ < ", re.I)),
]


def _classify_veto(reasoning: str) -> Optional[tuple[str, str]]:
    """Map a HOLD signal's reasoning string to (tag, label). Returns None
    for un-tagged HOLDs (e.g. plain AI HOLD with no specific veto)."""
    if not reasoning:
        return None
    for tag, label, pattern in _VETO_PATTERNS:
        if pattern.search(reasoning):
            return tag, label
    return None


async def _compute_veto_counts(db, user_id: str, since: datetime) -> dict:
    """Aggregate veto counts across the user's HOLD signals since `since`.

    Returns:
        {
          "window_hours": int,
          "total_holds": int,
          "classified": int,                       # holds matched a veto pattern
          "by_tag":   [{tag, label, count}, ...]   # sorted desc by count
        }
    """
    # bot_runner-generated signals carry no user_id (global market state) AND
    # the user's own generated signals carry user_id. Both should count — the
    # user wants to see what the bot has been holding off on globally.
    cursor = db.signals.find(
        {
            "action": "HOLD",
            "created_at": {"$gte": since.isoformat()},
        },
        projection={"reasoning": 1, "symbol": 1},
    ).limit(2000)

    counts: dict = {}
    total = 0
    classified = 0
    async for doc in cursor:
        total += 1
        cls = _classify_veto(doc.get("reasoning") or "")
        if not cls:
            continue
        classified += 1
        tag, label = cls
        if tag not in counts:
            counts[tag] = {"tag": tag, "label": label, "count": 0}
        counts[tag]["count"] += 1

    by_tag = sorted(counts.values(), key=lambda x: x["count"], reverse=True)
    return {
        "window_hours": int((datetime.now(timezone.utc) - since).total_seconds() / 3600),
        "total_holds": total,
        "classified": classified,
        "by_tag": by_tag,
    }


@router.get("/watch-status")
async def watch_status(user=Depends(get_current_user)):
    """Bot patient-watching status — "why the bot isn't trading right now."

    Returns the latest signal per symbol the user's bot is configured to watch,
    with entropy + sentiment + indicators surfaced for the dashboard tile.
    Bot_runner-generated signals are global market state (no user_id), so we
    grab the latest doc per symbol regardless of owner.
    """
    db = get_db()
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], None)) or {}
    symbols = cfg.get("symbols") or ["XAUUSD"]
    cooldown_min = cfg.get("signal_cooldown_minutes", 3)

    out_symbols = []
    now = datetime.now(timezone.utc)
    for sym in symbols:
        latest = await db.signals.find_one(
            {"symbol": sym, "indicators": {"$exists": True}},
            sort=[("created_at", -1)],
        )
        if not latest:
            out_symbols.append({"symbol": sym, "status": "no_data"})
            continue
        reasoning = latest.get("reasoning") or ""
        entropy = _extract_entropy(reasoning)
        # Parse last evaluation timestamp (stored as ISO string)
        last_ts_raw = latest.get("created_at")
        last_ts = None
        seconds_since = None
        if isinstance(last_ts_raw, str):
            try:
                last_ts = datetime.fromisoformat(last_ts_raw.replace("Z", "+00:00"))
                seconds_since = int((now - last_ts).total_seconds())
            except ValueError:
                pass
        next_eval_in = None
        if seconds_since is not None:
            next_eval_in = max(0, cooldown_min * 60 - seconds_since)
        out_symbols.append({
            "symbol": sym,
            "action": latest.get("action"),
            "confidence": latest.get("confidence"),
            "entropy": entropy,
            "entropy_threshold": _ENTROPY_NOISY_THRESHOLD,
            "noisy": entropy is not None and entropy >= _ENTROPY_NOISY_THRESHOLD,
            "reason": reasoning,
            "sentiment": latest.get("sentiment") or {},
            "indicators": latest.get("indicators") or {},
            "session": latest.get("session") or {},
            "last_evaluated_at": last_ts_raw,
            "seconds_since_last_eval": seconds_since,
            "next_evaluation_in_seconds": next_eval_in,
        })

    # Count how many HOLDs in a row since user's last non-HOLD signal
    last_non_hold = await db.signals.find_one(
        {"user_id": user["id"], "action": {"$in": ["BUY", "SELL"]}},
        sort=[("created_at", -1)],
    )
    hold_streak_query: dict = {"user_id": user["id"], "action": "HOLD"}
    if last_non_hold and last_non_hold.get("created_at"):
        hold_streak_query["created_at"] = {"$gt": last_non_hold["created_at"]}
    hold_streak = await db.signals.count_documents(hold_streak_query)

    # iter-68 — per-veto reject counters over the last 24h.
    since = now - timedelta(hours=24)
    veto_counts = await _compute_veto_counts(db, user["id"], since)

    return {
        "symbols": out_symbols,
        "cooldown_minutes": cooldown_min,
        "hold_streak": hold_streak,
        "last_actionable_signal_at": last_non_hold.get("created_at") if last_non_hold else None,
        "veto_counts": veto_counts,
        "philosophy": "STOIC refuses to trade in noisy regimes — every HOLD is a loss avoided.",
    }


@router.get("")
async def list_signals(limit: int = 50, user=Depends(get_current_user)):
    db = get_db()
    cursor = db.signals.find({"user_id": user["id"]}).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]


@router.post("/generate")
async def generate_signal(payload: dict, account_id: Optional[str] = None,
                          user=Depends(get_current_user)):
    """Generate AI signal for one symbol on demand.

    `account_id` (query param) — pick which bot_config to read risk_level from.
    Omitted → default profile (account_id is None). Lets the Signals UI scope
    the generation to a specific bot when the user has several.
    """
    symbol = (payload.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    db = get_db()
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id)) or {}
    risk_level = payload.get("risk_level") or cfg.get("risk_level", "medium")

    try:
        signal = await analyze_symbol(symbol, risk_level)
    except Exception as e:
        from errors import api_error
        raise api_error(502, "ai_analysis_failed", "AI analysis is temporarily unavailable.", exc=e)

    signal["user_id"] = user["id"]
    signal["consumed"] = False
    signal["created_at"] = datetime.now(timezone.utc).isoformat()
    if account_id:
        signal["account_id"] = account_id
    result = await db.signals.insert_one(signal)
    signal["_id"] = result.inserted_id
    return _serialize(signal)


@router.post("/generate-all")
async def generate_all(account_id: Optional[str] = None,
                       user=Depends(get_current_user)):
    """Generate signals for all configured symbols of a chosen bot.

    `account_id` (query param) selects which bot_config drives the symbol
    list + risk_level. Omitted → default profile.
    """
    db = get_db()
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id))
    if not cfg or not cfg.get("symbols"):
        raise HTTPException(status_code=400, detail="No symbols configured")
    risk_level = cfg.get("risk_level", "medium")
    results = []
    for sym in cfg["symbols"]:
        try:
            sig = await analyze_symbol(sym, risk_level)
            sig["user_id"] = user["id"]
            sig["consumed"] = False
            sig["created_at"] = datetime.now(timezone.utc).isoformat()
            if account_id:
                sig["account_id"] = account_id
            r = await db.signals.insert_one(sig)
            sig["_id"] = r.inserted_id
            results.append(_serialize(sig))
        except Exception as e:
            logger.warning("signal generation failed for %s: %s", sym, e)
            results.append({"symbol": sym, "error": "signal_failed"})
    return {"generated": results, "account_id": account_id}


@router.delete("/{signal_id}")
async def delete_signal(signal_id: str, user=Depends(get_current_user)):
    db = get_db()
    await db.signals.delete_one({"_id": parse_object_id(signal_id, "Signal"), "user_id": user["id"]})
    return {"ok": True}


@router.delete("")
async def bulk_clear_signals(
    scope: str = "all",
    older_than_days: int | None = None,
    user=Depends(get_current_user),
):
    """Bulk-delete signals for the current user.

    Query params:
      scope=all        → every signal owned by the user (default)
      scope=hold       → only HOLD signals (noise cleanup)
      scope=non_hold   → only BUY/SELL signals
      older_than_days  → restrict to signals created more than N days ago

    Examples:
      DELETE /api/signals                      → clear all
      DELETE /api/signals?scope=hold           → clear HOLDs only
      DELETE /api/signals?older_than_days=7    → clear signals > 7 days old
      DELETE /api/signals?scope=hold&older_than_days=1  → tidy HOLDs older than 1d
    """
    db = get_db()
    q: dict = {"user_id": user["id"]}
    if scope == "hold":
        q["action"] = "HOLD"
    elif scope == "non_hold":
        q["action"] = {"$in": ["BUY", "SELL"]}
    elif scope != "all":
        raise HTTPException(
            status_code=400,
            detail="scope must be one of: all, hold, non_hold",
        )
    if older_than_days is not None:
        if older_than_days < 0:
            raise HTTPException(status_code=400, detail="older_than_days must be ≥ 0")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        q["created_at"] = {"$lt": cutoff}
    result = await db.signals.delete_many(q)
    return {"ok": True, "deleted": result.deleted_count, "scope": scope, "older_than_days": older_than_days}

