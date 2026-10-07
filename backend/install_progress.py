"""Installer progress — pure derivation of the VPS pairing/installation state for ONE account
from data the platform already holds (pairing_tokens, installations, account heartbeat fields).
Read-only, grants nothing. Statuses: done | waiting | blocked | warn | pending."""
from __future__ import annotations

import os
from datetime import datetime, timezone

HEARTBEAT_FRESH_S = 120          # EA heartbeats every few seconds; 2 min without = not talking
FIRST_HEARTBEAT_GRACE_S = 120    # after pairing, the operator still has to attach the EA
MIN_INSTALLER_VERSION = "1.3"    # N104-1 — older installers failed the EA download


def _iso(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_s(now: datetime, v) -> float | None:
    d = _iso(v)
    return None if d is None else (now - d).total_seconds()


def _fmt_age(s: float | None) -> str:
    if s is None:
        return "never"
    s = max(0, int(s))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def _step(sid, title, status, detail="", hint=""):
    return {"id": sid, "title": title, "status": status, "detail": detail, "hint": hint}


def _version_lt(a: str | None, b: str) -> bool:
    def t(v):
        try:
            return tuple(int(x) for x in str(v).split("."))
        except ValueError:
            return (0,)
    return not a or t(a) < t(b)


def webrequest_url(request_base: str | None = None, forwarded_proto: str | None = None, forwarded_host: str | None = None) -> str:
    """The origin MT5 must allow for WebRequest: PUBLIC_BACKEND_URL, else the public origin the
    browser reached us through (ingress forwarded headers), else the raw request base."""
    env = (os.environ.get("PUBLIC_BACKEND_URL") or "").strip().rstrip("/")
    if env:
        return env
    host = (forwarded_host or "").split(",")[0].strip()
    if host:
        proto = (forwarded_proto or "https").split(",")[0].strip() or "https"
        return f"{proto}://{host}"
    return (request_base or "").rstrip("/")


def derive(account: dict, pairing: dict | None, installation: dict | None, *, attested: tuple,
           accepted_hashes: list[str], now: datetime | None = None, request_base: str | None = None,
           forwarded_proto: str | None = None, forwarded_host: str | None = None) -> dict:
    """`attested` = device_attestation.attested_hash(installation) → (method, measured_hash)."""
    now = now or datetime.now(timezone.utc)
    steps = []
    url = webrequest_url(request_base, forwarded_proto, forwarded_host)
    trusted_only = bool(installation) and not account.get("installer_paired_at")   # one-click trusted terminal, no installer run

    # 1 · pairing token
    paired_at = account.get("installer_paired_at")
    if pairing is None and not paired_at:
        steps.append(_step("token", "Pairing token", "pending", "no token generated yet",
                           "Generate a pairing token below and paste the one-liner in PowerShell on the MT5 host"))
    elif pairing and not pairing.get("consumed_at"):
        exp = _age_s(now, pairing.get("expires_at"))
        if exp is not None and exp > 0 and (trusted_only or account.get("verified_identity")):
            steps.append(_step("token", "Pairing token", "pending", f"last token expired unused ({_fmt_age(exp)}) — terminal was trusted without the installer",
                               "Optional: generate a token and run the installer for the strongest (device-key) pairing"))
        elif exp is not None and exp > 0:
            steps.append(_step("token", "Pairing token", "warn", f"token expired unused ({_fmt_age(exp)})",
                               "Generate a new token — each token is single-use and short-lived"))
        else:
            steps.append(_step("token", "Pairing token", "waiting", f"token issued {_fmt_age(_age_s(now, pairing.get('issued_at')))} — not redeemed yet",
                               "Run the installer on the VPS: it picks the terminal first, then redeems the token"))
    else:
        host = (pairing or {}).get("consumed_by_hostname") or account.get("installer_paired_hostname") or "unknown host"
        when = (pairing or {}).get("consumed_at") or paired_at
        steps.append(_step("token", "Pairing token", "done", f"redeemed by {host} {_fmt_age(_age_s(now, when))}"))

    # 2 · installer run
    inst_ver = account.get("installer_version") or (pairing or {}).get("installer_version")
    if not paired_at and not installation:
        steps.append(_step("install", "Installer run on the VPS", "pending", "no installer pairing recorded"))
    elif trusted_only:
        steps.append(_step("install", "Installer run on the VPS", "done",
                           f"terminal trusted without the installer · installation {installation.get('installation_id')} on {installation.get('host_fingerprint') or '?'}"))
    else:
        detail = f"installer v{inst_ver or '?'}"
        if installation:
            detail += f" · installation {installation.get('installation_id')} on {installation.get('host_fingerprint') or '?'}"
        if _version_lt(inst_ver, MIN_INSTALLER_VERSION):
            steps.append(_step("install", "Installer run on the VPS", "warn", detail + " — outdated installer",
                               f"Re-run with installer v{MIN_INSTALLER_VERSION}+ (older versions failed the EA download; the server serves the current one)"))
        else:
            steps.append(_step("install", "Installer run on the VPS", "done", detail))

    # 3 · EX5 binary
    method, measured = attested
    acc_method = account.get("ea_binary_sha256_method")
    if not measured:
        steps.append(_step("binary", "EA binary (.ex5) measured", "pending" if not paired_at else "waiting",
                           "no .ex5 measured on this installation",
                           "If the installer printed 'no .ex5 present': open MetaEditor, press F7 on EmergentTradingBridge.mq5, then re-run the installer"))
    elif acc_method == "installer_mismatch":
        steps.append(_step("binary", "EA binary (.ex5) measured", "blocked",
                           "the EA reports a different .ex5 than the installer measured",
                           "Re-run the installer on this terminal (the running EA is not the deployed binary)"))
    elif accepted_hashes and measured in accepted_hashes:
        steps.append(_step("binary", "EA binary (.ex5) measured", "done",
                           f"{measured[:12]}… matches the signed release" + ("" if method == "installer_attested" else f" ({method})")))
    else:
        steps.append(_step("binary", "EA binary (.ex5) measured", "warn",
                           f"{measured[:12]}… is not the signed release EX5" + ("" if accepted_hashes else " (no signed release published yet)"),
                           "Demo trading is fine; LIVE stays blocked until the CI-signed .ex5 is installed"))

    # 4 · heartbeat ⇒ WebRequest allow-list + EA attached + AutoTrading
    hb_age = _age_s(now, account.get("last_heartbeat"))
    hb_after_pairing = hb_age is not None and (not paired_at or (_iso(account.get("last_heartbeat")) or now) >= (_iso(paired_at) or now))
    ea_ver = (account.get("ea_identity") or {}).get("ea_version") or account.get("ea_version")
    trade_mode = account.get("account_trade_mode")
    tail = f" · EA {ea_ver}" if ea_ver else ""
    tail += f" · broker says {trade_mode}" if trade_mode else ""
    if hb_age is not None and hb_age <= HEARTBEAT_FRESH_S and hb_after_pairing:
        steps.append(_step("heartbeat", "EA heartbeat (WebRequest allowed · EA attached)", "done",
                           f"heartbeating, last {_fmt_age(hb_age)}{tail}"))
    elif paired_at and not hb_after_pairing:
        since = _age_s(now, paired_at) or 0
        if since <= FIRST_HEARTBEAT_GRACE_S:
            steps.append(_step("heartbeat", "EA heartbeat (WebRequest allowed · EA attached)", "waiting",
                               f"paired {_fmt_age(since)} — waiting for the first heartbeat",
                               f"In MT5: Tools → Options → Expert Advisors → allow WebRequest for {url or 'the STOIC server URL'}; attach EmergentTradingBridge to a chart; AutoTrading ON"))
        else:
            steps.append(_step("heartbeat", "EA heartbeat (WebRequest allowed · EA attached)", "blocked",
                               f"no heartbeat since pairing ({_fmt_age(since)})",
                               f"Most likely the WebRequest URL is missing: Tools → Options → Expert Advisors → tick 'Allow WebRequest for listed URL' and add {url or 'the STOIC server URL'}. "
                               "Then attach EmergentTradingBridge (Navigator → Experts) with ServerUrl set to this server, AutoTrading ON, and check the Experts tab for errors"))
    elif hb_age is not None:
        steps.append(_step("heartbeat", "EA heartbeat (WebRequest allowed · EA attached)", "warn",
                           f"last heartbeat {_fmt_age(hb_age)}{tail} — terminal closed or VPS offline",
                           "Start MT5 on the VPS and confirm the EA is still on a chart with AutoTrading ON"))
    else:
        steps.append(_step("heartbeat", "EA heartbeat (WebRequest allowed · EA attached)", "pending", "no heartbeat yet"))

    # 5 · verified terminal identity
    ident = account.get("ea_identity") or {}
    if account.get("verified_identity") and (ident.get("authoritative") or not ident):
        steps.append(_step("identity", "Terminal identity verified", "done",
                           f"installation {(account.get('verified_identity') or {}).get('installation_id') or ident.get('installation_id') or ''} bound to this account".strip()))
    elif ident and ident.get("reason"):
        steps.append(_step("identity", "Terminal identity verified", "blocked", str(ident.get("reason")),
                           "Re-pair this terminal with a fresh token (Quick Install) or trust it from Trusted Terminals"))
    elif account.get("broker_account_mismatch"):
        steps.append(_step("identity", "Terminal identity verified", "blocked",
                           "the EA is logged into a different broker account than this row",
                           "Log the terminal into the account configured here, or fix the account number on this row"))
    else:
        steps.append(_step("identity", "Terminal identity verified", "pending" if hb_age is None else "waiting",
                           "waiting for an authoritative heartbeat"))

    statuses = [s["status"] for s in steps]
    if "blocked" in statuses:
        state, headline = "blocked", next(s["title"] for s in steps if s["status"] == "blocked")
    elif all(s == "done" for s in statuses):
        state, headline = "ready", "VPS terminal paired, heartbeating and verified"
    elif "warn" in statuses:
        state, headline = "attention", next(s["title"] for s in steps if s["status"] == "warn")
    elif any(s == "waiting" for s in statuses):
        state, headline = "in_progress", next(s["title"] for s in steps if s["status"] == "waiting")
    else:
        state, headline = "not_started", "Generate a pairing token to begin"
    return {"account_id": str(account.get("_id") or account.get("id") or ""), "state": state, "headline": headline,
            "steps": steps, "webrequest_url": url, "generated_at": now.isoformat(),
            "done": sum(1 for s in statuses if s == "done"), "total": len(steps)}
