"""Explicit broker environment classification (iter-213 P0):

  LIVE  — real-money broker account
  DEMO  — broker demo server (practice money at a real broker)
  PAPER — STOIC-internal simulation, no broker at all

An explicit `broker_environment` field on the account always wins;
otherwise PAPER mode and demo-server naming are detected."""
import os

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


def _norm(v) -> str:
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def demo_proof(account: dict, *, max_heartbeat_age_s: int = 600, now=None) -> dict:
    """audit r29 P1-02 — independent evidence that the connected endpoint is DEMO.
    Every check except `server_demo_named` is mandatory; that one may be replaced
    by an explicitly audited admin override (verifier=admin_override)."""
    import hashlib
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    ident = account.get("ea_identity") or {}
    reported_server = ident.get("broker_server") or account.get("broker_server") or ""
    hb = account.get("last_heartbeat")
    age = None
    if hb:
        try:
            age = int((now - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))).total_seconds())
        except ValueError:
            age = None
    checks = {
        "terminal_identity_authoritative": bool(ident.get("authoritative")) and bool(ident.get("installation_id")),
        "server_reported_matches_declared": bool(reported_server) and _norm(reported_server) == _norm(account.get("server")),
        "login_reported_matches_account": bool(account.get("broker_account_id_reported"))
                                           and str(account.get("broker_account_id_reported")) == str(account.get("account_number") or ""),
        "heartbeat_fresh": age is not None and 0 <= age <= max_heartbeat_age_s,
        "no_live_capital_indicator": str(account.get("account_type") or "").lower() not in ("live", "real")
                                      and str(account.get("broker_environment") or "").upper() != "LIVE"
                                      and not account.get("broker_account_mismatch"),
        "server_demo_named": any(t in str(reported_server).lower() for t in _DEMO_TOKENS),
    }
    mandatory_ok = all(v for k, v in checks.items() if k != "server_demo_named")
    proof_id = hashlib.sha256(f"{attestation_identity(account)}|{sorted(checks.items())}|{hb}".encode()).hexdigest()[:24]
    return {"ok": mandatory_ok and checks["server_demo_named"], "mandatory_ok": mandatory_ok,
            "override_eligible": mandatory_ok and not checks["server_demo_named"],
            "checks": checks, "heartbeat_age_s": age, "reported_server": reported_server, "proof_id": proof_id}


# DEMO is a LEASE, not a permanent label (audit v2 P1-04): it holds only while
# the terminal keeps proving it (fresh heartbeat) and the admin approval is
# recent. A lapsed lease collapses to LIVE for every money-sensitive gate.
DEMO_LEASE_MAX_HEARTBEAT_AGE_S = int(os.environ.get("DEMO_LEASE_MAX_HEARTBEAT_AGE_S", "600"))
DEMO_ATTESTATION_MAX_AGE_DAYS = int(os.environ.get("DEMO_ATTESTATION_MAX_AGE_DAYS", "30"))


def _age_seconds(stamp) -> float | None:
    from datetime import datetime, timezone
    if not stamp:
        return None
    try:
        dt = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def demo_lease_lapse_reason(account: dict) -> str | None:
    """Why an otherwise identity-valid DEMO attestation no longer holds, or None."""
    hb_age = _age_seconds(account.get("last_heartbeat"))
    if hb_age is None or hb_age > DEMO_LEASE_MAX_HEARTBEAT_AGE_S:
        return "heartbeat_stale"
    att = account.get("environment_attestation") or {}
    if DEMO_ATTESTATION_MAX_AGE_DAYS > 0:
        att_age = _age_seconds(att.get("at"))
        if att_age is None or att_age > DEMO_ATTESTATION_MAX_AGE_DAYS * 86400:
            return "attestation_expired"
    return None


def _identity_attested_demo(account: dict) -> bool:
    att = account.get("environment_attestation") or {}
    return bool(str(att.get("environment") or "").upper() == "DEMO" and att.get("approved_by")
                and att.get("identity_hash") == attestation_identity(account)
                and (att.get("proof") or {}).get("verifier") in ("ea_heartbeat", "admin_override")
                and broker_environment(account) == "DEMO")


def attested_environment(account: dict) -> str:
    """Server-authoritative classification for money-sensitive gates (EX5
    binary proof, PAMM live certification). Only PAPER mode (no broker at
    all) or an admin attestation that AGREES with the declared classification
    AND still matches the bound identity digest may downgrade from LIVE —
    user-writable fields (account_type, server name, broker_environment)
    never do on their own."""
    if account.get("mode") == "paper":
        return "PAPER"
    if _identity_attested_demo(account) and demo_lease_lapse_reason(account) is None:
        return "DEMO"
    return "LIVE"


def attestation_state(account: dict) -> str:
    """none | valid | invalidated (bound identity changed since approval) |
    lapsed (identity still matches, but the DEMO lease is not currently
    proven: stale heartbeat or expired approval)."""
    att = account.get("environment_attestation") or {}
    if not att.get("approved_by"):
        return "none"
    if not _identity_attested_demo(account):
        return "invalidated"
    return "lapsed" if demo_lease_lapse_reason(account) else "valid"


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
