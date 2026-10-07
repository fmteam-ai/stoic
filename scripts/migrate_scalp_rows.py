#!/usr/bin/env python3
"""A15-7 — rewrite legacy `origin: "scalp"` trade rows to `origin: "auto"` + `engine: "scalp"`.
The API does this once at startup; run here for a dry run or on a host where the API is stopped.

    cd backend && python ../scripts/migrate_scalp_rows.py --dry-run
    cd backend && python ../scripts/migrate_scalp_rows.py
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))


async def main(dry_run: bool) -> int:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", ".env"))
    from motor.motor_asyncio import AsyncIOMotorClient
    import scalp_rows_migration as srm
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    res = await srm.migrate(db, dry_run=dry_run)
    print(f"legacy origin:scalp rows: {res['legacy']} · rewritten: {res['modified']}{' (dry run)' if dry_run else ''}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(main(ap.parse_args().dry_run)))
