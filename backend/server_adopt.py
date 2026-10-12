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


MANUAL_LOCK_HOURS = 24          # H3/M5 — a manual edit (owner_edit / ea_reported click) is never undone by auto-adopt within this window
FLIPFLOP_WINDOW_HOURS = 6       # H3 — two terminals switching the name back and forth → finding, no adoption
MANUAL_SOURCES = ("owner_edit", "ea_reported")


def _age_h(iso) -> float | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def manual_lock_active(acc: dict) -> bool:
    ch = acc.get("server_changed") or {}
    age = _age_h(ch.get("at"))
    return ch.get("source") in MANUAL_SOURCES and age is not None and age < MANUAL_LOCK_HOURS


def flip_flop(acc: dict, new_server: str) -> bool:
    """The previous auto-adoption moved AWAY from `new_server` recently → a second terminal is fighting it."""
    ch = acc.get("server_changed") or {}
    age = _age_h(ch.get("at"))
    return (ch.get("source") == "ea_auto_adopt" and age is not None and age < FLIPFLOP_WINDOW_HOURS
            and bool(ch.get("from")) and servers_match(str(ch.get("from")), new_server))


async def identity_passes_except_server(db, acc: dict, installation_id, reported_login) -> bool:
    """H3 — every link of the identity chain EXCEPT the server name: active installation bound to this account,
    same MT5 login, and the installation holds the execution lease."""
    if not installation_id:
        return False
    inst = await db.installations.find_one({"installation_id": installation_id, "revoked": {"$ne": True}})
    if not inst or str(inst.get("account_id")) != str(acc["_id"]):
        return False
    if str(reported_login).strip() != str(acc.get("account_number") or "").strip():
        return False
    lease = await db.execution_leases.find_one({"account_id": str(acc["_id"])})
    return bool(lease and not lease.get("revoked") and lease.get("installation_id") == installation_id)


async def _conflict_finding(db, acc: dict, new_server: str, installation_id) -> None:
    from security_agent.checks.common import f
    from security_agent.findings import open_or_update
    ch = acc.get("server_changed") or {}
    await open_or_update(db, f("server_flipflop", f"account:{acc['_id']}", "medium", "bridge",
                               f"Two terminals report different broker servers for account {acc.get('label') or acc['_id']} "
                               f"(#{acc.get('account_number')}): '{acc.get('server')}' vs '{new_server}' within {FLIPFLOP_WINDOW_HOURS} h — "
                               "auto-adoption stopped; remove the EA from the terminal that is not yours",
                               {"account_id": str(acc["_id"]), "current": acc.get("server"), "reported": new_server,
                                "previous_adoption": ch, "installation_id": installation_id}))


async def maybe_adopt_reported_server(db, acc: dict, reported_login, reported_server, *, installation_id=None,
                                      require_identity: bool = True) -> dict | None:
    """Adopt in place (mutates `acc`). Returns the audit evidence when adopted, None otherwise.
    H3 (main127): requires the identity chain minus the server link, refuses during the manual-edit lock and
    files a finding instead of adopting when two terminals keep switching the name."""
    if not adoption_applies(acc, reported_login, reported_server):
        return None
    if require_identity and not await identity_passes_except_server(db, acc, installation_id, reported_login):
        return None
    new_server = str(reported_server).strip()
    if manual_lock_active(acc):
        return None
    if flip_flop(acc, new_server):
        try:
            await _conflict_finding(db, acc, new_server, installation_id)
        except Exception:  # noqa: BLE001 — the refusal is the contract; the finding is a courtesy
            pass
        return None
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
