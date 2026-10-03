"""Fix plan A2/B3 + A3 — list / resolve LIVE trades sharing one (account_id, mt5_ticket[, leg]).

    docker compose exec -T backend python ops/ticket_duplicates.py                  # list (default)
    docker compose exec -T backend python ops/ticket_duplicates.py --archive        # DRY RUN of the archive plan
    docker compose exec -T backend python ops/ticket_duplicates.py --archive --yes  # apply (max --max rows, default 50)
    docker compose exec -T backend python ops/ticket_duplicates.py --build-index

The unique partial index `uniq_account_ticket` covers open/pending rows only and is refused at
boot while duplicates exist (seed.ensure_unique_ticket_index logs them). `--archive` keeps ONE
row per group and moves the rest to `trades_duplicates_archive` (reversible by hand).

Safety checks (every one refuses the group → MANUAL):
  * more than one OPEN row, or any row with a pending modification / close in flight
  * a candidate carries realised P&L the kept row does not already hold
  * a candidate was touched in the last 10 minutes (a live writer may still be on it)
  * archive insert is verified before the source row is deleted; hard cap per run (--max)
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass
from secrets_loader import resolve_file_secrets  # noqa: E402
resolve_file_secrets()

RECENT_TOUCH_S = 600


def _ts(row: dict) -> str:
    return str(row.get("updated_at") or row.get("closed_at") or row.get("opened_at") or "")


def plan_group(rows: list[dict], now: datetime | None = None) -> tuple[dict | None, list[dict], str | None]:
    """(keep, archive, refusal_reason). Pure — unit-tested."""
    now = now or datetime.now(timezone.utc)
    opens = [r for r in rows if r.get("status") in ("open", "pending")]
    if len(opens) > 1:
        return None, [], "more than one open/pending row"
    if any(r.get("pending_modification") or r.get("close_requested") for r in rows):
        return None, [], "modification or close in flight"
    keep = opens[0] if opens else max(rows, key=_ts)
    recent = (now - timedelta(seconds=RECENT_TOUCH_S)).isoformat()
    archive = []
    for r in rows:
        if r["_id"] == keep["_id"]:
            continue
        if abs(float(r.get("pnl") or 0)) > 1e-9 and abs(float(r.get("pnl") or 0) - float(keep.get("pnl") or 0)) > 1e-9:
            return None, [], f"row {r['_id']} carries P&L {r.get('pnl')} not held by the kept row"
        if _ts(r) >= recent:
            return None, [], f"row {r['_id']} was written in the last {RECENT_TOUCH_S // 60} minutes"
        archive.append(r)
    return keep, archive, None


async def main(mode: str, apply: bool, max_rows: int) -> int:
    from database import get_db
    from seed import duplicate_tickets, ensure_unique_ticket_index
    db = get_db()
    if mode == "--build-index":
        out = await ensure_unique_ticket_index(db)
        print("index created" if out["created"] else f"REFUSED — {len(out['duplicates'])} duplicate group(s) remain")
        return 0 if out["created"] else 1
    groups = await duplicate_tickets(db, limit=500)
    if not groups:
        print("no duplicate live (account_id, mt5_ticket, leg) groups — index can be built")
        return 0
    archived, planned = 0, 0
    for g in groups:
        q = {"account_id": g["account_id"], "mt5_ticket": g["mt5_ticket"], "status": {"$in": ["open", "pending"]}}
        if g.get("position_leg") is None:
            q["position_leg"] = None
        else:
            q["position_leg"] = g["position_leg"]
        rows = await db.trades.find(q).to_list(length=100)
        keep, archive, refusal = plan_group(rows)
        print(f"account={g['account_id']} ticket={g['mt5_ticket']} leg={g.get('position_leg')} rows={len(rows)}"
              + (f"  → MANUAL: {refusal}" if refusal else ""))
        for r in rows:
            tag = "KEEP" if keep is not None and r["_id"] == keep["_id"] else ("MANUAL" if refusal else "archive")
            print(f"   [{tag}] {r['_id']} status={r.get('status')} lots={r.get('lot_size')} "
                  f"opened={r.get('opened_at')} closed={r.get('closed_at')} pnl={r.get('pnl')}")
        if mode != "--archive" or refusal:
            continue
        planned += len(archive)
        if not apply:
            continue
        for r in archive:
            if archived >= max_rows:
                print(f"cap reached (--max {max_rows}) — re-run to continue")
                break
            doc = {**r, "archived_at": datetime.now(timezone.utc).isoformat(),
                   "archived_reason": "duplicate_ticket", "kept_trade_id": str(keep["_id"])}
            ins = await db.trades_duplicates_archive.insert_one(doc)
            if not await db.trades_duplicates_archive.find_one({"_id": ins.inserted_id}):
                print(f"ABORT: archive copy of {r['_id']} not readable back — source row kept")
                return 1
            await db.trades.delete_one({"_id": r["_id"]})
            archived += 1
    if mode == "--archive":
        if not apply:
            print(f"DRY RUN — {planned} row(s) would be archived; re-run with --yes to apply")
        else:
            remaining = await duplicate_tickets(db)
            print(f"archived {archived} duplicate row(s) to trades_duplicates_archive; "
                  f"remaining duplicate groups: {len(remaining)}"
                  + (" — run --build-index" if not remaining else " (resolve MANUAL groups)"))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = next((a for a in args if a in ("--list", "--archive", "--build-index")), "--list")
    if any(a.startswith("--") and a not in ("--list", "--archive", "--build-index", "--yes") and not a.startswith("--max") for a in args):
        print(__doc__)
        sys.exit(2)
    cap = next((int(a.split("=", 1)[1]) for a in args if a.startswith("--max=")), 50)
    sys.exit(asyncio.run(main(mode, "--yes" in args, cap)))
