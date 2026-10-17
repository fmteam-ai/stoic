#!/usr/bin/env python3
"""Write tombstones for accounts that were hard-deleted BEFORE tombstones labelled broker_deals.

Deals are the audit trail and are never deleted; without a tombstone Verified Performance can only show them as
"DELETED ACCOUNT · UNKNOWN" in the archived section. This writes the missing labels — the environment is whatever the
operator states (there is no account document left to classify), so pass it explicitly or it stays UNKNOWN.

  python3 scripts/backfill_account_tombstones.py --list
  python3 scripts/backfill_account_tombstones.py --account 6abe73a2…11d14d --environment DEMO --broker RoboForex --label "RoboForex demo"
  python3 scripts/backfill_account_tombstones.py --all-orphans            # UNKNOWN environment, labels only

Never overwrites an existing tombstone. Runs against MONGO_URL / DB_NAME from backend/.env.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


async def orphan_deal_accounts(db) -> dict:
    """{account_id: {"user_id", "deals", "first", "last"}} for deal account ids with no account document."""
    live = {str(a["_id"]) async for a in db.accounts.find({}, {"_id": 1})}
    out = {}
    async for row in db.broker_deals.aggregate([
            {"$group": {"_id": "$account_id", "user_id": {"$first": "$user_id"}, "deals": {"$sum": 1},
                        "first": {"$min": "$deal_time"}, "last": {"$max": "$deal_time"}}}]):
        if row["_id"] and row["_id"] not in live:
            out[row["_id"]] = {"user_id": row.get("user_id"), "deals": row["deals"], "first": row.get("first"), "last": row.get("last")}
    return out


def tombstone_doc(aid: str, info: dict, *, environment: str, broker: str | None, label: str | None, by: str) -> dict:
    env = (environment or "UNKNOWN").upper()
    if env not in ("DEMO", "LIVE", "PAPER", "UNKNOWN"):
        raise ValueError("environment must be DEMO, LIVE, PAPER or UNKNOWN")
    return {"_id": aid, "token_hashes": [], "label": label, "broker": broker, "account_number": None,
            "deleted_at": None, "deleted_by": by, "user_id": info.get("user_id"), "mode": None, "account_type": None,
            "server": None, "environment": env, "declared_environment": env if env != "UNKNOWN" else None,
            "has_deals": True, "expires_at": None, "expires_at_iso": None, "backfilled": True,
            "backfilled_at": datetime.now(timezone.utc).isoformat(),
            "polls_after_delete": 0, "last_poll_at": None, "finding_recorded": False}


async def backfill(db, *, account: str | None, all_orphans: bool, environment: str, broker: str | None,
                   label: str | None, by: str) -> list:
    orphans = await orphan_deal_accounts(db)
    targets = list(orphans) if all_orphans else ([account] if account else [])
    written = []
    for aid in targets:
        if aid not in orphans:
            print(f"-- {aid}: not an orphan (account exists or has no deals) — skipped")
            continue
        if await db.account_tombstones.find_one({"_id": aid}, {"_id": 1}):
            print(f"-- {aid}: tombstone already present — not overwritten")
            continue
        doc = tombstone_doc(aid, orphans[aid], environment=environment, broker=broker, label=label, by=by)
        await db.account_tombstones.insert_one(doc)
        written.append(aid)
        print(f"   {aid}: tombstone written · {orphans[aid]['deals']} deals · {doc['environment']}")
    return written


async def _main(ns) -> int:
    from dotenv import dotenv_values
    from motor.motor_asyncio import AsyncIOMotorClient
    env = dotenv_values(os.path.join(ROOT, "backend", ".env"))
    db = AsyncIOMotorClient(env["MONGO_URL"])[env["DB_NAME"]]
    if ns.list:
        orphans = await orphan_deal_accounts(db)
        tombs = {t["_id"] async for t in db.account_tombstones.find({"_id": {"$in": list(orphans)}}, {"_id": 1})}
        for aid, info in sorted(orphans.items()):
            print(f"{aid}  user={info['user_id']}  deals={info['deals']}  tombstone={'yes' if aid in tombs else 'NO'}")
        print(f"{len(orphans)} orphan deal account(s)")
        return 0
    if not ns.account and not ns.all_orphans:
        print("nothing to do: pass --account <id> or --all-orphans (or --list)")
        return 2
    written = await backfill(db, account=ns.account, all_orphans=ns.all_orphans, environment=ns.environment,
                             broker=ns.broker, label=ns.label, by=f"backfill:{os.environ.get('USER') or 'operator'}")
    print(f"{len(written)} tombstone(s) written")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--account")
    ap.add_argument("--all-orphans", action="store_true")
    ap.add_argument("--environment", default="UNKNOWN", help="DEMO | LIVE | PAPER | UNKNOWN (operator-stated)")
    ap.add_argument("--broker")
    ap.add_argument("--label")
    sys.exit(asyncio.run(_main(ap.parse_args())))
