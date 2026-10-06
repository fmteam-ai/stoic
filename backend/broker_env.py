"""Explicit broker environment classification (iter-213 P0):

  LIVE  — real-money broker account
  DEMO  — broker demo server (practice money at a real broker)
  PAPER — STOIC-internal simulation, no broker at all

An explicit `broker_environment` field on the account always wins;
otherwise PAPER mode and demo-server naming are detected."""

ENVIRONMENTS = ("LIVE", "DEMO", "PAPER")
_DEMO_TOKENS = ("demo", "trial", "practice", "contest")
TRADE_MODES = ("demo", "real", "contest")
_REAL_MONEY_MODES = ("real", "contest")


def reported_trade_mode(account: dict) -> str | None:
    """N98-6 — ACCOUNT_TRADE_MODE as the EA (v1.60+) reported it on the latest heartbeat:
    'demo' | 'real' | 'contest' | None (older EA — nothing reported)."""
    ident = account.get("ea_identity") or {}
    tm = str(ident.get("trade_mode") or account.get("account_trade_mode") or "").lower()
    return tm if tm in TRADE_MODES else None


def broker_reports_real(account: dict) -> bool:
    """Fail-closed: ANY heartbeat (authoritative or not) saying real/contest money ⇒ LIVE."""
    ident = account.get("ea_identity") or {}
    return any(str(v or "").lower() in _REAL_MONEY_MODES
               for v in (ident.get("trade_mode"), account.get("account_trade_mode")))


def ea_binary_accepted(account: dict) -> bool:
    """N99-2/N100-5 — the terminal runs an EX5 whose hash is in the signed/pinned accepted set AND the
    hash was MEASURED by the installer (`installer_attested`). The heartbeat-reported hash is what a
    self-compiled EA would echo, so it is never evidence."""
    if str(account.get("ea_binary_sha256_method") or "") != "installer_attested":
        return False
    h = str(account.get("ea_binary_sha256") or "").lower()
    if len(h) != 64:
        return False
    try:
        from ea_capabilities import accepted_ea_sha256s
        return h in [str(x).lower() for x in accepted_ea_sha256s()]
    except Exception:  # noqa: BLE001 — no release record ⇒ nothing is accepted
        return False


def broker_reports_demo(account: dict) -> bool:
    """'demo' counts only on an AUTHORITATIVE identity chain running an ACCEPTED EX5 (N99-2): a
    self-compiled EA could send "demo" from a real account. Otherwise the legacy path applies."""
    ident = account.get("ea_identity") or {}
    return bool(ident.get("authoritative")) and str(ident.get("trade_mode") or "").lower() == "demo" \
        and not broker_reports_real(account) and ea_binary_accepted(account)


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
        # N98-6 — the broker's own ACCOUNT_TRADE_MODE (EA 1.60+): real/contest money is a
        # hard stop; 'demo' is positive evidence that supersedes the server-name heuristic.
        "broker_not_real_money": not broker_reports_real(account),
        "broker_reports_demo": broker_reports_demo(account),
    }
    mandatory_ok = all(v for k, v in checks.items() if k not in NON_MANDATORY_CHECKS)
    demo_evidence = checks["broker_reports_demo"] or checks["server_demo_named"]
    proof_id = hashlib.sha256(f"{attestation_identity(account)}|{sorted(checks.items())}|{hb}".encode()).hexdigest()[:24]
    return {"ok": mandatory_ok and demo_evidence, "mandatory_ok": mandatory_ok,
            "override_eligible": mandatory_ok and not demo_evidence,
            "checks": checks, "heartbeat_age_s": age, "reported_server": reported_server,
            "reported_trade_mode": reported_trade_mode(account), "proof_id": proof_id}


# checks that are evidence alternatives, not gates (either one satisfies the DEMO proof)
NON_MANDATORY_CHECKS = ("server_demo_named", "broker_reports_demo")


def attested_environment(account: dict) -> str:
    """Server-authoritative classification for money-sensitive gates (EX5
    binary proof, PAMM live certification). Only PAPER mode (no broker at
    all) or an admin attestation that AGREES with the declared classification
    AND still matches the bound identity digest may downgrade from LIVE —
    user-writable fields (account_type, server name, broker_environment)
    never do on their own. N98-6: a broker-reported real/contest trade mode
    voids any attestation — the broker's word beats everything."""
    if account.get("mode") == "paper":
        return "PAPER"
    if broker_reports_real(account):
        return "LIVE"
    att = account.get("environment_attestation") or {}
    if (str(att.get("environment") or "").upper() == "DEMO" and att.get("approved_by")
            and att.get("identity_hash") == attestation_identity(account)
            and (att.get("proof") or {}).get("verifier") in ("ea_heartbeat", "admin_override")
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
    # N98-6 — the broker said real money: no declared field may call it DEMO
    if account.get("mode") != "paper" and broker_reports_real(account):
        return "LIVE"
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
