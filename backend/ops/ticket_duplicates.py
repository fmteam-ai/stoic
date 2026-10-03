"""Fix plan A2/B3 + A3 — list / resolve LIVE trades sharing one (account_id, mt5_ticket[, leg]).

    docker compose exec -T backend python ops/ticket_duplicates.py                  # list (default)
    docker compose exec -T backend python ops/ticket_duplicates.py --archive        # DRY RUN of the archive plan
    docker compose exec -T backend python ops/ticket_duplicates.py --archive --yes  # apply (max --max rows, default 50)
    docker compose exec -T backend python ops/ticket_duplicates.py --build-index

The unique partial index `uniq_account_ticket` covers open/pending rows only and is refused at
boot while duplicates exist (seed.ensure_unique_ticket_index logs them). `--archive` keeps ONE
row per group and moves the rest to `trades_duplicates_archive` (reversible by hand).

Keep = the bot-originated / protected / partial-banked / richest row (see _rank). Safety checks
(each refuses the group → MANUAL):
  * any row with a pending modification / close in flight
  * more than one bot trade in the group (distinct execution_intent_id / signal_id)
  * a candidate carries realised P&L the kept row does not already hold
  * a candidate was WRITTEN in the last 10 minutes (its report may still be landing)
  * archive insert is verified before the source row is deleted; the delete is conditional on
    the row being unchanged since read (pnl, pending_modification, close_requested, status,
    updated_at) — otherwise the archive copy is removed again; hard cap per run (--max)
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

RECENT_CREATE_S = 600


def _ts(row: dict) -> str:
    return str(row.get("updated_at") or row.get("closed_at") or row.get("opened_at") or "")


def _rank(row: dict) -> tuple:
    """Higher = better row to KEEP: bot-originated (intent/signal) > protected (SL) >
    partial already banked > richer broker data > newest."""
    return (
        1 if row.get("execution_intent_id") or row.get("signal_id") else 0,
        1 if (row.get("stop_loss") or 0) > 0 else 0,
        1 if row.get("partial_closed") else 0,
        1 if row.get("position_volume") is not None else 0,
        str(row.get("opened_at") or ""),
    )


def plan_group(rows: list[dict], now: datetime | None = None) -> tuple[dict | None, list[dict], str | None]:
    """(keep, archive, refusal_reason) for one LIVE duplicate group (the index only covers
    open/pending rows, so every group is live by definition). Pure — unit-tested.

    A5/A6-H3 — refusals (→ MANUAL) are things an operator must look at, not the mere fact
    that two rows are open: a close/modification in flight, MORE THAN ONE bot trade in the
    group (distinct execution_intent_id / signal_id — never archive a real trade), realised
    P&L on a row we would drop, or a row WRITTEN in the last 10 minutes (last write, not
    creation — a report may still be landing)."""
    now = now or datetime.now(timezone.utc)
    if any(r.get("pending_modification") or r.get("close_requested") for r in rows):
        return None, [], "modification or close in flight"
    intents = {str(r["execution_intent_id"]) for r in rows if r.get("execution_intent_id")}
    signals = {str(r["signal_id"]) for r in rows if r.get("signal_id")}
    if len(intents) > 1 or len(signals) > 1:
        return None, [], (f"{max(len(intents), len(signals))} distinct bot trades in the group "
                          f"(intents={sorted(intents)} signals={sorted(signals)})")
    keep = max(rows, key=_rank)
    recent = (now - timedelta(seconds=RECENT_CREATE_S)).isoformat()
    archive = []
    for r in rows:
        if r["_id"] == keep["_id"]:
            continue
        if abs(float(r.get("pnl") or 0)) > 1e-9 and abs(float(r.get("pnl") or 0) - float(keep.get("pnl") or 0)) > 1e-9:
            return None, [], f"row {r['_id']} carries P&L {r.get('pnl')} not held by the kept row"
        if _ts(r) >= recent:
            return None, [], f"row {r['_id']} was written in the last {RECENT_CREATE_S // 60} minutes"
        archive.append(r)
    return keep, archive, None


UNCHANGED_FIELDS = ("pnl", "pending_modification", "close_requested", "status", "updated_at")


def unchanged_filter(row: dict) -> dict:
    """H3 — delete only the row exactly as it was read: any P&L / close / status write that
    landed mid-run makes the delete a no-op (the archive copy is then removed again)."""
    q = {"_id": row["_id"]}
    for k in UNCHANGED_FIELDS:
        q[k] = row.get(k) if k in row else {"$exists": False}
    return q


async def main(mode: str, apply: bool, max_rows: int) -> int:
    from database import get_db
    from seed import duplicate_tickets, ensure_unique_ticket_index
    db = get_db()
    if mode == "--build-index":
        out = await ensure_unique_ticket_index(db, rebuild=True)
        if out.get("present") and not out["created"]:
            print("index already present with the current definition")
            return 0
        print("index created" if out["created"] else f"REFUSED — {len(out['duplicates'])} duplicate group(s) remain")
        return 0 if out["created"] else 1
    groups = await duplicate_tickets(db, limit=500)
    if not groups:
        print("no duplicate live (account_id, mt5_ticket, leg) groups — index can be built")
        return 0
    archived, planned, capped = 0, 0, False
    for g in groups:
        if capped:
            break
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
                capped = True
                break
            doc = {**r, "archived_at": datetime.now(timezone.utc).isoformat(),
                   "archived_reason": "duplicate_ticket", "kept_trade_id": str(keep["_id"]),
                   "status_at_archive": r.get("status"), "status": "archived"}
            ins = await db.trades_duplicates_archive.insert_one(doc)
            if not await db.trades_duplicates_archive.find_one({"_id": ins.inserted_id}):
                print(f"ABORT: archive copy of {r['_id']} not readable back — source row kept")
                return 1
            deleted = await db.trades.delete_one(unchanged_filter(r))
            if deleted.deleted_count != 1:
                # H3 — the row changed since it was read (P&L / close / status landed mid-run)
                await db.trades_duplicates_archive.delete_one({"_id": ins.inserted_id})
                print(f"SKIPPED: {r['_id']} changed since read — source row kept, archive copy removed")
                continue
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
