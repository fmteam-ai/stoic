"""SA3 — containment rules R1–R8: map open findings to proposed actions with proof thresholds.
In observe mode (SA3) every proposal is recorded in security_actions as `would_have_done`; SA4 adds
actions.py that executes them. The protected list and the hourly cap are evaluated here too, so the
observe log already shows what enforce mode would refuse."""
import ipaddress
import os
from datetime import datetime, timedelta, timezone

from security_agent.checks.common import f
from security_agent.findings import open_or_update

CLOUDFLARE_IPV4 = ("173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
                   "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
                   "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22")
ALWAYS_PROTECTED_IPS = ("127.0.0.1", "::1") + CLOUDFLARE_IPV4

RULES = {
    "R1": {"title": "Brute force → block IP on auth routes", "checks": ("A1",), "action": "block_ip", "scope": "auth", "undo": "unblock_ip"},
    "R2": {"title": "Account under attack → lock password login", "checks": ("A1", "A3"), "action": "lock_login", "scope": "account", "undo": "unlock_account"},
    "R3": {"title": "2FA/OTP guessing → lock OTP verify", "checks": ("A2",), "action": "lock_otp", "scope": "account", "undo": "unlock_otp"},
    "R4": {"title": "Stolen session → revoke session family", "checks": ("A4",), "action": "revoke_sessions", "scope": "user", "undo": None},
    "R5": {"title": "Stolen bridge token → suspend token", "checks": ("B1",), "action": "suspend_bridge_token", "scope": "account", "undo": "reinstate_token"},
    "R6": {"title": "Bridge token guessing → block IP on /bridge/*", "checks": ("B2",), "action": "block_ip", "scope": "bridge", "undo": "unblock_ip"},
    "R7": {"title": "Unauthorised command → freeze new entries + revoke actor sessions", "checks": ("A6", "B3"), "action": "freeze_new_entries", "scope": "account", "undo": "unfreeze"},
    "R8": {"title": "Scanning / flooding → block IP", "checks": ("A7",), "action": "block_ip", "scope": "all", "undo": "unblock_ip"},
}
FORBIDDEN_ACTIONS = ("close_trade", "modify_trade", "open_trade", "set_sl_tp", "stop_protection_loop", "stop_trade_manager")


def protected_networks(cfg: dict) -> list:
    nets = []
    for raw in list(ALWAYS_PROTECTED_IPS) + list(cfg.get("protected_ips") or []):
        try:
            nets.append(ipaddress.ip_network(raw.strip(), strict=False))
        except ValueError:
            continue
    return nets


def is_protected(cfg: dict, kind: str, value: str) -> bool:
    """Master admin account and allow-listed IPs/CIDRs can never be blocked or locked."""
    if kind in ("account", "user"):
        admin = (os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com").lower()
        return str(value or "").lower() == admin
    if kind == "ip":
        try:
            ip = ipaddress.ip_address(str(value))
        except ValueError:
            return False
        return any(ip in n for n in protected_networks(cfg))
    return False


def evaluate(finding: dict, cfg: dict) -> list[dict]:
    """Pure: proposals for one finding. Thresholds are the rule's proof thresholds (section 4)."""
    ev, cid, key = finding.get("evidence") or {}, finding.get("check_id"), str(finding.get("dedup_key") or "")
    ipm, accm = int(cfg.get("ip_block_min") or 60), int(cfg.get("account_lock_min") or 30)
    th = cfg.get("thresholds") or {}
    out = []

    def p(rule, kind, value, minutes, **extra):
        if value in (None, ""):
            return
        out.append({"rule": rule, "action": RULES[rule]["action"], "scope": RULES[rule]["scope"], "target_kind": kind,
                    "target": str(value), "expires_min": minutes, "undo": RULES[rule]["undo"], **extra})

    if cid == "A1" and key.startswith("A1:ip:") and int(ev.get("failures") or 0) >= th.get("A1", {}).get("ip_fail_5m", 20):
        p("R1", "ip", ev.get("ip"), ipm)
    if cid == "A1" and key.startswith("A1:account:") and int(ev.get("failures") or 0) >= th.get("A1", {}).get("account_fail_10m", 50) and len(ev.get("ips") or []) >= 3:
        p("R2", "account", ev.get("account"), accm, notify_owner=True)
    if cid == "A3" and int(ev.get("accounts") or 0) >= th.get("A3", {}).get("accounts_per_ip_10m", 5):
        p("R2", "ip", ev.get("ip"), ipm, note="credential stuffing source blocked on auth routes")
    if cid == "A2" and int(ev.get("failures") or 0) >= th.get("A2", {}).get("otp_fail_10m", 10):
        p("R3", "account", ev.get("target"), accm)
    if cid == "A4":
        p("R4", "user", (ev.get("event") or {}).get("user_id"), 0, note="family already revoked by security.revoke_family; force MFA re-login")
    if cid == "B1" and len(ev.get("sources") or []) >= th.get("B1", {}).get("distinct_sources_60s", 2):
        p("R5", "account", ev.get("account_id"), 0, note="until re-paired")
    if cid == "B2" and int(ev.get("requests") or 0) >= th.get("B2", {}).get("invalid_token_5m", 30):
        p("R6", "ip", ev.get("ip"), ipm)
    if cid == "A6":
        p("R7", "user", (ev.get("event") or {}).get("user_id"), 0, note="until admin clears")
    if cid == "B3":
        p("R7", "account", (ev.get("event") or {}).get("account_id"), 0, note="until admin clears")
    if cid == "A7" and int(ev.get("top_ip_rpm") or 0) >= 300:
        p("R8", "ip", ev.get("top_ip"), ipm)
    for prop in out:
        assert prop["action"] not in FORBIDDEN_ACTIONS
        if is_protected(cfg, prop["target_kind"], prop["target"]):
            prop.update(blocked_by="protected_target", severity_escalation="critical")
    return out


async def sweep(db, cfg: dict) -> int:
    """Observe mode: record proposals as would_have_done (one row per finding+rule+target per hour)."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=1)).isoformat()
    cap = int(cfg.get("max_actions_per_hour") or 10)
    recorded = 0
    rows = await db.security_findings.find({"status": {"$in": ["open", "contained"]}, "check_id": {"$in": sorted({c for r in RULES.values() for c in r["checks"]})}}).limit(500).to_list(length=500)
    for fd in rows:
        for prop in evaluate(fd, cfg):
            dedup = f"{fd['_id']}|{prop['rule']}|{prop['target']}"
            if await db.security_actions.find_one({"dedup": dedup, "at": {"$gte": since}}):
                continue
            n_hour = await db.security_actions.count_documents({"kind": "containment", "at": {"$gte": since}})
            status = "would_have_done" if cfg.get("mode") == "observe" else "pending"
            if prop.get("blocked_by"):
                status = "refused_protected"
                await open_or_update(db, f("protected_target", f"{prop['rule']}:{prop['target']}", "critical", "platform",
                                           f"{prop['rule']} would have applied {prop['action']} to protected {prop['target_kind']} {prop['target']} — refused",
                                           {"rule": prop["rule"], "target": prop["target"], "finding": fd["dedup_key"]}))
            elif n_hour >= cap:
                status = "refused_cap"
                await open_or_update(db, f("containment_cap_reached", "platform", "critical", "platform",
                                           f"{n_hour} containment actions in the last hour reached the cap of {cap}; agent is alert-only",
                                           {"actions_last_hour": n_hour, "cap": cap}))
            await db.security_actions.insert_one({"kind": "containment", "dedup": dedup, "finding_id": str(fd["_id"]), "dedup_key": fd["dedup_key"],
                                                  "check_id": fd["check_id"], "status": status, "mode": cfg.get("mode"), "at": now.isoformat(),
                                                  "actor": "security_agent", "expires_at": (now + timedelta(minutes=prop["expires_min"])).isoformat() if prop["expires_min"] else None,
                                                  **prop})
            recorded += 1
    return recorded
