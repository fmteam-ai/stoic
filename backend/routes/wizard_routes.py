"""Easy Start Wizard (spec Oct 12 2026) — ONE poll endpoint per account that drives the 4 screens
(Broker → Connect → Activate → Start) and turns every failure into one sentence + one button. Read-only."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from security import rate_limit
from route_utils import parse_object_id

router = APIRouter(prefix="/setup/wizard", tags=["easy-start"])

RISK_CARDS = [
    {"id": "low", "title": "Conservative", "meaning": "risks about 0.5% per trade"},
    {"id": "medium", "title": "Balanced", "meaning": "risks about 1% per trade", "default": True},
    {"id": "high", "title": "Aggressive", "meaning": "risks about 2.5% per trade"},
]
NO_HEARTBEAT_AFTER_S = 180


def _age_s(now, iso):
    try:
        return max(0.0, (now - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds())
    except (TypeError, ValueError):
        return None


def connect_error(acc: dict, progress: dict, now: datetime) -> dict | None:
    """Screen 2 — one sentence + one button, never a code. None = no error."""
    login = acc.get("account_number")
    rej = acc.get("bridge_rejected") or {}
    rej_age = _age_s(now, rej.get("at"))
    hb_age = _age_s(now, acc.get("last_heartbeat"))
    ident = acc.get("ea_identity") or {}
    reason = str(ident.get("reason") or "")
    from broker_env import broker_reports_real
    declared_real = str(acc.get("declared_environment") or acc.get("account_type") or "demo").lower() == "real"
    if broker_reports_real(acc) and not declared_real:
        return {"code": "real_money", "text": "This is a real-money account.", "buttons": [{"action": "switch_real", "label": "Switch to Real"}]}
    m = re.search(r"MT5 login mismatch: EA reports (\d+)", reason)
    if m and not ident.get("authoritative"):
        other = m.group(1)
        return {"code": "login_mismatch", "text": f"MT5 is logged in to #{other}, not #{login}.",
                "buttons": [{"action": "use_reported_login", "label": f"Use #{other} instead", "login": other},
                            {"action": "wait", "label": f"I'll log in to #{login}"}]}
    if rej_age is not None and rej_age <= 300 and (hb_age is None or rej_age < hb_age):
        return {"code": "old_token", "text": "MT5 is using an old connection key.", "buttons": [{"action": "repair", "label": "Repair"}]}
    sc = acc.get("ea_self_check") or {}
    hb_step = next((s for s in progress.get("steps", []) if s["id"] == "heartbeat"), {})
    if sc.get("webrequest_ok") is False or re.search(r"4014|WebRequest|blocks the address", f"{hb_step.get('detail', '')} {hb_step.get('hint', '')}", re.I) and hb_step.get("status") in ("blocked", "warn"):
        return {"code": "webrequest", "text": "MT5 is blocking STOIC.", "buttons": [{"action": "repair", "label": "Repair"}]}
    paired = next((s for s in progress.get("steps", []) if s["id"] == "token"), {}).get("status") == "done"
    since = _age_s(now, acc.get("pairing_redeemed_at") or acc.get("installer_paired_at") or acc.get("created_at"))
    if hb_age is None and paired and since is not None and since > NO_HEARTBEAT_AFTER_S:
        return {"code": "no_heartbeat", "text": f"We can't hear MT5 yet. Is MT5 open and logged in to #{login}?",
                "buttons": [{"action": "check_again", "label": "Check again"}, {"action": "repair", "label": "Repair"}]}
    if hb_age is not None and hb_age > 300:
        return {"code": "no_heartbeat", "text": f"We can't hear MT5 yet. Is MT5 open and logged in to #{login}?",
                "buttons": [{"action": "check_again", "label": "Check again"}, {"action": "repair", "label": "Repair"}]}
    return None


def activate_state(acc: dict, rules: dict | None, proj: dict, others: list) -> dict:
    """Screen 3 — ticks for demo, waiting line for real money, one sentence + fix when the rules block."""
    from broker_env import broker_environment, broker_reports_real
    env = broker_environment(acc)                      # the broker's own word (heartbeat trade mode) — attestation is the "admitted" tick
    adm = acc.get("admission") or {}
    approved = bool(proj.get("approved_hash")) and proj.get("approved_hash") == proj.get("inventory_hash")
    ticks = [{"id": "env", "label": "DEMO confirmed by the broker" if not broker_reports_real(acc) else "Real-money account confirmed by the broker",
              "done": (env == "DEMO" and not broker_reports_real(acc)) or broker_reports_real(acc)},
             {"id": "admitted", "label": "Admitted by the platform rules", "done": adm.get("state") == "admitted"},
             {"id": "approved", "label": "Inventory approved", "done": approved}]
    out = {"ticks": ticks, "done": all(t["done"] for t in ticks), "rules_policy": (rules or {}).get("policy_version"),
           "mode": "real" if broker_reports_real(acc) else "demo", "error": None}
    if not rules:
        out["error"] = {"code": "no_rules", "text": "The platform has no active rules policy yet — the operator must load one.", "buttons": [{"action": "wait", "label": "Check again"}]}
        return out
    if rules.get("expired"):
        out["error"] = {"code": "rules_expired", "text": "The platform rules expired — the operator must renew them.", "buttons": [{"action": "wait", "label": "Check again"}]}
        return out
    reason = str(adm.get("reason") or "")
    if adm.get("state") == "pending_operator":
        out["waiting_operator"] = True
        return out
    if adm.get("state") == "blocked":
        cap = int(rules.get("max_accounts_per_user") or 0)
        if "cap" in reason.lower() or re.search(r"more than \d+ account", reason):
            cand = next((o for o in others if o.get("trading_enabled") is True), None)
            if cand:
                out["error"] = {"code": "over_cap", "text": f"Your plan allows {cap} account{'s' if cap != 1 else ''} — disable {cand.get('label') or '#' + str(cand.get('account_number'))} to continue.",
                                "buttons": [{"action": "disable_account", "label": "Disable it", "account_id": str(cand["_id"]), "account_label": cand.get("label")}]}
            else:
                out["error"] = {"code": "over_cap", "text": f"Your plan allows {cap} account{'s' if cap != 1 else ''}.", "buttons": [{"action": "wait", "label": "Check again"}]}
        elif "broker" in reason.lower():
            out["error"] = {"code": "broker_not_allowed", "text": f"{acc.get('broker')} is not on the allowed broker list for this platform.", "buttons": [{"action": "wait", "label": "Check again"}]}
        elif "risk" in reason.lower():
            out["error"] = {"code": "risk_over_cap", "text": "The risk setting is above the platform cap — pick a lower risk on the next screen.", "buttons": [{"action": "lower_risk", "label": "Use Conservative"}]}
        elif "real" in reason.lower() or "demo" in reason.lower():
            out["error"] = {"code": "demo_only", "text": "This platform period is demo-only — connect a demo login.", "buttons": [{"action": "wait", "label": "Check again"}]}
        else:
            out["error"] = {"code": "blocked", "text": reason or "Outside the platform rules.", "buttons": [{"action": "wait", "label": "Check again"}]}
    return out


def current_screen(acc: dict, progress: dict, act: dict, bot_active: bool) -> int:
    if acc.get("mode") == "paper":
        return 5 if bot_active else 4
    if bot_active and (acc.get("admission") or {}).get("state") == "admitted":
        return 5
    connect_done = all(s["status"] == "done" for s in progress.get("steps", []) if s["id"] in ("token", "install", "binary", "heartbeat", "identity"))
    if not connect_done:
        return 2
    if not act["done"]:
        return 3
    return 4


@router.get("/{account_id}")
async def wizard_state(account_id: str, request: Request, user=Depends(get_current_user)):
    db = get_db()
    await rate_limit(db, "wizard_poll", f"user:{user['id']}", 120, 60, "Too many wizard polls", request=request)
    oid = parse_object_id(account_id, "Account")
    acc = await db.accounts.find_one({"_id": oid, "user_id": user["id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    import install_progress as ip
    import device_attestation as da
    import rules_policy as rp
    from inventory_projection import projection
    from broker_env import attested_environment
    now = datetime.now(timezone.utc)
    pairing = await db.pairing_tokens.find_one({"account_id": account_id}, sort=[("issued_at", -1)])
    installation = await db.installations.find_one({"account_id": account_id, "revoked": {"$ne": True}}, sort=[("created_at", -1)])
    base = str(request.base_url).rstrip("/")
    from ea_capabilities import accepted_ea_sha256s
    progress = ip.derive(acc, pairing, installation, attested=da.attested_hash(installation), accepted_hashes=accepted_ea_sha256s(),
                         request_base=base, forwarded_proto=request.headers.get("x-forwarded-proto"), forwarded_host=request.headers.get("x-forwarded-host"))
    rules = await rp.active(db)
    proj = await projection(db, user["id"])
    others = await db.accounts.find({"user_id": user["id"], "_id": {"$ne": oid}, "mode": {"$ne": "paper"}},
                                    {"label": 1, "account_number": 1, "trading_enabled": 1}).to_list(length=50)
    act = activate_state(acc, rules, proj, others)
    cfg = await db.bot_configs.find_one({"account_id": account_id}, {"active": 1, "risk_level": 1, "symbols": 1}) or {}
    caps = await rp.risk_caps(db)
    from risk import get_profile
    cards = []
    cap = caps.get("max_risk_pct")
    fitting = [c["id"] for c in RISK_CARDS if cap is None or float(get_profile(c["id"]).get("risk_pct") or 0) <= cap] or ["low"]
    for c in RISK_CARDS:
        pct = float(get_profile(c["id"]).get("risk_pct") or 0)
        capped = cap is not None and pct > cap
        cards.append({**c, "risk_pct": min(pct, cap) if capped else pct, "capped": capped,
                      "apply_level": fitting[-1] if capped else c["id"],          # a capped card saves the highest level that fits the rules
                      "meaning": (f"risks about {min(pct, cap):g}% per trade (capped by the platform rules)" if capped else c["meaning"])})
    suffix = acc.get("symbol_suffix") or acc.get("auto_detected_suffix") or ""
    symbols = [f"{s}{suffix}" for s in (cfg.get("symbols") or ["XAUUSD", "BTCUSD"])]
    screen = current_screen(acc, progress, act, bool(cfg.get("active")))
    checklist = [{"id": s["id"], "label": lbl, "done": s["status"] == "done", "status": s["status"]}
                 for s, lbl in zip(progress.get("steps", []), ("Code redeemed by the VPS", "Installer finished", "EA file verified (signed release)", "First heartbeat received", "Terminal verified"))]
    return {"account_id": account_id, "label": acc.get("label"), "broker": acc.get("broker"), "login": acc.get("account_number"),
            "environment": attested_environment(acc), "mode": acc.get("mode"), "account_type": acc.get("account_type"),
            "screen": screen, "connect": {"checklist": checklist, "done": all(c["done"] for c in checklist), "error": connect_error(acc, progress, now)},
            "activate": act, "start": {"risk_cards": cards, "risk_level": cfg.get("risk_level") or "medium", "symbols": symbols, "suffix": suffix, "bot_active": bool(cfg.get("active"))},
            "generated_at": now.isoformat()}


@router.post("/{account_id}/use-reported-login")
async def use_reported_login(account_id: str, request: Request, user=Depends(get_current_user)):
    """Screen 2 'Use #X instead' — the terminal is logged into a different login: adopt it (own account, audited).
    Only the login the EA actually reported can be adopted; nothing is typed."""
    db = get_db()
    oid = parse_object_id(account_id, "Account")
    acc = await db.accounts.find_one({"_id": oid, "user_id": user["id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    m = re.search(r"MT5 login mismatch: EA reports (\d+)", str((acc.get("ea_identity") or {}).get("reason") or ""))
    if not m:
        raise HTTPException(status_code=409, detail={"code": "no_reported_login", "message": "The EA has not reported a different login."})
    reported = m.group(1)
    dup = await db.accounts.find_one({"user_id": user["id"], "broker": acc.get("broker"), "account_number": reported, "_id": {"$ne": oid}})
    if dup:
        raise HTTPException(status_code=409, detail={"code": "login_exists", "message": f"#{reported} is already your account '{dup.get('label')}'. Open that one instead."})
    now = datetime.now(timezone.utc).isoformat()
    await db.accounts.update_one({"_id": oid}, {"$set": {"account_number": reported, "login_changed": {"from": acc.get("account_number"), "to": reported, "at": now, "source": "ea_reported"}},
                                                "$unset": {"ea_identity": "", "verified_identity": ""}})
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": user.get("email"), "action": "account_login_adopted_from_ea", "target_kind": "account", "target_id": account_id,
                              "reason": "wizard: Use the login the terminal reports", "at": now, "meta": {"from": acc.get("account_number"), "to": reported}})
    return {"ok": True, "login": reported}
