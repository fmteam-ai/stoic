"""iter-107 · Economic Calendar Intelligence.

Instead of just AVOIDING news, predict HOW the market reacts to each
scheduled release: P(breakout / fakeout / reversal / continuation).

Model: Dirichlet priors per event class (FOMC, CPI, NFP, …) updated with
REAL observed outcomes (`event_outcomes` collection — classified from M15
candles after each event passes), then adjusted for current market context:
  · volatility compression (coiled range → breakout more likely)
  · resting stop clusters near price (fuel for a stop-hunt fakeout)
  · trend extension (stretched trend into the print → reversal risk)

`calendar_entry_policy` turns the prediction into entry guidance: veto new
entries into a fakeout-dominant event window, caution otherwise."""
import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

OUTCOMES = ("breakout", "fakeout", "reversal", "continuation")

# Dirichlet pseudo-counts per event class (informed priors, in %-like units).
PRIORS = {
    "FOMC":   {"breakout": 28, "fakeout": 36, "reversal": 20, "continuation": 16},
    "CPI":    {"breakout": 38, "fakeout": 26, "reversal": 16, "continuation": 20},
    "NFP":    {"breakout": 34, "fakeout": 32, "reversal": 14, "continuation": 20},
    "GDP":    {"breakout": 24, "fakeout": 24, "reversal": 16, "continuation": 36},
    "PMI":    {"breakout": 18, "fakeout": 22, "reversal": 16, "continuation": 44},
    "SPEECH": {"breakout": 14, "fakeout": 28, "reversal": 18, "continuation": 40},
    "OTHER":  {"breakout": 20, "fakeout": 25, "reversal": 17, "continuation": 38},
}

_CLASS_PATTERNS = [
    ("FOMC", r"fomc|federal funds rate|fed interest rate|rate decision|monetary policy (statement|report)"),
    ("CPI", r"\bcpi\b|consumer price|inflation rate|\bpce\b|\bppi\b"),
    ("NFP", r"non-?farm|nfp|unemployment rate|jobless claims|employment change|jobs report|adp"),
    ("GDP", r"\bgdp\b"),
    ("PMI", r"\bpmi\b|\bism\b|manufacturing index|services index"),
    ("SPEECH", r"speaks|speech|testimony|testifies|press conference|minutes"),
]

PRE_EVENT_WINDOW_MIN = 45
POST_EVENT_TRAP_MIN = 15
FAKEOUT_VETO_P = 0.35
PRE_BARS = 8    # 2h of M15 before the event
POST_BARS = 4   # 1h of M15 after

_sweep_last = 0.0


def classify_event(title: str) -> str:
    t = (title or "").lower()
    for cls, pat in _CLASS_PATTERNS:
        if re.search(pat, t):
            return cls
    return "OTHER"


def _atr(bars, n=14):
    trs = [max(bars[i]["h"] - bars[i]["l"],
               abs(bars[i]["h"] - bars[i - 1]["c"]),
               abs(bars[i]["l"] - bars[i - 1]["c"]))
           for i in range(1, len(bars))]
    w = trs[-n:]
    return sum(w) / len(w) if w else 0.0


def context_multipliers(bars, lmap=None) -> tuple[dict, list]:
    """Market-context multipliers per outcome + human-readable drivers."""
    mult = {k: 1.0 for k in OUTCOMES}
    drivers = []
    if not bars or len(bars) < 30:
        return mult, drivers
    atr_full = _atr(bars, n=len(bars) - 1)
    atr_recent = _atr(bars, n=8)
    if atr_full > 0:
        ratio = atr_recent / atr_full
        if ratio < 0.7:
            mult["breakout"] *= 1.3
            drivers.append(f"volatility compressed to {round(ratio * 100)}% of "
                           f"session ATR — coiled for a breakout")
        elif ratio > 1.3:
            mult["fakeout"] *= 1.15
            mult["reversal"] *= 1.15
            drivers.append("volatility already expanded — spike risk of "
                           "exhaustion/fakeout")
    if lmap and lmap.get("ready"):
        atr = lmap.get("atr") or atr_full or 1.0
        near = [c for c in (lmap.get("stop_clusters") or [])
                if abs(c["dist"]) <= 1.5 * atr and c["strength"] >= 2]
        if near:
            mult["fakeout"] *= 1.25
            sides = {c["side"] for c in near}
            drivers.append(f"{len(near)} stop cluster(s) within 1.5×ATR "
                           f"({'/'.join(sorted(sides))}) — stop-hunt fuel")
    closes = [b["c"] for b in bars[-20:]]
    sma20 = sum(closes) / len(closes)
    stretch = abs(bars[-1]["c"] - sma20) / atr_full if atr_full > 0 else 0
    if stretch > 2.0:
        mult["reversal"] *= 1.3
        drivers.append(f"price stretched {round(stretch, 1)}×ATR from its mean "
                       f"into the print — snap-back risk")
    elif stretch > 1.0:
        mult["continuation"] *= 1.15
    return mult, drivers


async def get_posterior(db, event_class: str) -> tuple[dict, int]:
    """Prior + observed outcome counts for this event class (Dirichlet)."""
    counts = dict(PRIORS.get(event_class, PRIORS["OTHER"]))
    n_obs = 0
    try:
        cur = db.event_outcomes.aggregate([
            {"$match": {"event_class": event_class}},
            {"$group": {"_id": "$outcome", "n": {"$sum": 1}}}])
        async for row in cur:
            if row["_id"] in counts:
                counts[row["_id"]] += row["n"] * 8   # each real outcome ≈ 8 prior units
                n_obs += row["n"]
    except Exception as e:
        logger.debug("calendar_intel posterior read failed: %s", e)
    return counts, n_obs


async def predict_event_reaction(db, event: dict, bars=None, lmap=None) -> dict:
    cls = classify_event(event.get("title", ""))
    counts, n_obs = await get_posterior(db, cls)
    mult, drivers = context_multipliers(bars or [], lmap)
    weighted = {k: counts[k] * mult[k] for k in OUTCOMES}
    total = sum(weighted.values()) or 1.0
    probs = {k: round(weighted[k] / total, 2) for k in OUTCOMES}
    top = max(probs, key=probs.get)
    mins_to = round((event.get("when_ts", 0) - time.time()) / 60)
    return {"title": event.get("title"), "country": event.get("country"),
            "impact": event.get("impact"), "when": event.get("when"),
            "minutes_to": mins_to, "event_class": cls, "probs": probs,
            "top": top, "top_p": probs[top], "learned_outcomes": n_obs,
            "context_drivers": drivers}


def calendar_entry_policy(action: str, prediction: dict | None) -> dict | None:
    """Entry guidance inside the event window. VETO only fakeout-dominant."""
    if action not in ("BUY", "SELL") or not prediction:
        return None
    m = prediction.get("minutes_to")
    if m is None:
        return None
    title = prediction.get("title", "event")
    probs = prediction.get("probs") or {}
    top = prediction.get("top")
    if 0 <= m <= PRE_EVENT_WINDOW_MIN:
        if top == "fakeout" and probs.get("fakeout", 0) >= FAKEOUT_VETO_P:
            return {"mode": "VETO",
                    "reason": (f"Calendar intel: '{title}' in {m}min — fakeout is "
                               f"the most likely reaction ({round(probs['fakeout'] * 100)}%). "
                               f"Entries paused; let the stop-hunt sweep run first.")}
        return {"mode": "CAUTION",
                "reason": (f"Calendar intel: '{title}' in {m}min — most likely "
                           f"{top} ({round(probs.get(top, 0) * 100)}%).")}
    if -POST_EVENT_TRAP_MIN <= m < 0:
        if top == "fakeout" and probs.get("fakeout", 0) >= FAKEOUT_VETO_P:
            return {"mode": "VETO",
                    "reason": (f"Calendar intel: '{title}' released {abs(m)}min ago — "
                               f"first spike is a likely trap "
                               f"({round(probs['fakeout'] * 100)}% fakeout). Waiting "
                               f"for the real move.")}
        return {"mode": "CAUTION",
                "reason": (f"Calendar intel: '{title}' released {abs(m)}min ago — "
                           f"expecting {top}.")}
    return None


def classify_outcome(bars, event_ts: float) -> str | None:
    """Post-hoc: what did the market actually do in the hour after the print?"""
    if not bars:
        return None
    idx = None
    for i, b in enumerate(bars):
        if b["t"] >= event_ts:
            idx = i
            break
    if idx is None or idx < PRE_BARS or idx + POST_BARS > len(bars):
        return None
    pre = bars[idx - PRE_BARS:idx]
    post = bars[idx:idx + POST_BARS]
    pre_hi = max(b["h"] for b in pre)
    pre_lo = min(b["l"] for b in pre)
    pre_trend = pre[-1]["c"] - pre[0]["c"]
    last_c = post[-1]["c"]
    wick_beyond = any(b["h"] > pre_hi or b["l"] < pre_lo for b in post)
    closed_beyond = last_c > pre_hi or last_c < pre_lo
    if closed_beyond:
        move = last_c - pre[-1]["c"]
        if pre_trend != 0 and (move > 0) != (pre_trend > 0):
            return "reversal"
        return "breakout"
    if wick_beyond:
        return "fakeout"
    move = last_c - pre[-1]["c"]
    if pre_trend != 0 and (move > 0) != (pre_trend > 0) \
            and abs(move) > 0.5 * _atr(pre + post):
        return "reversal"
    return "continuation"


async def sweep_event_outcomes(db) -> int:
    """Classify passed high-impact events from streamed M15 candles and store
    them in `event_outcomes` (global learning pool). Throttled to 1/30min."""
    global _sweep_last
    now = time.time()
    if now - _sweep_last < 1800:
        return 0
    _sweep_last = now
    from economic_calendar import get_events
    events = [e for e in await get_events()
              if e.get("impact") == "high"
              and now - 26 * 3600 <= e.get("when_ts", 0) <= now - 2 * 3600]
    if not events:
        return 0
    stored = 0
    cdocs = await db.intraday_candles.find({}).to_list(50)
    seen_syms = set()
    for cdoc in cdocs:
        sym = cdoc.get("symbol")
        if sym in seen_syms:
            continue
        seen_syms.add(sym)
        bars = cdoc.get("bars") or []
        for e in events:
            key = f"{e['title']}|{int(e['when_ts'])}|{sym}"
            if await db.event_outcomes.find_one({"key": key}):
                continue
            outcome = classify_outcome(bars, e["when_ts"])
            if not outcome:
                continue
            await db.event_outcomes.insert_one({
                "key": key, "event_class": classify_event(e["title"]),
                "title": e["title"], "country": e.get("country"),
                "symbol": sym, "outcome": outcome,
                "event_ts": e["when_ts"],
                "recorded_at": datetime.now(timezone.utc).isoformat()})
            stored += 1
    if stored:
        logger.info("Calendar intel learned %d new event outcome(s)", stored)
    return stored


async def next_event_prediction(db, base: str, bars=None, lmap=None) -> dict | None:
    """Prediction for the soonest high-impact event within 24h for `base`."""
    from economic_calendar import upcoming_for
    try:
        events = [e for e in await upcoming_for(base, hours=24)
                  if e.get("impact") == "high"]
    except Exception as e:
        logger.debug("calendar_intel upcoming failed: %s", e)
        return None
    if not events:
        return None
    return await predict_event_reaction(db, events[0], bars, lmap)
