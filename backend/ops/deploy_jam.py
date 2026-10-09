"""M114-2 — deploy-jam marker: when deploy/update.sh stops on overlay EBUSY it stops worker-trading and
records TRADING PAUSED here; readiness shows it until a clean recreate clears it.
    python ops/deploy_jam.py set --reason "…"     (run inside the backend container by update.sh)
    python ops/deploy_jam.py clear
    python ops/deploy_jam.py status"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DOC_ID = "deploy_jam"


async def _run(args) -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from secrets_loader import resolve_file_secrets
    resolve_file_secrets()
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    if args.cmd == "set":
        doc = {"_id": DOC_ID, "trading_paused": True, "reason": args.reason,
               "at": datetime.now(timezone.utc).isoformat(), "by": "deploy/update.sh"}
        await db.platform_state.replace_one({"_id": DOC_ID}, doc, upsert=True)
        print(json.dumps(doc))
    elif args.cmd == "clear":
        r = await db.platform_state.delete_one({"_id": DOC_ID})
        print(json.dumps({"cleared": r.deleted_count}))
    else:
        print(json.dumps(await db.platform_state.find_one({"_id": DOC_ID}) or {"trading_paused": False}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=("set", "clear", "status"))
    p.add_argument("--reason", default="deploy jammed on overlay EBUSY — worker-trading stopped pending reboot")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
