"""Phase 3 (U12 U13 U14) — rules-based signed policy + automatic admission.

Instead of a signed list of account ids (one GitHub workflow + release + two approvals per account), the
operator signs RULES once: demo_only, max_accounts_per_user, allowed_brokers, risk caps, expiry. v2 signature
(purpose "policy-rules") and installation-id binding stay. Accounts that satisfy the rules are ADMITTED
automatically (audited with the evidence); real-money accounts wait for ONE in-app operator approval
(step-up: password + authenticator). Policies load from `policies/*.json` at startup or by admin upload —
no release. Fail closed: anything unevaluable → not admitted."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

logger = logging.getLogger(__name__)

SCHEMA = "stoic.policy-rules/v1"
PURPOSE = "policy-rules"
STATE_ID = "rules_policy"
POLICIES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policies")
BODY_FIELDS = ("schema", "installation_id", "environment", "policy_version", "demo_only", "max_accounts_per_user",
               "allowed_brokers", "risk_caps", "reason", "issuer", "issued_at", "expires_at", "nonce")
VERSION_RE = r"^[A-Za-z0-9._-]{1,64}$"
SYSTEM_ACTOR = "system:rules"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def body(pol: dict) -> bytes:
    b = {k: pol.get(k) for k in BODY_FIELDS}
    b["demo_only"] = bool(b.get("demo_only"))
    b["allowed_brokers"] = sorted(str(x).strip().lower() for x in (b.get("allowed_brokers") or []))
    b["risk_caps"] = {k: b["risk_caps"][k] for k in sorted(b.get("risk_caps") or {})}
    return json.dumps(b, sort_keys=True, separators=(",", ":")).encode()


def problems(pol: dict, *, now: datetime | None = None) -> list:
    from inventory_projection import (DEMO_POLICY_MAX_DAYS, POLICY_MAX_DAYS, environment_label, installation_id)
    from release_signing import verify_hex
    now = now or _now()
    errs = []
    if not isinstance(pol, dict):
        return ["policy is not an object"]
    try:                                                   # C2 — a future-dated issue would win every "newest" comparison
        issued = datetime.fromisoformat(str(pol.get("issued_at")).replace("Z", "+00:00"))
        if issued > now + timedelta(minutes=5):
            errs.append("issued_at is in the future")
    except (TypeError, ValueError):
        pass
    if pol.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA}")
    if not re.match(VERSION_RE, str(pol.get("policy_version") or "")):
        errs.append("policy_version invalid")
    want = installation_id()
    if want:
        if pol.get("installation_id") != want:
            errs.append("installation_id does not name this installation")
    else:
        from app_env import is_production
        if is_production():          # M1 (main127) — production without STOIC_INSTALLATION_ID: fail closed, never load an unbound policy
            errs.append("STOIC_INSTALLATION_ID is not set on this host — a rules policy cannot be bound to the installation (fail closed)")
    if pol.get("environment") != environment_label():
        errs.append(f"environment must be {environment_label()}")
    cap = pol.get("max_accounts_per_user")
    if not isinstance(cap, int) or cap < 1 or cap > 50:
        errs.append("max_accounts_per_user must be 1..50")
    if not isinstance(pol.get("allowed_brokers"), list) or not pol.get("allowed_brokers"):
        errs.append("allowed_brokers must be a non-empty list ('*' = any registry broker)")
    rc = pol.get("risk_caps") or {}
    for k in ("max_risk_pct", "max_daily_loss_pct"):
        v = rc.get(k)
        if v is not None and not (isinstance(v, (int, float)) and 0 < v <= 100):
            errs.append(f"risk_caps.{k} must be 0 < x <= 100")
    try:
        issued = datetime.fromisoformat(str(pol.get("issued_at")).replace("Z", "+00:00"))
        exp = datetime.fromisoformat(str(pol.get("expires_at")).replace("Z", "+00:00"))
        days = (exp - issued).total_seconds() / 86400
        limit = DEMO_POLICY_MAX_DAYS if pol.get("demo_only") else POLICY_MAX_DAYS
        if days <= 0 or days > limit:
            errs.append(f"expires_at must be within 1..{limit} days of issued_at")
        if exp <= now:
            errs.append("policy already expired")
    except (TypeError, ValueError):
        errs.append("issued_at/expires_at must be ISO-8601")
    if not pol.get("nonce") or not pol.get("issuer") or not str(pol.get("reason") or "").strip():
        errs.append("nonce, issuer and reason are required")
    sig = pol.get("signature_hex")
    if not sig:
        errs.append("signature_hex missing")
    elif not errs and not verify_hex(body(pol), str(sig), purpose=PURPOSE):
        errs.append("signature does not verify under the release key")
    return errs


async def active(db) -> dict | None:
    pol = await db.platform_state.find_one({"_id": STATE_ID})
    if not pol:
        return None
    try:
        if datetime.fromisoformat(str(pol["expires_at"]).replace("Z", "+00:00")) <= _now():
            return {**pol, "expired": True}
    except (KeyError, ValueError):
        return None
    return pol


async def activate(db, pol: dict, *, actor: str, source: str) -> dict:
    """Verify, replay-check the nonce, store as the ONE active rules policy, audit. Raises 422 on problems."""
    errs = problems(pol)
    if errs:
        raise HTTPException(status_code=422, detail={"code": "rules_policy_invalid", "problems": errs})
    if await db.policy_migration_nonces.find_one({"_id": str(pol["nonce"])}):
        raise HTTPException(status_code=409, detail={"code": "rules_policy_replayed"})
    await db.policy_migration_nonces.insert_one({"_id": str(pol["nonce"]), "at": _now().isoformat(), "kind": "rules"})
    doc = {k: pol.get(k) for k in BODY_FIELDS}
    doc.update({"_id": STATE_ID, "signature_hex": pol["signature_hex"], "activated_at": _now().isoformat(),
                "activated_by": actor, "source": source})
    prev = await db.platform_state.find_one({"_id": STATE_ID}, {"policy_version": 1})
    await db.platform_state.replace_one({"_id": STATE_ID}, doc, upsert=True)
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": actor, "action": "rules_policy_activated", "target_kind": "platform",
                              "target_id": STATE_ID, "reason": str(pol.get("reason")), "at": doc["activated_at"],
                              "meta": {"policy_version": pol["policy_version"], "previous": (prev or {}).get("policy_version"),
                                       "demo_only": bool(pol.get("demo_only")), "max_accounts_per_user": pol["max_accounts_per_user"],
                                       "allowed_brokers": pol["allowed_brokers"], "risk_caps": pol.get("risk_caps") or {},
                                       "expires_at": pol["expires_at"], "source": source}})
    logger.info("rules policy %s activated (%s)", pol["policy_version"], source)
    return doc


async def load_from_repo(db) -> dict | None:
    """U13 — startup: the newest valid policy file under policies/ that is not yet active becomes active
    (no release needed: the file is signed, the server only verifies). Invalid files are logged, never applied."""
    if not os.path.isdir(POLICIES_DIR):
        return None
    cur = await db.platform_state.find_one({"_id": STATE_ID}, {"policy_version": 1, "issued_at": 1})
    best = None
    for name in sorted(os.listdir(POLICIES_DIR)):
        if not name.endswith(".json"):
            continue
        try:
            pol = json.load(open(os.path.join(POLICIES_DIR, name), encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("rules policy file %s unreadable: %s", name, e)
            continue
        if not isinstance(pol, dict):                      # C1 — a JSON list/scalar is not a policy
            logger.warning("rules policy file %s skipped: not an object", name)
            continue
        try:
            errs = problems(pol)
        except Exception as e:  # noqa: BLE001 — C1: one malformed file must never abort loading of the others
            logger.warning("rules policy file %s skipped: malformed (%s)", name, type(e).__name__)
            continue
        if errs:
            logger.info("rules policy file %s skipped: %s", name, errs[:2])
            continue
        if best is None or str(pol.get("issued_at")) > str(best.get("issued_at")):
            best = pol
    if not best or (cur and str(cur.get("issued_at") or "") >= str(best.get("issued_at"))):
        return None
    if await db.policy_migration_nonces.find_one({"_id": str(best["nonce"])}):
        return None
    return await activate(db, best, actor="system:policies-dir", source="repo")


# ── admission ─────────────────────────────────────────────────────────────────────────────────────────
def cap_count_query(acc: dict) -> dict:
    """Easy Start — accounts that count toward max_accounts_per_user: this one plus every ENABLED non-paper account
    of the same user. A disabled account stops counting, so "Disable it" is a real fix for an over-cap sentence."""
    return {"user_id": acc.get("user_id"), "mode": {"$ne": "paper"}, "$or": [{"trading_enabled": True}, {"_id": acc["_id"]}]}


def broker_allowed(pol: dict, broker: str) -> bool:
    allowed = {str(x).strip().lower() for x in (pol.get("allowed_brokers") or [])}
    b = str(broker or "").strip().lower()
    if "*" in allowed:
        from broker_presets import BROKER_PRESETS
        return b in {p["broker"].lower() for p in BROKER_PRESETS} or b in allowed
    return b in allowed


def effective_max_risk_pct(cfg: dict | None, clamp: float | None = None) -> float:
    """M2 (main127) — the risk the bot can ACTUALLY use for this config: the risk-level profile's risk_pct, lifted
    to adaptive_risk_cap_pct when adaptive sizing may scale up (bot_runner: adaptive → cap → Kelly). Saved configs
    carry risk_level/adaptive_risk_cap_pct, never a literal risk_per_trade_pct."""
    from risk import get_profile
    cfg = cfg or {}
    base = float(get_profile(str(cfg.get("risk_level") or "medium")).get("risk_pct") or 0)
    explicit = cfg.get("risk_percent") or cfg.get("risk_per_trade_pct") or cfg.get("risk_pct")
    if explicit is not None:
        try:
            base = max(base, float(explicit))
        except (TypeError, ValueError):
            pass
    if cfg.get("adaptive_sizing_enabled", True):
        adaptive = float(cfg.get("adaptive_risk_cap_pct") or 2.0)
        if clamp is not None:
            adaptive = min(adaptive, float(clamp))      # the runtime clamp (bot_runner) caps adaptive scaling at the policy cap
        base = max(base, adaptive)
    return base


async def risk_caps(db) -> dict:
    """Active policy risk caps for the runtime clamps (bot_runner sizing, circuit_breakers daily loss). {} = none."""
    pol = await active(db)
    if not pol or pol.get("expired"):
        return {}
    return {k: float(v) for k, v in (pol.get("risk_caps") or {}).items() if isinstance(v, (int, float)) and v > 0}


def evaluate_admission(acc: dict, pol: dict, *, user_account_count: int, bot_cfg: dict | None = None) -> dict:
    """Pure. {'state': admitted|pending_operator|blocked, 'reason', 'checks'} — fail closed."""
    from broker_env import attested_environment, broker_reports_real
    if pol.get("expired"):
        return {"state": "blocked", "reason": "rules policy expired — operator must activate a new one", "checks": {}}
    env = attested_environment(acc)
    checks = {
        "broker_allowed": broker_allowed(pol, acc.get("broker")),
        "within_account_cap": user_account_count <= int(pol.get("max_accounts_per_user") or 0),
        "demo_attested": env == "DEMO",
        "not_real_money_reported": not broker_reports_real(acc),
    }
    rc = pol.get("risk_caps") or {}
    if bot_cfg is not None and rc.get("max_risk_pct") is not None:
        eff = effective_max_risk_pct(bot_cfg, clamp=float(rc["max_risk_pct"]))
        checks["risk_within_cap"] = eff <= float(rc["max_risk_pct"])
        checks["effective_max_risk_pct"] = eff
    if not checks["broker_allowed"]:
        return {"state": "blocked", "reason": f"broker {acc.get('broker')} is not in the policy's allowed brokers", "checks": checks}
    if not checks["within_account_cap"]:
        return {"state": "blocked", "reason": f"more than {pol.get('max_accounts_per_user')} account(s) for this user", "checks": checks}
    if checks.get("risk_within_cap") is False:
        return {"state": "blocked", "reason": f"risk per trade above the policy cap {rc.get('max_risk_pct')}%", "checks": checks}
    if env == "DEMO" and checks["not_real_money_reported"]:
        return {"state": "admitted", "reason": "demo account within the rules — admitted automatically", "checks": checks}
    if pol.get("demo_only"):
        return {"state": "blocked", "reason": "demo-only policy — this account is not an attested DEMO account", "checks": checks}
    return {"state": "pending_operator", "reason": "real money needs one operator approval (password + authenticator)", "checks": checks}


async def sync_admission(db, acc: dict) -> dict | None:
    """Heartbeat / creation hook: compute, persist on change, audit. Operator approvals are never overwritten
    while their conditions hold; a broker-reported real mode on an auto-admitted demo account revokes it."""
    pol = await active(db)
    if not pol:
        return None
    n = await db.accounts.count_documents(cap_count_query(acc))
    cfg = await db.bot_configs.find_one({"account_id": str(acc["_id"])},
                                        {"risk_level": 1, "risk_percent": 1, "risk_per_trade_pct": 1, "risk_pct": 1,
                                         "adaptive_sizing_enabled": 1, "adaptive_risk_cap_pct": 1})
    d = evaluate_admission(acc, pol, user_account_count=n, bot_cfg=cfg)
    cur = acc.get("admission") or {}
    if cur.get("state") == "admitted" and cur.get("by") not in (SYSTEM_ACTOR, None) and d["state"] == "pending_operator":
        # operator-approved real-money account stays admitted — but ONLY for the identity it was approved on
        from broker_env import attestation_identity
        if cur.get("identity_hash") and cur.get("identity_hash") == attestation_identity(acc):
            return cur
        d = {"state": "pending_operator", "reason": "operator approval was bound to a different terminal/server/login identity — re-approval needed",
             "checks": {**d["checks"], "approval_identity_hash": cur.get("identity_hash"), "current_identity_hash": attestation_identity(acc)}}
    if cur.get("state") == d["state"] and cur.get("policy_version") == pol.get("policy_version"):
        return cur
    now = _now().isoformat()
    new = {"state": d["state"], "reason": d["reason"], "by": SYSTEM_ACTOR, "at": now,
           "policy_version": pol.get("policy_version"), "checks": d["checks"]}
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"admission": new}})
    acc["admission"] = new
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": SYSTEM_ACTOR, "action": f"account_admission_{d['state']}", "target_kind": "account",
                              "target_id": str(acc["_id"]), "reason": d["reason"], "at": now,
                              "meta": {"policy_version": pol.get("policy_version"), "checks": d["checks"], "previous": cur.get("state")}})
    return new


async def operator_approve(db, acc: dict, *, approver_email: str) -> dict:
    """Real money: ONE in-app approval (caller enforced step-up). Refused for demo-only policies."""
    pol = await active(db)
    if not pol or pol.get("expired"):
        raise HTTPException(status_code=409, detail={"code": "no_active_rules_policy"})
    if pol.get("demo_only"):
        raise HTTPException(status_code=409, detail={"code": "demo_only_policy", "message": "the active policy admits demo accounts only"})
    n = await db.accounts.count_documents(cap_count_query(acc))
    d = evaluate_admission(acc, pol, user_account_count=n)
    if d["state"] == "blocked":
        raise HTTPException(status_code=409, detail={"code": "admission_blocked", "message": d["reason"]})
    now = _now().isoformat()
    from broker_env import attestation_identity
    new = {"state": "admitted", "reason": "real-money account approved by the operator", "by": approver_email, "at": now,
           "policy_version": pol.get("policy_version"), "checks": d["checks"],
           "identity_hash": attestation_identity(acc)}       # the approval is bound to THIS terminal/server/login identity
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"admission": new}})
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": approver_email, "action": "account_admission_operator_approved", "target_kind": "account",
                              "target_id": str(acc["_id"]), "reason": new["reason"], "at": now,
                              "meta": {"policy_version": pol.get("policy_version"), "checks": d["checks"]}})
    return new


def days(n: int) -> timedelta:
    return timedelta(days=n)


# ── Easy Start A1 / A3 / A4 ───────────────────────────────────────────────────────────────────────────
RENEW_REMINDER_DAYS = 7


def github_repo() -> str:
    return (os.environ.get("GITHUB_REPO") or "").strip().strip("/")


def renew_link() -> str | None:
    repo = github_repo()
    return f"https://github.com/{repo}/actions/workflows/rules-policy.yml" if repo else None


def expiry(pol: dict | None, now: datetime | None = None) -> dict:
    """A4 — {'expires_at','days_left','reminder_due','expired'} for the ACTIVE rules policy (7-day reminder)."""
    if not pol or not pol.get("expires_at"):
        return {"expires_at": None, "days_left": None, "reminder_due": False, "expired": False}
    now = now or _now()
    try:
        exp_at = datetime.fromisoformat(str(pol["expires_at"]).replace("Z", "+00:00"))
    except ValueError:
        return {"expires_at": str(pol["expires_at"]), "days_left": 0, "reminder_due": False, "expired": True}
    left = (exp_at - now).total_seconds() / 86400
    import math
    days_left = max(0, math.ceil(left))
    return {"expires_at": exp_at.isoformat(), "days_left": days_left, "reminder_due": 0 < left <= RENEW_REMINDER_DAYS, "expired": left <= 0}


def renewal_prefill(pol: dict) -> dict:
    """A4 — the workflow-form values for the next period (same rules, new version suffix)."""
    import re
    v = str(pol.get("policy_version") or "rules-v1")
    m = re.search(r"(\d+)$", v)
    nxt = (v[: m.start()] + str(int(m.group(1)) + 1)) if m else v + "-2"
    rc = pol.get("risk_caps") or {}
    exp = expiry(pol)
    issued, expires = pol.get("issued_at"), pol.get("expires_at")
    try:
        period_days = max(1, round((datetime.fromisoformat(str(expires).replace("Z", "+00:00")) - datetime.fromisoformat(str(issued).replace("Z", "+00:00"))).days))
    except (TypeError, ValueError):
        period_days = 30 if pol.get("demo_only") else 45
    return {"policy_version": nxt, "demo_only": bool(pol.get("demo_only")), "max_accounts_per_user": pol.get("max_accounts_per_user"),
            "allowed_brokers": ",".join(pol.get("allowed_brokers") or ["*"]), "max_risk_pct": rc.get("max_risk_pct"),
            "max_daily_loss_pct": rc.get("max_daily_loss_pct"), "expires_days": period_days, "installation_id": pol.get("installation_id"),
            "reason": f"renew {v} ({exp['days_left']} day(s) left)", "link": renew_link()}


def _github_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "stoic-rules-policy"}
    tok = os.environ.get("GITHUB_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def fetch_candidates_from_github() -> list:
    """A1 — every policies/*.json on the repo's default branch (GITHUB_REPO; token optional for public repos).
    Returns [] when the repo is not configured or unreachable — the caller falls back to the local policies/ dir."""
    repo = github_repo()
    if not repo:
        return []
    import httpx
    out = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=_github_headers()) as cli:
            r = await cli.get(f"https://api.github.com/repos/{repo}/contents/policies")
            if r.status_code != 200:
                logger.info("rules policy: github contents %s", r.status_code)
                return []
            for item in r.json() if isinstance(r.json(), list) else []:
                if not str(item.get("name", "")).endswith(".json") or not item.get("download_url"):
                    continue
                if not str(item["download_url"]).startswith("https://raw.githubusercontent.com/"):
                    continue
                f = await cli.get(item["download_url"])
                if f.status_code == 200:
                    try:
                        pol = f.json()
                        if isinstance(pol, dict):
                            out.append({"name": item["name"], "policy": pol})
                    except ValueError:
                        pass
    except Exception as e:  # noqa: BLE001 — network is best effort; the local dir is the fallback
        logger.warning("rules policy: github fetch failed: %s", type(e).__name__)
    return out


def local_candidates() -> list:
    out = []
    if not os.path.isdir(POLICIES_DIR):
        return out
    for name in sorted(os.listdir(POLICIES_DIR)):
        if name.endswith(".json"):
            try:
                out.append({"name": name, "policy": json.load(open(os.path.join(POLICIES_DIR, name), encoding="utf-8"))})
            except (OSError, ValueError):
                pass
    return out


async def load_latest(db, *, actor: str) -> dict:
    """A1 — one click: newest VALID signed policy from GitHub policies/ (or the local dir) becomes active.
    Verifies only (signature, installation, environment, expiry, nonce). Never downgrades to an older issue."""
    cands = await fetch_candidates_from_github()
    source = "github"
    if not cands:
        cands, source = local_candidates(), "repo"
    valid, rejected = [], []
    for c in cands:
        errs = problems(c["policy"])
        (rejected if errs else valid).append({"name": c["name"], "problems": errs[:2]} if errs else c)
    if not valid:
        return {"activated": False, "source": source, "reason": "no valid signed policy found", "rejected": rejected[:5]}
    best = max(valid, key=lambda c: str(c["policy"].get("issued_at") or ""))
    cur = await db.platform_state.find_one({"_id": STATE_ID}, {"policy_version": 1, "issued_at": 1})
    if cur and str(cur.get("issued_at") or "") >= str(best["policy"].get("issued_at") or ""):
        return {"activated": False, "source": source, "reason": f"{cur.get('policy_version')} is already the newest", "policy_version": cur.get("policy_version"), "rejected": rejected[:5]}
    doc = await activate(db, best["policy"], actor=actor, source=source)
    return {"activated": True, "source": source, "policy_version": doc["policy_version"], "expires_at": doc["expires_at"], "file": best["name"], "rejected": rejected[:5]}


async def retire_id_list(db, *, actor: str, reason: str = "") -> dict:
    """A3 — one-time: the signed per-account id list (platform_state.inventory_expectation) is retired in favour
    of the active rules policy. Archived (never deleted outright), audited. Refused without an unexpired policy."""
    pol = await active(db)
    if not pol or pol.get("expired"):
        raise HTTPException(status_code=409, detail={"code": "rules_policy_required", "message": "activate an unexpired rules policy first — fail closed"})
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"})
    if not exp:
        return {"retired": False, "reason": "no account-list policy in force", "rules_policy": pol.get("policy_version")}
    now = _now().isoformat()
    archived = {**exp, "_id": "inventory_expectation_retired", "retired_at": now, "retired_by": actor,
                "replaced_by_rules_policy": pol.get("policy_version"), "retire_reason": reason}
    await db.platform_state.replace_one({"_id": "inventory_expectation_retired"}, archived, upsert=True)
    await db.platform_state.delete_one({"_id": "inventory_expectation"})
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": actor, "action": "inventory_expectation_retired", "target_kind": "platform",
                              "target_id": "inventory_expectation", "reason": reason or "replaced by rules policy", "at": now,
                              "meta": {"retired_policy_version": exp.get("policy_version"), "account_ids": exp.get("account_ids"),
                                       "rules_policy": pol.get("policy_version")}})
    return {"retired": True, "retired_policy_version": exp.get("policy_version"), "rules_policy": pol.get("policy_version")}
