"""Account identity structure (iter-125, correction #2).

Every account carries three distinct identity layers:
  • display_name        — user label, PRESENTATION ONLY, never used in logic
  • expected_identity   — what the user CLAIMS at registration
                          {account_number, broker_server}
  • verified_identity   — what the broker API actually REPORTED through a
                          fully verified EA heartbeat chain
                          {account_number, broker_server, installation_id,
                           verified_at}
Authority order: verified_identity > broker-reported fields > expected.
"""
from datetime import datetime, timezone


def authoritative_account_number(acc: dict):
    """Broker-verified login wins; the user-entered account_number is the
    LAST resort (pre-verification convenience only)."""
    ver = acc.get("verified_identity") or {}
    return (ver.get("account_number")
            or acc.get("broker_account_id_reported")
            or (acc.get("expected_identity") or {}).get("account_number")
            or acc.get("account_number"))


def expected_account_number(acc: dict):
    return ((acc.get("expected_identity") or {}).get("account_number")
            or acc.get("account_number"))


def expected_broker_server(acc: dict):
    return ((acc.get("expected_identity") or {}).get("broker_server")
            or acc.get("server"))


def build_verified_identity(*, account_number, broker_server,
                            installation_id) -> dict:
    return {"account_number": (str(account_number)
                               if account_number is not None else None),
            "broker_server": broker_server,
            "installation_id": installation_id,
            "verified_at": datetime.now(timezone.utc).isoformat()}


async def backfill_identity_structure(db) -> int:
    """Idempotent startup migration — stamp display_name + expected_identity
    onto accounts created before iter-125."""
    n = 0
    cursor = db.accounts.find({"expected_identity": {"$exists": False}},
                              {"label": 1, "account_number": 1, "server": 1,
                               "display_name": 1})
    async for a in cursor:
        set_doc = {"expected_identity": {
            "account_number": a.get("account_number"),
            "broker_server": a.get("server")}}
        if not a.get("display_name"):
            set_doc["display_name"] = a.get("label")
        await db.accounts.update_one({"_id": a["_id"]}, {"$set": set_doc})
        n += 1
    return n
