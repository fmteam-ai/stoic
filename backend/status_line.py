"""U15 (Phase 4) — ONE status line per account: Connecting → Verified → Ready → Trading (or Blocked), with
exactly one fix action. Everything the badges used to show lives behind "Why?" (`internals`).

States
  connecting  no accepted heartbeat yet (or heartbeats rejected) → fix: install / repair
  verified    heartbeat + verified terminal chain, but not yet admitted/attested → fix: the one missing thing
  ready       admitted (rules policy) or approved inventory + attested environment, bot not started → fix: Start
  trading     bot active and everything above holds
  blocked     a hard stop (broker reports real money on a demo policy, policy expired, waiting for the operator)"""
from __future__ import annotations

from datetime import datetime, timezone

from install_progress import BRIDGE_REJECTED_FRESH_S, HEARTBEAT_FRESH_S, _age_s


def _fix(action: str, label: str, **extra) -> dict:
    return {"action": action, "label": label, **extra}


def derive(acc: dict, *, bot_active: bool, rules_active: bool, now: datetime | None = None) -> dict:
    from broker_env import attestation_state, attested_environment, broker_reports_real
    now = now or datetime.now(timezone.utc)
    hb_age = _age_s(now, acc.get("last_heartbeat"))
    rej = acc.get("bridge_rejected") or {}
    rej_age = _age_s(now, rej.get("at"))
    ident = acc.get("ea_identity") or {}
    adm = acc.get("admission") or {}
    env = attested_environment(acc)
    internals = {"heartbeat_age_s": hb_age, "identity_authoritative": bool(ident.get("authoritative")),
                 "identity_reason": ident.get("reason"), "environment_effective": env, "attestation_state": attestation_state(acc),
                 "admission": adm.get("state"), "admission_reason": adm.get("reason"), "server": acc.get("server"),
                 "position_mode": acc.get("margin_mode"), "ea_version": ident.get("ea_version") or acc.get("ea_version"),
                 "bot_active": bot_active, "rules_policy": rules_active}
    if acc.get("mode") == "paper":
        st = "trading" if bot_active else "ready"
        return {"state": st, "label": "Paper — " + ("trading" if bot_active else "ready"), "fix": None if bot_active else _fix("start", "Start"), "internals": internals}
    # 1 · Connecting
    if rej_age is not None and rej_age <= BRIDGE_REJECTED_FRESH_S and (hb_age is None or rej_age < hb_age):
        return {"state": "connecting", "label": "Connecting — the EA is reachable but its token is rejected",
                "fix": _fix("repair", "Repair (one line)", why="old token file on the terminal"), "internals": internals}
    if hb_age is None:
        return {"state": "connecting", "label": "Connecting — waiting for the first heartbeat",
                "fix": _fix("install", "Connect (run the installer)"), "internals": internals}
    if hb_age > HEARTBEAT_FRESH_S:
        return {"state": "connecting", "label": f"Connecting — last heartbeat {int(hb_age // 60)} min ago",
                "fix": _fix("repair", "Repair (one line)", why="EA not running or MT5 closed"), "internals": internals}
    sc = acc.get("ea_self_check") or {}
    if sc.get("webrequest_ok") is False:
        return {"state": "connecting", "label": "Connecting — MT5 blocks the server address",
                "fix": _fix("webrequest", "Allow the URL in MT5 (guided)"), "internals": internals}
    # 2 · Verified
    if not ident.get("authoritative"):
        reason = str(ident.get("reason") or "terminal not verified yet")
        if "broker server mismatch" in reason:
            return {"state": "connecting", "label": "Connecting — server name differs (adopting from the EA)",
                    "fix": _fix("adopt_server", "Use the server the EA reports"), "internals": internals}
        return {"state": "connecting", "label": "Connecting — " + reason,
                "fix": _fix("trust", "Trust this terminal"), "internals": internals}
    if broker_reports_real(acc) and env != "LIVE":
        return {"state": "blocked", "label": "Blocked — the broker reports a real-money account",
                "fix": _fix("operator", "Ask the operator to approve real money"), "internals": internals}
    if env == "LIVE" and attestation_state(acc) != "valid" and adm.get("state") != "admitted":
        if adm.get("state") == "pending_operator":
            return {"state": "blocked", "label": "Verified — waiting for operator approval (real money)",
                    "fix": _fix("operator", "Operator approval needed"), "internals": internals}
        return {"state": "verified", "label": "Verified — demo status not confirmed yet (next heartbeat)",
                "fix": _fix("wait", "Waiting for the broker's demo confirmation"), "internals": internals}
    if rules_active and adm.get("state") == "blocked":
        return {"state": "blocked", "label": "Blocked — " + str(adm.get("reason") or "outside the policy rules"),
                "fix": _fix("operator", "Ask the operator"), "internals": internals}
    if rules_active and adm.get("state") == "pending_operator":
        return {"state": "blocked", "label": "Verified — waiting for operator approval (real money)",
                "fix": _fix("operator", "Operator approval needed"), "internals": internals}
    if rules_active and adm.get("state") != "admitted":
        return {"state": "verified", "label": "Verified — admission pending (next heartbeat)",
                "fix": _fix("wait", "Checking the policy rules"), "internals": internals}
    # 3/4 · Ready / Trading
    if bot_active:
        return {"state": "trading", "label": f"Trading — {env} · verified terminal", "fix": None, "internals": internals}
    return {"state": "ready", "label": f"Ready — {env} · verified terminal", "fix": _fix("start", "Start"), "internals": internals}
