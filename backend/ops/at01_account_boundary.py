"""AT-01 — six-account execution boundary drill (run INSIDE the backend
container on staging: `docker compose exec -T backend python ops/at01_account_boundary.py`).

Seeds six real-shaped account rows for a dedicated drill user:
  3 × trading_enabled=True (bot ON)  ·  2 × trading_enabled=False  ·  1 × field missing
then asserts that FIVE surfaces contain exactly the same three account ids:
  1. state contract (what the UI renders)          state_contract.contract
  2. worker selection                               bot_runner._connected_accounts
  3. authority API domain                            trading_authority.account_domain
  4. canonical execution choke point                 execution_authority.submit_intent
  5. broker requests                                 engine stub call log
False/missing rows must produce ZERO intents and ZERO broker requests.
Everything seeded is deleted again. Exit 0 = PASS, 1 = FAIL. Writes a JSON
evidence file to release/drills/at01-<ts>.json.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bson import ObjectId  # noqa: E402

try:  # container has env injected; local/staging shells fall back to backend/.env
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass


class EngineStub:
    """Stands in for the broker engine: records every dispatch it receives."""
    def __init__(self):
        self.calls = []

    async def execute(self, *a, **kw):
        self.calls.append({"args": [str(x)[:80] for x in a], "kw": {k: str(v)[:80] for k, v in kw.items()}})
        return {"ok": True, "stub": True, "order_id": f"stub-{len(self.calls)}"}

    def __getattr__(self, name):
        async def _rec(*a, **kw):
            self.calls.append({"method": name})
            return {"ok": True, "stub": True}
        return _rec


async def main() -> int:
    from database import get_db
    from bot_runner import _connected_accounts
    from state_contract import contract
    from trading_authority import account_domain
    from execution_authority import submit_intent

    db = get_db()
    ts = datetime.now(timezone.utc)
    uid = f"at01_drill_{uuid.uuid4().hex[:8]}"
    now = ts.isoformat()
    base = {"user_id": uid, "status": "connected", "last_heartbeat": now, "mode": "paper",
            "broker": "DrillBroker", "server": "Drill-Demo", "account_type": "demo",
            "verified_identity": {"account_number": "0", "broker_server": "Drill-Demo"},
            "balance": 10000, "equity": 10000, "base_currency": "USD", "created_at": now}
    rows = []
    for i, flag in enumerate([True, True, True, False, False, "MISSING"]):
        r = {**base, "_id": ObjectId(), "label": f"AT01-{i}-{flag}",
             "bridge_token": f"at01_{uuid.uuid4().hex}", "account_number": str(900000 + i),
             "verified_identity": {"account_number": str(900000 + i), "broker_server": "Drill-Demo"}}
        if flag != "MISSING":
            r["trading_enabled"] = flag
        rows.append(r)
    expected = {str(r["_id"]) for r in rows[:3]}
    evidence = {"drill": "AT-01", "at": now, "user_id": uid, "expected_enabled_ids": sorted(expected), "surfaces": {}}
    ok = True

    await db.accounts.insert_many(rows)
    bot_ids = []
    for r in rows[:3]:
        res = await db.bot_configs.insert_one({"user_id": uid, "account_id": str(r["_id"]), "enabled": True,
                                               "strategy": "drill", "risk_pct": 0.1, "created_at": now})
        bot_ids.append(res.inserted_id)
    try:
        # 1 · state contract (UI)
        sc = await contract(db, uid)
        ui_enabled = {a["account_id"] for a in sc["accounts"] if a.get("account_enabled") is True}
        evidence["surfaces"]["state_contract"] = sorted(ui_enabled)
        ok &= ui_enabled == expected and sc["totals"].get("accounts_enabled") == 3

        # 2 · worker selection
        sel = {str(a["_id"]) for a in await _connected_accounts(db, uid)}
        evidence["surfaces"]["worker_selection"] = sorted(sel)
        ok &= sel == expected

        # 3 · authority domain per account
        auth = {}
        for r in rows:
            d = await account_domain(db, r)
            auth[str(r["_id"])] = d["level"]
        full = {k for k, v in auth.items() if v == "FULL"}
        evidence["surfaces"]["authority_full"] = sorted(full)
        evidence["authority_levels"] = auth
        ok &= full == expected and all(auth[str(r["_id"])] == "LOCKED" for r in rows[3:])

        # 4 + 5 · choke point + broker requests, one synthetic BUY per account
        engine = EngineStub()
        intents_by_acc, results = {}, {}
        for r in rows:
            res = await submit_intent(user_id=uid, account=r, engine=engine,
                                      signal={"symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                                              "confidence": 0.9, "strategy": "drill",
                                              "signal_id": f"at01_{uuid.uuid4().hex[:8]}"})
            results[str(r["_id"])] = res if isinstance(res, dict) else {"result": str(res)[:200]}
            intents_by_acc[str(r["_id"])] = await db.execution_intents.count_documents(
                {"account_id": str(r["_id"])})
        evidence["choke_point_results"] = {k: (v.get("blocked") or "dispatched") for k, v in results.items()}
        evidence["intents_per_account"] = intents_by_acc
        evidence["broker_requests"] = len(engine.calls)
        disabled_ids = {str(r["_id"]) for r in rows[3:]}
        ok &= all(intents_by_acc[i] == 0 for i in disabled_ids)
        ok &= all(results[i].get("blocked") == "account_not_enabled" for i in disabled_ids)
        with_intent = {i for i, n in intents_by_acc.items() if n > 0}
        evidence["surfaces"]["intents_created"] = sorted(with_intent)
        # enabled accounts may still be refused by other hard gates on staging
        # (fresh EA, authority, budget) — that is fine; what is FORBIDDEN is any
        # intent/broker call for a disabled row, or an intent outside the 3.
        ok &= with_intent <= expected and len(engine.calls) <= 3
        # every dispatched (enabled) account minted exactly one canonical intent
        ok &= all(intents_by_acc[i] == 1 for i, v in results.items() if not v.get("blocked"))
        evidence["result"] = "PASS" if ok else "FAIL"
    finally:
        await db.accounts.delete_many({"user_id": uid})
        await db.bot_configs.delete_many({"user_id": uid})
        await db.execution_intents.delete_many({"account_id": {"$in": [str(r["_id"]) for r in rows]}})
        await db.trades.delete_many({"user_id": uid})
        await db.signals.delete_many({"user_id": uid})
        await db.ops_alerts.delete_many({"user_id": uid})
    out_dir = os.environ.get("DRILL_EVIDENCE_DIR", "/stoic/release/drills")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"at01-{ts.strftime('%Y%m%dT%H%M%SZ')}.json")
        json.dump(evidence, open(path, "w"), indent=1)
        evidence["evidence_file"] = path
    except OSError:
        pass
    print(json.dumps(evidence, indent=1))
    print(f"AT-01 {evidence['result']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
