"""#46 — one-off DRY-RUN report: effective break-even trigger per bot config BEFORE v1.60.55 (runtime read the raw
stored value: `cfg.get("breakeven_trigger_r") or 0` → presets' 0.4/0.5/0.6, or DISABLED when unset) and AFTER
(models.effective_breakeven_trigger_r: shared default 1.0R, breakeven_enabled honoured). Nothing is written.

    python scripts/report_breakeven_migration.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))


def before_value(cfg: dict) -> float:
    try:
        return float(cfg.get("breakeven_trigger_r") or 0)
    except (TypeError, ValueError):
        return 0.0


async def report() -> list[dict]:
    from motor.motor_asyncio import AsyncIOMotorClient
    from models import effective_breakeven_trigger_r
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    rows = []
    async for cfg in db.bot_configs.find({}, {"user_id": 1, "account_id": 1, "breakeven_trigger_r": 1, "breakeven_enabled": 1,
                                             "preset": 1, "active_preset": 1, "enabled": 1}):
        b, a = before_value(cfg), effective_breakeven_trigger_r(cfg)
        rows.append({"config_id": str(cfg["_id"]), "user_id": cfg.get("user_id"), "account_id": cfg.get("account_id") or "default",
                     "enabled": cfg.get("enabled"), "stored": cfg.get("breakeven_trigger_r"), "breakeven_enabled": cfg.get("breakeven_enabled"),
                     "before_r": b, "after_r": a, "changes": abs(a - b) > 1e-9})
    return rows


def main() -> None:
    rows = asyncio.run(report())
    changed = [r for r in rows if r["changes"]]
    print(f"DRY RUN — {len(rows)} bot configs, {len(changed)} effective break-even values change on deploy:")
    for r in rows:
        tag = "CHANGE" if r["changes"] else "same  "
        print(f"  {tag} cfg={r['config_id']} acct={r['account_id']:<26} enabled={r['enabled']!s:<5} stored={r['stored']!s:<5} "
              f"be_enabled={r['breakeven_enabled']!s:<5} before={r['before_r']:g}R → after={r['after_r']:g}R")


if __name__ == "__main__":
    main()
