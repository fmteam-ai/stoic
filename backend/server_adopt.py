"""U2 (Phase 1) — broker-server adoption from the token-authenticated EA.

The EA knows the server it is logged into; the user typing it was the single most common onboarding trap
(OnEquity-Demo vs OnEquity-Server blocked an account for good). Rule: token valid + reported MT5 login ==
the account's login + the reported server differs (or none registered) → adopt the reported server, audited
with the evidence used. A login mismatch NEVER adopts anything (guardrail: verified terminal)."""
from __future__ import annotations

from datetime import datetime, timezone

from audit_chain import append_chained
from broker_servers import servers_match

SERVER_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,63}$"


def server_change_doc(acc: dict, new_server: str, *, source: str, by: str) -> dict:
    """One Mongo update for manual edit, adopt-by-click and auto-adopt: server / broker_server /
    expected_identity.broker_server move together, the verified chain is cleared so the next heartbeat
    re-verifies against the new name, and the mismatch flag no longer blocks trust-terminal."""
    now = datetime.now(timezone.utc).isoformat()
    exp = dict(acc.get("expected_identity") or {})
    exp["broker_server"] = new_server
    return {
        "$set": {"server": new_server, "broker_server": new_server, "expected_identity": exp,
                 "broker_account_mismatch": False, "broker_account_mismatch_reason": None,
                 "server_changed": {"from": acc.get("server"), "to": new_server, "at": now, "source": source, "by": by}},
        "$unset": {"verified_identity": ""},
    }


def adoption_applies(acc: dict, reported_login, reported_server) -> bool:
    import re
    if acc.get("mode") == "paper" or not reported_server or reported_login is None:
        return False
    if not re.match(SERVER_NAME_RE, str(reported_server).strip()):
        return False
    configured_login = str(acc.get("account_number") or "").strip()
    if not configured_login or str(reported_login).strip() != configured_login:
        return False                                                   # never on a login mismatch
    current = str((acc.get("expected_identity") or {}).get("broker_server") or acc.get("broker_server") or acc.get("server") or "")
    if not current:
        return True
    return not servers_match(current, str(reported_server))


async def maybe_adopt_reported_server(db, acc: dict, reported_login, reported_server, *, installation_id=None) -> dict | None:
    """Adopt in place (mutates `acc` so the caller's identity check sees the new server). Returns the audit
    evidence when adopted, None otherwise."""
    if not adoption_applies(acc, reported_login, reported_server):
        return None
    new_server = str(reported_server).strip()
    change = server_change_doc(acc, new_server, source="ea_auto_adopt", by="system")
    await db.accounts.update_one({"_id": acc["_id"]}, change)
    evidence = {"account_id": str(acc["_id"]), "mt5_login": str(reported_login).strip(), "from": acc.get("server"),
                "to": new_server, "installation_id": installation_id, "token_authenticated": True}
    await append_chained(db, {"actor_email": "system", "action": "server_adopted_from_ea", "target_kind": "account",
                              "target_id": str(acc["_id"]), "reason": "U2 — same login, token-authenticated EA reports a different server name",
                              "at": change["$set"]["server_changed"]["at"], "meta": evidence})
    acc.update(change["$set"])
    acc.pop("verified_identity", None)
    return evidence
