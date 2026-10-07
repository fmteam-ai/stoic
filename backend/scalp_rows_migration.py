"""A15-7 — legacy scalp rows. Before N99-1 the scalp engine inserted trades with `origin: "scalp"` and
no `engine`; since then every automated row is `origin: "auto"` + `engine: "scalp" | None` and the
caps count with `trade_counter_filter()` — so the old rows were in NEITHER counter. Rewrite them once
(idempotent, audited in platform_state) so scalp and auto trades keep separate, complete daily caps."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("scalp_rows_migration")

STATE_ID = "migration_scalp_rows_v1"
LEGACY_FILTER = {"origin": "scalp"}
BATCH = 5000


def rewrite_update() -> dict:
    return {"$set": {"origin": "auto", "engine": "scalp", "legacy_origin": "scalp",
                     "origin_migrated_at": datetime.now(timezone.utc).isoformat()}}


def rewrite_row(doc: dict) -> dict:
    """Pure form of the update for tests / dry runs."""
    out = dict(doc)
    out.update(rewrite_update()["$set"])
    return out


async def count_legacy(db) -> int:
    return await db.trades.count_documents(LEGACY_FILTER)


async def migrate(db, *, dry_run: bool = False) -> dict:
    """Returns {'legacy': n_before, 'modified': n, 'dry_run': bool}. Safe to run repeatedly."""
    legacy = await count_legacy(db)
    if dry_run or legacy == 0:
        if legacy == 0 and not dry_run:
            await db.platform_state.update_one({"_id": STATE_ID}, {"$setOnInsert": {"done_at": datetime.now(timezone.utc).isoformat(), "modified": 0}}, upsert=True)
        return {"legacy": legacy, "modified": 0, "dry_run": dry_run}
    # SA7-P3 — bounded per call: at most BATCH rows so API boot is never held by a huge legacy set;
    # the remainder is picked up by the next start / the script, which loops until clean.
    ids = [d["_id"] async for d in db.trades.find(LEGACY_FILTER, {"_id": 1}).limit(BATCH)]
    res = await db.trades.update_many({"_id": {"$in": ids}, **LEGACY_FILTER}, rewrite_update())
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"done_at": datetime.now(timezone.utc).isoformat(), "remaining": max(0, legacy - res.modified_count)},
         "$inc": {"modified": res.modified_count}},
        upsert=True)
    logger.info("scalp rows migration: %d legacy origin:scalp rows → origin:auto engine:scalp (%d remaining)",
                res.modified_count, max(0, legacy - res.modified_count))
    return {"legacy": legacy, "modified": res.modified_count, "dry_run": False, "remaining": max(0, legacy - res.modified_count)}
