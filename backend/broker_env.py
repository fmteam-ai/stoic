"""Explicit broker environment classification (iter-213 P0):

  LIVE  — real-money broker account
  DEMO  — broker demo server (practice money at a real broker)
  PAPER — STOIC-internal simulation, no broker at all

An explicit `broker_environment` field on the account always wins;
otherwise PAPER mode and demo-server naming are detected."""

ENVIRONMENTS = ("LIVE", "DEMO", "PAPER")
_DEMO_TOKENS = ("demo", "trial", "practice", "contest")


def attestation_identity(account: dict) -> str:
    """Canonical identity digest an admin DEMO attestation is bound to: immutable
    account id, normalized broker, declared + EA-reported broker server, broker
    account number, verified terminal/installation identity and the credential
    version. Any change ⇒ digest differs ⇒ attestation is void (never inherited
    by a replaced record or changed credential)."""
    import hashlib
    import json
    ident = account.get("ea_identity") or {}
    parts = {
        "account_id": str(account.get("_id") or ""),
        "broker": str(account.get("broker") or "").strip().lower(),
        "server": str(account.get("server") or "").strip().lower(),
        "broker_server": str(account.get("broker_server") or "").strip().lower(),
        "account_number": str(account.get("account_number") or ""),
        "account_type": str(account.get("account_type") or "").lower(),
        "installation_id": str(ident.get("installation_id") or ""),
        "terminal_login": str(account.get("broker_account_id_reported") or ""),
        "creds_version": int(account.get("creds_version") or 0),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def attested_environment(account: dict) -> str:
    """Server-authoritative classification for money-sensitive gates (EX5
    binary proof, PAMM live certification). Only PAPER mode (no broker at
    all) or an admin attestation that AGREES with the declared classification
    AND still matches the bound identity digest may downgrade from LIVE —
    user-writable fields (account_type, server name, broker_environment)
    never do on their own."""
    if account.get("mode") == "paper":
        return "PAPER"
    att = account.get("environment_attestation") or {}
    if (str(att.get("environment") or "").upper() == "DEMO" and att.get("approved_by")
            and att.get("identity_hash") == attestation_identity(account)
            and broker_environment(account) == "DEMO"):
        return "DEMO"
    return "LIVE"


def attestation_state(account: dict) -> str:
    """none | valid | invalidated (bound identity changed since approval)."""
    att = account.get("environment_attestation") or {}
    if not att.get("approved_by"):
        return "none"
    return "valid" if attested_environment(account) == "DEMO" else "invalidated"


def broker_environment(account: dict) -> str:
    explicit = str(account.get("broker_environment") or "").upper()
    if explicit in ENVIRONMENTS:
        return explicit
    if account.get("mode") == "paper":
        return "PAPER"
    # audit v4 P0-2 — the account's own declared type wins over server-name
    # heuristics; a record showing TYPE · DEMO must never classify as LIVE.
    if str(account.get("account_type") or "").lower() == "demo":
        return "DEMO"
    server = str(account.get("broker_server") or account.get("server")
                 or "").lower()
    if any(t in server for t in _DEMO_TOKENS):
        return "DEMO"
    return "LIVE"
