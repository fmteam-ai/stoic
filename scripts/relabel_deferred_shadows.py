#!/usr/bin/env python3
"""v1.60.40 — one-off: re-label the pre-M1 deferred_shadow rows (15 Oct) with the M1 replay.

Rows stamped before v1.60.39 carry no `shadow.timeframe`; their M15 verdict ("both touched in one bar → SL first",
211/211 on 15 Oct) is replaced by the M1 replay wherever the tick store / EA M1 feed covers them, otherwise they are
tagged M15 and the Strategy Report no longer counts them as verdicts. Idempotent — the analytics sweep runs the same
pass every cycle after deploy; this script just does it now and prints what changed.

  python3 scripts/relabel_deferred_shadows.py            # all users
  python3 scripts/relabel_deferred_shadows.py --dry-run  # count only

Runs against MONGO_URL / DB_NAME from backend/.env. Touches only trade_decisions.shadow — no trading or risk state.
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


async def main(dry_run: bool) -> int:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, "backend", ".env"))
    from database import get_db
    from deferred_shadow import rescore_legacy_shadows
    db = get_db()
    q = {"status": "deferred", "shadow": {"$exists": True}, "shadow.timeframe": {"$exists": False}}
    users = await db.trade_decisions.distinct("user_id", q)
    total = await db.trade_decisions.count_documents(q)
    print(f"legacy (pre-M1) shadows: {total} across {len(users)} user(s)")
    if dry_run:
        return 0
    rescored = tagged = 0
    for uid in users:
        while True:
            out = await rescore_legacy_shadows(db, uid, limit=500)
            rescored += out["rescored"]
            tagged += out["tagged_m15"]
            if out["rescored"] + out["tagged_m15"] == 0:
                break
        print(f"  user {uid}: done")
    flipped = await db.trade_decisions.count_documents(
        {"status": "deferred", "shadow.rescored_from": {"$exists": True},
         "$expr": {"$ne": ["$shadow.outcome", "$shadow.rescored_from.outcome"]}})
    print(f"rescored on M1: {rescored} (verdict changed: {flipped}) · tagged M15 (no M1 tape): {tagged}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().dry_run)))
