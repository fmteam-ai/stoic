"""Fix plan A2/B3 — list / resolve trades sharing one (account_id, mt5_ticket).

    docker compose exec -T backend python ops/ticket_duplicates.py            # list (default)
    docker compose exec -T backend python ops/ticket_duplicates.py --archive  # move non-open dups
    docker compose exec -T backend python ops/ticket_duplicates.py --build-index

The unique partial index `uniq_account_ticket` is refused at boot while duplicates exist
(seed.ensure_unique_ticket_index logs them). `--archive` keeps ONE row per (account, ticket)
— the open one, else the newest — and moves the rest to `trades_duplicates_archive`
(reversible by hand). Rows where TWO are open are never touched: resolve those manually.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass
from secrets_loader import resolve_file_secrets  # noqa: E402
resolve_file_secrets()


def _keep(rows: list[dict]) -> dict | None:
    opens = [r for r in rows if r.get("status") == "open"]
    if len(opens) > 1:
        return None
    if opens:
        return opens[0]
    return max(rows, key=lambda r: str(r.get("opened_at") or ""))


async def main(mode: str) -> int:
    from database import get_db
    from seed import duplicate_tickets, ensure_unique_ticket_index
    db = get_db()
    groups = await duplicate_tickets(db, limit=500)
    if mode == "--build-index":
        out = await ensure_unique_ticket_index(db)
        print("index created" if out["created"] else f"REFUSED — {len(out['duplicates'])} duplicate group(s) remain")
        return 0 if out["created"] else 1
    if not groups:
        print("no duplicate (account_id, mt5_ticket) groups — index can be built")
        return 0
    archived = 0
    for g in groups:
        rows = await db.trades.find({"account_id": g["account_id"], "mt5_ticket": g["mt5_ticket"]}).to_list(length=100)
        keep = _keep(rows)
        print(f"account={g['account_id']} ticket={g['mt5_ticket']} rows={len(rows)}")
        for r in rows:
            tag = "KEEP" if keep is not None and r["_id"] == keep["_id"] else ("MANUAL" if keep is None else "dup")
            print(f"   [{tag}] {r['_id']} status={r.get('status')} lots={r.get('lot_size')} "
                  f"opened={r.get('opened_at')} closed={r.get('closed_at')} pnl={r.get('pnl')}")
        if mode == "--archive" and keep is not None:
            for r in rows:
                if r["_id"] == keep["_id"]:
                    continue
                r["archived_at"] = datetime.now(timezone.utc).isoformat()
                r["archived_reason"] = "duplicate_ticket"
                r["kept_trade_id"] = str(keep["_id"])
                await db.trades_duplicates_archive.insert_one(r)
                await db.trades.delete_one({"_id": r["_id"]})
                archived += 1
    if mode == "--archive":
        print(f"archived {archived} duplicate row(s) to trades_duplicates_archive")
        remaining = await duplicate_tickets(db)
        print("remaining duplicate groups:", len(remaining), "— run --build-index" if not remaining else "(resolve MANUAL groups)")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--list"
    if arg not in ("--list", "--archive", "--build-index"):
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(arg)))
