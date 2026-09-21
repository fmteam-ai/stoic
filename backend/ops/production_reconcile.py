"""P1-4 — READ-ONLY production reconciliation: prints and signs the actual
account/bot/EA truth so the release decision can attach it.

  docker compose exec -T backend python ops/production_reconcile.py [--expect 6/3/3]

Output (JSON, HMAC-signed with LEDGER_ANCHOR_KEY/JWT_SECRET):
  accounts: id, label, environment, trading_enabled (explicit bool?),
            verified identity, EA freshness, bot enabled
  totals:   accounts, explicitly_enabled, bots_enabled_on_enabled_accounts,
            fresh_ea_on_enabled, enabled_ids == bot_ids == fresh_ids
Never writes to the database. Exit 0 = matches --expect (if given), 1 = not.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass

FRESH_S = 600


def _age(hb, now):
    try:
        return (now - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", help="accounts/enabled/bots e.g. 6/3/3")
    ap.add_argument("--user-id", help="restrict to one user id")
    a = ap.parse_args()
    from database import get_db
    from broker_env import broker_environment
    db = get_db()
    now = datetime.now(timezone.utc)
    q = {"status": {"$ne": "deleted"}}
    if a.user_id:
        q["user_id"] = a.user_id
    accs = await db.accounts.find(q).to_list(length=20000)
    bots = {b["account_id"]: b for b in await db.bot_configs.find({"enabled": True}, {"account_id": 1, "strategy": 1}).to_list(length=1000)}
    rows, enabled, bot_ids, fresh = [], set(), set(), set()
    for acc in accs:
        aid = str(acc["_id"])
        flag = acc.get("trading_enabled")
        age = _age(acc.get("last_heartbeat"), now)
        vi = acc.get("verified_identity") or {}
        row = {"id": aid, "label": acc.get("label"), "user_id": acc.get("user_id"),
               "environment": broker_environment(acc), "trading_enabled": flag,
               "trading_enabled_is_bool": isinstance(flag, bool), "explicitly_enabled": flag is True,
               "verified_identity": {"account_number": vi.get("account_number"), "broker_server": vi.get("broker_server")},
               "ea_version": acc.get("ea_version"), "policy_version": acc.get("policy_version"),
               "heartbeat_age_s": None if age is None else round(age), "ea_fresh": age is not None and age <= FRESH_S,
               "bot_enabled": aid in bots, "bot_strategy": (bots.get(aid) or {}).get("strategy")}
        rows.append(row)
        if flag is True:
            enabled.add(aid)
            if aid in bots:
                bot_ids.add(aid)
            if row["ea_fresh"]:
                fresh.add(aid)
    totals = {"accounts": len(rows), "explicitly_enabled": len(enabled),
              "bots_enabled_on_enabled_accounts": len(bot_ids), "fresh_ea_on_enabled": len(fresh),
              "bots_on_disabled_accounts": sorted(set(bots) & {r["id"] for r in rows if not r["explicitly_enabled"]}),
              "non_boolean_flags": [r["id"] for r in rows if not r["trading_enabled_is_bool"]],
              "enabled_ids": sorted(enabled), "bot_ids": sorted(bot_ids), "fresh_ids": sorted(fresh),
              "sets_identical": enabled == bot_ids == fresh,
              "environments_enabled": sorted({r["environment"] for r in rows if r["explicitly_enabled"]})}
    body = {"report": "production-reconciliation", "read_only": True, "at": now.isoformat(),
            "build": os.environ.get("GIT_SHA") or "unknown", "filter": q if a.user_id else "all non-deleted accounts",
            "accounts": rows, "totals": totals}
    ok = True
    if a.expect:
        n_acc, n_en, n_bot = (int(x) for x in a.expect.split("/"))
        ok = totals["accounts"] == n_acc and totals["explicitly_enabled"] == n_en and totals["bots_enabled_on_enabled_accounts"] == n_bot \
            and not totals["bots_on_disabled_accounts"] and not totals["non_boolean_flags"] and totals["sets_identical"]
        body["expected"] = a.expect
    body["result"] = ("PASS" if ok else "FAIL") if a.expect else "REPORTED"
    key = os.environ.get("LEDGER_ANCHOR_KEY") or os.environ.get("JWT_SECRET")
    payload = json.dumps(body, sort_keys=True, default=str, separators=(",", ":")).encode()
    body["evidence_hash"] = hashlib.sha256(payload).hexdigest()
    body["signature"] = hmac.new(key.encode(), payload, hashlib.sha256).hexdigest() if key else None
    out_dir = os.environ.get("DRILL_EVIDENCE_DIR", "/tmp/drills")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"reconcile-{now.strftime('%Y%m%dT%H%M%SZ')}.json")
        json.dump(body, open(path, "w"), indent=1, default=str)
        body["evidence_file"] = path
    except OSError:
        pass
    print(json.dumps(body, indent=1, default=str))
    print(f"RECONCILE {body['result']} accounts={totals['accounts']} enabled={totals['explicitly_enabled']} bots={totals['bots_enabled_on_enabled_accounts']} fresh={totals['fresh_ea_on_enabled']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
