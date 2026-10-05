"""Security & Health Agent — settings (env + platform_state), safe defaults. SA1."""
import os

MODES = ("observe", "enforce")
STATE_ID = "security_agent"
SEVERITIES = ("low", "medium", "high", "critical")
AREAS = ("access", "bridge", "secrets", "dependencies", "integrity", "platform", "trading")

DEFAULTS = {
    "mode": "observe",
    "rules_enabled": [],
    "protected_ips": [],
    "max_actions_per_hour": 10,
    "ip_block_min": 60,
    "account_lock_min": 30,
    "critical_repeat_min": 30,
    "daily_report_utc": "07:00",
    "retention_days": 180,
    "ai_summary": False,
    "alert_emails": ["admin@stoicaibot.com"],
    # check thresholds (section 3) — tunable without code changes
    "thresholds": {
        "A1": {"ip_fail_5m": 20, "account_fail_10m": 50},
        "A2": {"otp_fail_10m": 10},
        "A3": {"accounts_per_ip_10m": 5},
        "A5": {"window_min": 5},
        "A7": {"spike_ratio": 3.0, "min_count": 50},
        "B1": {"distinct_sources_60s": 2, "repeats": 3},
        "B2": {"invalid_token_5m": 30},
        "S3": {"warn_days": 14, "critical_days": 3},
        "I4": {"max_backup_age_h": 26},
        "P4": {"disk_pct": 85, "disk_critical_pct": 95, "ram_pct": 90},
        "P5": {"repeats_15m": 20},
        "T1": {"stale_accounts_min": 3, "stale_fraction": 0.5},
        "T2": {"unacked_s": 120},
    },
}

_ENV = {
    "mode": ("SECURITY_AGENT_MODE", str), "rules_enabled": ("SECURITY_AGENT_RULES", "list"),
    "protected_ips": ("SECURITY_AGENT_PROTECTED_IPS", "list"),
    "max_actions_per_hour": ("SECURITY_AGENT_MAX_ACTIONS_PER_HOUR", int),
    "ip_block_min": ("SECURITY_AGENT_IP_BLOCK_MIN", int), "account_lock_min": ("SECURITY_AGENT_ACCOUNT_LOCK_MIN", int),
    "critical_repeat_min": ("SECURITY_AGENT_CRITICAL_REPEAT_MIN", int),
    "daily_report_utc": ("SECURITY_AGENT_DAILY_REPORT_UTC", str), "retention_days": ("SECURITY_AGENT_RETENTION_DAYS", int),
    "ai_summary": ("SECURITY_AGENT_AI_SUMMARY", "bool"), "alert_emails": ("SECURITY_AGENT_ALERT_EMAILS", "list"),
}


def from_env(env: dict | None = None) -> dict:
    env = os.environ if env is None else env
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for key, (var, typ) in _ENV.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue
        if typ == "list":
            cfg[key] = [x.strip() for x in raw.split(",") if x.strip()]
        elif typ == "bool":
            cfg[key] = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            cfg[key] = typ(raw)
    if cfg["mode"] not in MODES:
        cfg["mode"] = "observe"
    return cfg


def merge_state(cfg: dict, state: dict | None) -> dict:
    """platform_state overrides env (admin panel settings), thresholds merged per check."""
    out = dict(cfg)
    for k, v in (state or {}).items():
        if k == "_id":
            continue
        if k == "thresholds" and isinstance(v, dict):
            th = {ck: dict(cv) for ck, cv in out["thresholds"].items()}
            for ck, cv in v.items():
                th.setdefault(ck, {}).update(cv or {})
            out["thresholds"] = th
        elif k in DEFAULTS:
            out[k] = v
    if out.get("mode") not in MODES:
        out["mode"] = "observe"
    return out


async def load(db) -> dict:
    state = await db.platform_state.find_one({"_id": STATE_ID})
    cfg = merge_state(from_env(), state)
    # Audit #5 SEC-001: user-id-keyed rules (R4/R7) must recognise the master admin too.
    admin = await db.users.find_one({"email": (os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com").lower()}, {"_id": 1})
    cfg["admin_user_id"] = str(admin["_id"]) if admin else None
    return cfg
