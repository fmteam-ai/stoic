"""#40 — recompute realized R for closed trades with the corrected contract-size resolution.

    python scripts/recompute_r_multiples.py            # dry run: report what would change (default)
    python scripts/recompute_r_multiples.py --apply    # write trade.r_multiple / r_source on closed trades

Compares OLD rule (raw symbol → table XAU/XAG/BTC/ETH else contract 1, clamped ±10R) with the NEW rule
(base_symbol → account spec / instrument table, None when unknown). Never touches pnl or any trading field.
"""
import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

_OLD_CS = {"XAUUSD": 100, "XAGUSD": 5000, "BTCUSD": 1, "ETHUSD": 1}


def old_r(t: dict):
    try:
        entry, sl, lot, pnl = (float(t.get("entry_price") or 0), float(t.get("stop_loss") or 0),
                               float(t.get("lot_size") or 0), float(t.get("pnl") or 0))
        if entry <= 0 or sl <= 0 or lot <= 0:
            return None
        risk = abs(entry - sl) * lot * _OLD_CS.get((t.get("symbol") or "").upper(), 1)
        return max(-10.0, min(10.0, pnl / risk)) if risk > 0 else None
    except (TypeError, ValueError):
        return None


async def run(apply: bool) -> dict:
    from motor.motor_asyncio import AsyncIOMotorClient
    from analytics import account_specs, trade_r_multiple
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    specs_by_user: dict = {}
    changes, by_symbol, unknown_cs = [], Counter(), Counter()
    total = 0
    async for t in db.trades.find({"status": "closed"}):
        total += 1
        uid = str(t.get("user_id") or "")
        if uid not in specs_by_user:
            specs_by_user[uid] = await account_specs(db, uid)
        acct = specs_by_user[uid].get(str(t.get("account_id") or ""))
        new = trade_r_multiple(t, acct)
        old = old_r(t)
        stored = t.get("r_multiple")
        if new is None and old is not None:
            unknown_cs[t.get("symbol")] += 1
        if (new is None) != (old is None) or (new is not None and old is not None and abs(new - old) > 0.005):
            by_symbol[t.get("symbol")] += 1
            changes.append({"trade_id": str(t["_id"]), "ticket": t.get("ticket") or t.get("broker_ticket"),
                            "symbol": t.get("symbol"), "pnl": t.get("pnl"), "old_r": old, "new_r": new})
        if apply and (stored != new):
            await db.trades.update_one({"_id": t["_id"]}, {"$set": {
                "r_multiple": new, "r_source": "contract_v2",
                "r_recomputed_at": datetime.now(timezone.utc).isoformat()}})
    return {"closed_trades": total, "changed": len(changes), "by_symbol": dict(by_symbol),
            "unknown_contract_size": dict(unknown_cs), "changes": changes, "applied": apply}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write r_multiple onto closed trades (default: dry run)")
    ap.add_argument("--limit", type=int, default=25, help="rows to print")
    a = ap.parse_args()
    res = asyncio.run(run(a.apply))
    print(f"{'APPLIED' if a.apply else 'DRY RUN'} — closed trades: {res['closed_trades']}, R changed: {res['changed']}")
    print("changed by symbol:", res["by_symbol"])
    print("unknown contract size (now None, was clamped):", res["unknown_contract_size"])
    for c in res["changes"][:a.limit]:
        print(f"  {c['symbol']:<12} {c['trade_id']} pnl={c['pnl']} old={c['old_r']} new={c['new_r']}")


if __name__ == "__main__":
    main()
