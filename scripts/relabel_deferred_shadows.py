#!/usr/bin/env python3
"""Re-label deferred_shadow rows (trade_decisions.status == "deferred").

v1.60.40 — rows stamped before the M1 replay (no `shadow.timeframe`) were re-run on M1 bars.
v1.60.49 — two label bugs invalidated EVERY pre-v2 label: the Execution Optimizer stored the deferred signal with
action HOLD and ablation.replay_outcome replayed HOLD as SELL (#33), and scalp_ticks.tm is broker time (UTC+3) but
was bucketed as UTC (#34). Labels now carry `shadow.label_version` (2). Default run re-scores every label below the
current version; `--all` re-scores EVERY shadow. The previous label is always kept in `shadow.rescored_from`; rows
without M1 coverage are marked `shadow.invalid` (never scored) instead of keeping a wrong verdict.

  python3 scripts/relabel_deferred_shadows.py            # labels below the current version (idempotent)
  python3 scripts/relabel_deferred_shadows.py --all      # re-score every deferred shadow
  python3 scripts/relabel_deferred_shadows.py --dry-run  # count only

Shipped in the backend image (/app/scripts) since v1.60.49:
  docker compose exec backend python /app/scripts/relabel_deferred_shadows.py --all
Runs against MONGO_URL / DB_NAME from backend/.env. Touches only trade_decisions.shadow — no trading or risk state.
"""
import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend") if os.path.isdir(os.path.join(ROOT, "backend")) else "/app/backend")


async def main(dry_run: bool, rescore_all: bool) -> int:
    from dotenv import load_dotenv
    for env in (os.path.join(ROOT, "backend", ".env"), "/app/backend/.env"):
        if os.path.exists(env):
            load_dotenv(env)
            break
    from database import get_db
    from deferred_shadow import LABEL_VERSION, _legacy_q, rescore_legacy_shadows
    db = get_db()
    base_q = {"status": "deferred", "shadow": {"$exists": True}}
    q = base_q if rescore_all else {k: v for k, v in _legacy_q("").items() if k != "user_id"}
    users = await db.trade_decisions.distinct("user_id", q)
    total = await db.trade_decisions.count_documents(q)
    print(f"{'ALL' if rescore_all else f'below label v{LABEL_VERSION}'}: {total} shadow(s) across {len(users)} user(s)")
    if dry_run:
        return 0
    rescored = tagged = kept = 0
    for uid in users:
        after = None
        while True:                                   # v1.60.50 — _id cursor: every row visited exactly once
            out = await rescore_legacy_shadows(db, uid, limit=500, rescore_all=rescore_all, after_id=after)
            rescored += out["rescored"]
            tagged += out["tagged_m15"]
            kept += out.get("kept", 0)
            after = out.get("next_after_id")
            if not out.get("visited") or after is None:
                break
        print(f"  user {uid}: done")
    flipped = await db.trade_decisions.count_documents(
        {"status": "deferred", "shadow.rescored_from": {"$exists": True},
         "$expr": {"$ne": ["$shadow.outcome", "$shadow.rescored_from.outcome"]}})
    invalid = await db.trade_decisions.count_documents({"status": "deferred", "shadow.invalid": True})
    print(f"re-scored: {rescored} (verdict changed vs original: {flipped}) · kept valid v2 (tape expired): {kept} · "
          f"no bars at all → marked invalid: {tagged} (total invalid now: {invalid})")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="re-score every deferred shadow, not only pre-v2 labels")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.dry_run, a.all)))
