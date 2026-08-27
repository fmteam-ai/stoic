"""T0→T9 execution latency profiler — the canonical decision-path trace.

  T0 tick received · T1 features ready · T2 opportunity detected ·
  T3 strategy verdict · T4 AI verdict · T5 risk decision ·
  T6 Execution Authority authorization · T7 EA receives (dispatch pickup) ·
  T8 OrderSend (EA-side, when reported) · T9 broker acknowledgement

Marks accumulate on the signal/trade doc as `latency_trace` (epoch ms).
Segments make performance independently measurable per broker × symbol ×
session — commercial-grade evidence, not a claim."""
from datetime import datetime, timedelta, timezone

SEGMENTS = ["strategy_ms", "risk_authority_ms", "cloud_to_ea_ms",
            "ea_processing_ms", "broker_ms", "total_ms"]


def _session(dt: datetime) -> str:
    h = dt.astimezone(timezone.utc).hour
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "overlap"
    if 16 <= h < 21:
        return "newyork"
    return "asia"


def _diff(lt: dict, a: str, b: str):
    va, vb = lt.get(a), lt.get(b)
    if va is None or vb is None:
        return None
    d = int(vb) - int(va)
    return d if 0 <= d < 3_600_000 else None


def segments_of(lt: dict) -> dict:
    """Derived timing segments (user-facing decomposition)."""
    return {
        "strategy_ms": _diff(lt, "t0_ms", "t4_ms"),
        "risk_authority_ms": _diff(lt, "t4_ms", "t6_ms"),
        "cloud_to_ea_ms": _diff(lt, "t6_ms", "t7_ms"),
        "ea_processing_ms": _diff(lt, "t7_ms", "t8_ms"),
        "broker_ms": _diff(lt, "t8_ms", "t9_ms")
        or _diff(lt, "t7_ms", "t9_ms"),
        "total_ms": _diff(lt, "t0_ms", "t9_ms")
        or _diff(lt, "t6_ms", "t9_ms"),
    }


def _pct(vals: list, q: float):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


async def latency_summary(db, days: int = 7,
                          user_id: str | None = None) -> dict:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, min(int(days), 90)))).isoformat()
    q = {"latency_trace.t9_ms": {"$exists": True},
         "opened_at": {"$gte": cutoff}}
    if user_id:
        q["user_id"] = user_id
    brokers = {}
    async for a in db.accounts.find({}, {"broker": 1, "server": 1}):
        brokers[str(a["_id"])] = str(a.get("broker")
                                     or a.get("server") or "unknown")
    groups: dict = {}
    total = 0
    async for t in db.trades.find(
            q, {"latency_trace": 1, "symbol": 1, "account_id": 1,
                "opened_at": 1, "scope": 1}).limit(5000):
        segs = segments_of(t.get("latency_trace") or {})
        opened = t.get("opened_at")
        try:
            sess = _session(datetime.fromisoformat(
                str(opened).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            sess = "unknown"
        key = (brokers.get(str(t.get("account_id")), "unknown"),
               str(t.get("symbol") or "?"), sess)
        g = groups.setdefault(key, {s: [] for s in SEGMENTS})
        g.setdefault("_n", 0)
        g["_n"] += 1
        total += 1
        for s in SEGMENTS:
            if segs.get(s) is not None:
                g[s].append(segs[s])
    rows = []
    for (broker, symbol, sess), g in groups.items():
        row = {"broker": broker, "symbol": symbol, "session": sess,
               "trades": g["_n"]}
        for s in SEGMENTS:
            row[s] = {"p50": _pct(g[s], 0.5), "p95": _pct(g[s], 0.95),
                      "p99": _pct(g[s], 0.99),
                      "max": max(g[s]) if g[s] else None,
                      "n": len(g[s]),
                      "unknown_rate": round(1 - len(g[s]) / g["_n"], 3)
                      if g["_n"] else None}
        rows.append(row)
    rows.sort(key=lambda r: -r["trades"])
    # UNKNOWN rate — trades in the window with NO complete trace at all
    q_all = dict(q)
    q_all.pop("latency_trace.t9_ms", None)
    q_all["status"] = {"$in": ["open", "closed"]}
    all_n = await db.trades.count_documents(q_all)
    unknown_rate = round(1 - total / all_n, 3) if all_n else None
    return {"days": days, "traced_trades": total,
            "total_trades": all_n, "unknown_rate": unknown_rate,
            "groups": rows}


async def clock_skew(db, user_id: str | None = None,
                     days: int = 7) -> dict:
    """Explicit clock-skew monitoring across cloud → Host Agent/EA →
    broker. cloud_to_ea (t7−t6) can never be negative on synchronized
    clocks; a negative minimum bounds the EA/Host clock offset."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, min(int(days), 30)))).isoformat()
    q = {"latency_trace.t6_ms": {"$exists": True},
         "latency_trace.t7_ms": {"$exists": True},
         "opened_at": {"$gte": cutoff}}
    if user_id:
        q["user_id"] = user_id
    per_acct: dict = {}
    async for t in db.trades.find(
            q, {"latency_trace": 1, "account_id": 1}).limit(3000):
        lt = t["latency_trace"]
        d = int(lt["t7_ms"]) - int(lt["t6_ms"])
        per_acct.setdefault(str(t.get("account_id")), []).append(d)
    accounts = []
    for acc, ds in per_acct.items():
        mn, med = min(ds), sorted(ds)[len(ds) // 2]
        # negative min = EA clock behind cloud by at least |mn| ms
        skew_bound = min(0, mn)
        status = ("SKEW_SUSPECTED" if skew_bound < -250
                  or med > 60_000 else "OK")
        accounts.append({"account_id": acc, "n": len(ds),
                         "cloud_to_ea_min_ms": mn,
                         "cloud_to_ea_median_ms": med,
                         "skew_bound_ms": skew_bound, "status": status})
    accounts.sort(key=lambda a: a["skew_bound_ms"])
    return {"days": days, "accounts": accounts,
            "suspected": [a["account_id"] for a in accounts
                          if a["status"] != "OK"],
            "note": "t7−t6 spans network + queueing; a NEGATIVE minimum "
                    "is impossible on synchronized clocks and lower-bounds"
                    " the Host Agent/EA clock offset vs cloud"}


async def recent_traces(db, limit: int = 30,
                        user_id: str | None = None) -> list:
    q = {"latency_trace.t9_ms": {"$exists": True}}
    if user_id:
        q["user_id"] = user_id
    out = []
    async for t in db.trades.find(
            q, {"latency_trace": 1, "symbol": 1, "opened_at": 1,
                "scope": 1, "account_id": 1}).sort(
            "opened_at", -1).limit(max(1, min(int(limit), 100))):
        out.append({"trade_id": str(t.pop("_id")),
                    "symbol": t.get("symbol"),
                    "scope": t.get("scope"),
                    "opened_at": t.get("opened_at"),
                    "marks": t.get("latency_trace"),
                    "segments": segments_of(t.get("latency_trace") or {})})
    return out
