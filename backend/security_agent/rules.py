"""SA3 — containment rules R1–R8: map open findings to proposed actions with proof thresholds.
actions.py executes them (observe mode = would_have_done). The protected list is evaluated here so
the observe log already shows what enforce mode would refuse."""
import ipaddress
import os

CLOUDFLARE_IPV4 = ("173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
                   "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
                   "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22")
CLOUDFLARE_IPV6 = ("2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
                   "2a06:98c0::/29", "2c0f:f248::/32")
# S1 — loopback, RFC1918, CGNAT, link-local and docker/ULA ranges can never be blocked:
# behind an extra proxy every user shares one of these, and R1 would lock everyone out.
PRIVATE_RANGES = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                  "100.64.0.0/10", "169.254.0.0/16", "fc00::/7", "fe80::/10")
ALWAYS_PROTECTED_IPS = PRIVATE_RANGES + CLOUDFLARE_IPV4 + CLOUDFLARE_IPV6
# S1 — an IP that carries this share of the recent auth/bridge traffic is a proxy, not an attacker
SHARED_IP_MIN_SHARE = 0.5
SHARED_IP_MIN_EVENTS = 20


def is_cloudflare(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return any(a in ipaddress.ip_network(n) for n in CLOUDFLARE_IPV4 + CLOUDFLARE_IPV6)

RULES = {
    "R1": {"title": "Brute force → block IP on auth routes", "checks": ("A1",), "action": "block_ip", "scope": "auth", "undo": "unblock_ip"},
    "R2": {"title": "Account under attack → lock password login", "checks": ("A1",), "action": "lock_login", "scope": "account", "undo": "unlock_account"},
    "R3": {"title": "2FA/OTP guessing → lock OTP verify", "checks": ("A2",), "action": "lock_otp", "scope": "account", "undo": "unlock_otp"},
    "R4": {"title": "Stolen session → revoke session family", "checks": ("A4",), "action": "revoke_sessions", "scope": "user", "undo": None},
    "R5": {"title": "Stolen bridge token → suspend token", "checks": ("B1",), "action": "suspend_bridge_token", "scope": "account", "undo": "reinstate_token"},
    "R6": {"title": "Bridge token guessing → block IP on /bridge/*", "checks": ("B2",), "action": "block_ip", "scope": "bridge", "undo": "unblock_ip"},
    "R7": {"title": "Unauthorised command → freeze new entries + revoke actor sessions", "checks": ("A6", "B3"), "action": "freeze_new_entries", "scope": "account", "undo": "unfreeze"},
    "R8": {"title": "Scanning / flooding → block IP", "checks": ("A7",), "action": "block_ip", "scope": "all", "undo": "unblock_ip"},
    # P1-04 — credential stuffing (A3: one IP, many accounts) targets the IP on auth routes; it was
    # wrongly proposed as R2 (lock_login, account scope) with an IP target.
    "R9": {"title": "Credential stuffing → block IP on auth routes", "checks": ("A3",), "action": "block_ip", "scope": "auth", "undo": "unblock_ip"},
}
# P1-04 — fixed (action → target kind) mapping; `evaluate` asserts every proposal against it so a
# rule can never be emitted with a target type its action cannot act on.
ACTION_TARGET_KIND = {"block_ip": "ip", "lock_login": "account", "lock_otp": "account", "revoke_sessions": "user",
                      "suspend_bridge_token": "account", "freeze_new_entries": ("account", "user")}


def target_kind_valid(action: str, kind: str) -> bool:
    allowed = ACTION_TARGET_KIND.get(action)
    return kind == allowed if isinstance(allowed, str) else kind in (allowed or ())
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
        v = str(value or "").lower()
        return v == admin or (bool(cfg.get("admin_user_id")) and v == str(cfg["admin_user_id"]).lower())
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
        p("R9", "ip", ev.get("ip"), ipm, note="credential stuffing source blocked on auth routes")
    if cid == "A2" and int(ev.get("failures") or 0) >= th.get("A2", {}).get("otp_fail_10m", 10):
        p("R3", "account", ev.get("target"), accm)
    if cid == "A4":
        p("R4", "user", (ev.get("event") or {}).get("user_id"), 0, note="family already revoked by security.revoke_family; force MFA re-login")
    if cid == "B1" and len(ev.get("sources") or []) >= th.get("B1", {}).get("distinct_sources_60s", 2):
        p("R5", "account", ev.get("account_id"), int(cfg.get("token_suspend_min") or 240), note="expires; re-pair lifts it earlier")
    if cid == "B2" and int(ev.get("requests") or 0) >= th.get("B2", {}).get("invalid_token_5m", 100):
        p("R6", "ip", ev.get("ip"), ipm)
    if cid == "A6" and (ev.get("event") or {}).get("reason") in (None, "step_up_forged", "step_up_missing"):
        # S4 — only forged / absent tokens (expired or double-clicked ones never reach a finding)
        p("R7", "user", (ev.get("event") or {}).get("user_id"), 0, note="until admin clears",
          revoke_sessions=True)
    if cid == "B3":
        p("R7", "account", (ev.get("event") or {}).get("account_id"), 0, note="until admin clears")
    if cid == "A7" and int(ev.get("top_ip_rpm") or 0) >= 300:
        p("R8", "ip", ev.get("top_ip"), ipm)
    for prop in out:
        assert prop["action"] not in FORBIDDEN_ACTIONS
        assert target_kind_valid(prop["action"], prop["target_kind"]), (prop["rule"], prop["action"], prop["target_kind"])
        if is_protected(cfg, prop["target_kind"], prop["target"]):
            prop.update(blocked_by="protected_target", severity_escalation="critical")
    return out
