"""Demo-readiness checklist (main94 deploy checklist, p.8) — one page for the operator.
Auto checks read the running environment + database; manual steps are ticked by an admin
and persisted in db.demo_readiness_checks. Status vocabulary: pass | warn | fail | info."""
import logging
import os
from datetime import datetime, timedelta, timezone

from bson import ObjectId

logger = logging.getLogger("demo_readiness")

HEARTBEAT_FRESH_SEC = 120
FLEET_WINDOW_H = 24
FLEET_LIMIT = 50
DEMO_ACCEPTED_EA = ("1.57", "1.58")   # D-1 — accepted on attested DEMO accounts until a signed 1.59 ships

MANUAL_STEPS = [
    ("telegram_revoked", "Leaked security-bot token revoked in @BotFather; new token created",
     "The old token appeared in a commit message — it must be dead before the demo."),
    ("backup_noted", "`deploy/backup.sh backup` run and the archive name noted",
     "update.sh takes one too, but keep a known-good archive you can name."),
    ("ci_green", "CI green on the commit you deploy (unit · integration · ea-structural · frontend)",
     "No red gates: the deploy would be refused or rolled back."),
    ("deployed_with_hold", "Deployed with UPDATE_HOLD_ON_FAILURE=1 STOIC_READINESS_POLICY=onboarding-close-only ./deploy/update.sh",
     "Backend log shows “P1-01 — migrated N account(s)”."),
    ("relogin_done", "Logged in again after the deploy (changed the password if asked)",
     "Every access token was revoked by the release."),
    ("bot_pulse_full", "Bot Pulse shows FULL authority and the right engine on each demo account",
     "Dashboard → Bot Pulse; anything else means a gate is still closed."),
    ("demo_plan_read", "Demo Test Plan read: daily brake check, re-attach EA after any installer run, watch Demo 3 for whole-position closes",
     "During the test: never redeploy mid-test."),
]


def _now():
    return datetime.now(timezone.utc)


def _check(cid, title, status, detail="", hint=""):
    return {"id": cid, "title": title, "status": status, "detail": detail, "hint": hint}


def env_checks(env=None) -> list[dict]:
    env = env if env is not None else os.environ
    out = []
    tok = (env.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN") or "").strip()
    if env.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN_FILE") and tok:
        out.append(_check("telegram_secret", "Security-bot token from secrets/security_telegram_token", "pass",
                          "token loaded from the secret file"))
    elif tok:
        out.append(_check("telegram_secret", "Security-bot token from secrets/security_telegram_token", "warn",
                          "token is set directly in backend/.env",
                          "put the NEW token in secrets/security_telegram_token and remove SECURITY_AGENT_TELEGRAM_* from backend/.env"))
    else:
        out.append(_check("telegram_secret", "Security-bot token from secrets/security_telegram_token", "fail",
                          "no token configured — SA3 alerts are off"))
    chat = (env.get("SECURITY_AGENT_TELEGRAM_CHAT_ID") or "").strip()
    out.append(_check("telegram_chat", "Security-bot chat ID set", "pass" if chat else "fail",
                      "configured" if chat else "SECURITY_AGENT_TELEGRAM_CHAT_ID missing"))
    key = (env.get("BRIDGE_TOKEN_HASH_KEY") or "").strip()
    if len(key) >= 32:
        out.append(_check("bridge_hash_key", "BRIDGE_TOKEN_HASH_KEY set (32+ chars, decided once)", "pass", f"{len(key)} chars"))
    elif key:
        out.append(_check("bridge_hash_key", "BRIDGE_TOKEN_HASH_KEY set (32+ chars, decided once)", "warn",
                          f"only {len(key)} chars", "use 32+ random characters"))
    else:
        out.append(_check("bridge_hash_key", "BRIDGE_TOKEN_HASH_KEY set (32+ chars, decided once)", "warn",
                          "unset — EA tokens are hashed with JWT_SECRET",
                          "set it BEFORE the deploy or never rotate JWT_SECRET during the test (R-4)"))
    out.append(_check("resend", "RESEND_API_KEY present (activation / reset mails)",
                      "pass" if (env.get("RESEND_API_KEY") or "").strip() else "fail",
                      "configured" if (env.get("RESEND_API_KEY") or "").strip() else "missing — production refuses to boot"))
    cf = (env.get("TRUST_CF_CONNECTING_IP") or "").strip().lower() == "true"
    cidrs = (env.get("TRUSTED_PROXY_CIDRS") or "").strip()
    out.append(_check("client_ip", "Client-IP chain configured (TRUSTED_PROXY_CIDRS · TRUST_CF_CONNECTING_IP)", "info",
                      f"proxy ranges: {'set' if cidrs else 'unset'} · Cloudflare header: {'trusted' if cf else 'off'}",
                      "set TRUST_CF_CONNECTING_IP=true ONLY if the domain is proxied by Cloudflare (R-8)"))
    hold = (env.get("UPDATE_HOLD_ON_FAILURE") or "").strip()
    out.append(_check("hold_on_failure", "UPDATE_HOLD_ON_FAILURE=1 for this deploy", "pass" if hold == "1" else "info",
                      "set" if hold == "1" else "not visible to the backend — tick the manual step when you run update.sh"))
    return out


async def db_checks(db) -> list[dict]:
    out = []
    plain = await db.accounts.count_documents({"bridge_token": {"$type": "string"}})
    out.append(_check("token_migration", "Bridge tokens stored as hashes only (P1-01 migration complete)",
                      "pass" if plain == 0 else "fail",
                      "no plaintext tokens" if plain == 0 else f"{plain} account(s) still carry a plaintext token"))
    from auth import master_admin_email
    admin = await db.users.find_one({"email": master_admin_email()}, {"two_factor_enabled": 1, "must_change_password": 1})
    if not admin:
        out.append(_check("admin_mfa", "Master admin has TOTP 2FA and no pending password change", "fail", "admin user not found"))
    else:
        ok = bool(admin.get("two_factor_enabled")) and not admin.get("must_change_password")
        out.append(_check("admin_mfa", "Master admin has TOTP 2FA and no pending password change", "pass" if ok else "fail",
                          ("2FA on" if admin.get("two_factor_enabled") else "2FA OFF")
                          + (" · password change pending" if admin.get("must_change_password") else "")))
    from execution_health import braked_accounts
    braked = await braked_accounts(db)
    out.append(_check("brakes", "No active execution brake", "pass" if not braked else "fail",
                      "none" if not braked else ", ".join((b.get("label") or b["id"]) for b in braked[:5]),
                      "Dashboard banner → ADMIN RESUME after checking the EA / VPS"))
    try:
        from ea_capabilities import accepted_ea_sha256s
        hashes = accepted_ea_sha256s()
    except Exception as e:  # noqa: BLE001
        hashes = []
        logger.warning("ea hashes unavailable: %s", type(e).__name__)
    out.append(_check("ea_release", "Signed EA release recorded (or EA_RELEASE_SHA256 pinned)", "pass" if hashes else "warn",
                      f"{len(hashes)} accepted EX5 hash(es)" if hashes else "none — LIVE accounts stay close-only; attested DEMO accounts are unaffected",
                      "run the ea-release workflow or pin the 1.57 hash"))
    return out


async def _cap_config(db, acc: dict):
    cfg = await db.bot_configs.find_one({"user_id": acc.get("user_id"), "account_id": str(acc["_id"])})
    if not cfg:
        cfg = await db.bot_configs.find_one({"user_id": acc.get("user_id"), "account_id": None})
    return cfg


async def fleet(db) -> list[dict]:
    """Accounts that matter for the demo: enabled or heartbeating in the last 24 h."""
    since = (_now() - timedelta(hours=FLEET_WINDOW_H)).isoformat()
    rows = await db.accounts.find({"mode": {"$ne": "paper"},
                                   "$or": [{"trading_enabled": True}, {"last_heartbeat": {"$gte": since}}]},
                                  {"label": 1, "user_id": 1, "last_heartbeat": 1, "ea_version": 1, "mode": 1,
                                   "environment_attestation": 1, "account_type": 1, "broker_environment": 1,
                                   "position_mode_override": 1, "trading_enabled": 1, "execution_brake": 1,
                                   "broker_server": 1, "server": 1, "verified_identity": 1, "expected_identity": 1,
                                   "account_number": 1}).to_list(length=FLEET_LIMIT)
    from broker_env import attested_environment
    from routes.bridge_routes import position_mode_resolution
    from routes.diagnostic_routes import LATEST_EA
    out = []
    for a in rows:
        hb = a.get("last_heartbeat")
        fresh = False
        if hb:
            try:
                fresh = (_now() - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))).total_seconds() <= HEARTBEAT_FRESH_SEC
            except ValueError:
                fresh = False
        cfg = await _cap_config(db, a) or {}
        cap = cfg.get("trade_of_day_cap")
        pm = await position_mode_resolution(db, a)
        demo = attested_environment(a) == "DEMO"
        ea_v = str(a.get("ea_version") or "")
        out.append({
            "id": str(a["_id"]), "label": a.get("label") or a.get("account_number"),
            "heartbeat_fresh": fresh, "last_heartbeat": hb,
            # D-1 — until a signed 1.59 exists, attested DEMO terminals legitimately run 1.57/1.58
            "ea_version": a.get("ea_version"), "ea_current": ea_v == LATEST_EA or (demo and ea_v in DEMO_ACCEPTED_EA),
            "attested_demo": demo,
            "position_mode": pm.get("mode"), "position_mode_source": pm.get("source"),
            "position_mode_explicit": pm.get("source") in ("admin", "registry", "ea"),
            "netting": pm.get("mode") == "netting",
            "trade_of_day_cap": cap, "max_concurrent_trades": cfg.get("max_concurrent_trades"),
            "caps_explicit": isinstance(cap, int) and cap > 0 and cfg.get("max_concurrent_trades") is not None,
            "braked": bool((a.get("execution_brake") or {}).get("active")),
        })
    return out


def fleet_checks(rows: list[dict]) -> list[dict]:
    if not rows:
        return [_check("fleet", "Demo accounts present (enabled or heartbeating in 24 h)", "fail",
                       "no live/demo account enabled or heartbeating", "connect the demo terminals first")]
    def agg(cid, title, key, hint):
        bad = [r["label"] for r in rows if not r.get(key)]
        return _check(cid, title, "pass" if not bad else "fail",
                      f"{len(rows) - len(bad)}/{len(rows)} ok" + (f" · missing: {', '.join(map(str, bad[:5]))}" if bad else ""), hint)
    netting = [r["label"] for r in rows if r.get("netting")]
    return [
        agg("fleet_heartbeat", "Every demo EA heartbeat fresh (≤ 2 min)", "heartbeat_fresh", "re-attach the EA / restart MT5 within 15 min after any installer run"),
        agg("fleet_ea", "Every demo account runs the current EA (1.57/1.58 accepted on attested DEMO accounts)", "ea_current", "attach the shipped EA build on each terminal"),
        agg("fleet_attested", "Every demo account admin-attested as DEMO", "attested_demo", "Accounts → admin attestation (needs the EA identity proof)"),
        agg("fleet_position_mode", "Position mode known for every demo account (netting for Demo 3)", "position_mode_explicit", "Accounts → Position Mode (admin) or let the EA report margin_mode"),
        agg("fleet_caps", "trade_of_day_cap and max open trades set explicitly on each config", "caps_explicit", "Bot Config → apply preset, then set both caps per the Demo Test Plan"),
        # D-2 / Q-1 — netting is the main open risk: whole-position closes
        _check("fleet_netting", "Netting accounts (whole-position close risk — Q-1)", "warn" if netting else "pass",
               (f"{len(netting)} netting: {', '.join(map(str, netting[:5]))}" if netting else "none — all hedging"),
               "closes are volume-limited one leg per poll; keep max 1 open trade per symbol on netting demos"),
    ]


async def manual_state(db) -> list[dict]:
    rows = {r["_id"]: r for r in await db.demo_readiness_checks.find({}).to_list(length=100)}
    out = []
    for cid, title, hint in MANUAL_STEPS:
        r = rows.get(cid) or {}
        out.append({"id": cid, "title": title, "hint": hint, "checked": bool(r.get("checked")),
                    "checked_by": r.get("checked_by"), "checked_at": r.get("checked_at")})
    return out


async def set_manual(db, cid: str, checked: bool, actor: str) -> dict:
    if cid not in {m[0] for m in MANUAL_STEPS}:
        raise KeyError(cid)
    now = _now().isoformat()
    await db.demo_readiness_checks.update_one(
        {"_id": cid}, {"$set": {"checked": bool(checked), "checked_by": actor if checked else None,
                                "checked_at": now if checked else None}}, upsert=True)
    try:
        await db.audit_log.insert_one({"user_id": None, "action": "demo_readiness_manual",
                                       "detail": {"step": cid, "checked": bool(checked), "actor": actor},
                                       "step_up_verified": False, "at": now})
    except Exception as e:  # noqa: BLE001 — audit r31 P3: never silent, surface as an ops alert
        logger.error("demo_readiness audit write failed (%s) step=%s actor=%s", type(e).__name__, cid, actor)
        try:
            from alerting import raise_alert
            await raise_alert(db, "audit_write_failed", "critical",
                              f"Audit log write failed for demo-readiness step {cid} by {actor} ({type(e).__name__}).",
                              dedup_key=f"audit_write_failed:demo_readiness:{cid}",
                              meta={"step": cid, "actor": actor, "error": type(e).__name__})
        except Exception as e2:  # noqa: BLE001
            logger.error("ops alert for audit failure also failed: %s", type(e2).__name__)
    return {"id": cid, "checked": bool(checked), "checked_by": actor if checked else None, "checked_at": now if checked else None}


async def build(db) -> dict:
    rows = await fleet(db)
    checks = env_checks() + await db_checks(db) + fleet_checks(rows)
    manual = await manual_state(db)
    gating = [c for c in checks if c["status"] != "info"]
    passed = sum(1 for c in gating if c["status"] == "pass") + sum(1 for m in manual if m["checked"])
    total = len(gating) + len(manual)
    blockers = [c["title"] for c in checks if c["status"] == "fail"] + [m["title"] for m in manual if not m["checked"]]
    return {"generated_at": _now().isoformat(), "checks": checks, "fleet": rows, "manual": manual,
            "score": {"passed": passed, "total": total, "ready": not blockers},
            "blockers": blockers, "warnings": [c["title"] for c in checks if c["status"] == "warn"]}
