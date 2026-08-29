"""STOIC Certification Center (iter-154) — turns the six proof pillars
(Risk Truth, Position Truth, Execution Alpha, Digital Twin, Strategy Decay,
Broker Intelligence) into one scored view and issues PUBLIC, hash-chained,
independently verifiable certificates (like an SSL cert for a trading
account)."""
import os
import secrets
from datetime import datetime, timedelta, timezone

VALID_DAYS = 30
TIERS = ("CERTIFIED_A", "CERTIFIED_B", "PROVISIONAL", "UNCERTIFIED")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _status(score) -> str:
    if score is None:
        return "NO_DATA"
    return "GREEN" if score >= 85 else ("YELLOW" if score >= 60 else "RED")


def _pillar(name: str, score, detail: str, metrics: dict) -> dict:
    return {"pillar": name,
            "score": None if score is None else round(float(score), 1),
            "status": _status(score), "detail": detail, "metrics": metrics}


async def _risk_truth(db, acct_id: str) -> dict:
    since = (_now_dt() - timedelta(days=7)).isoformat()
    q = {"account_id": acct_id, "at": {"$gte": since}}
    total = await db.pamm_risk_decisions.count_documents(q)
    if not total:
        return _pillar("risk_truth", None,
                       "no governed decisions in the last 7 days",
                       {"decisions_7d": 0})
    hashed = await db.pamm_risk_decisions.count_documents(
        {**q, "hash": {"$exists": True, "$ne": None}})
    blocked = await db.pamm_risk_decisions.count_documents(
        {**q, "authorized": False})
    score = 100.0 * hashed / total
    return _pillar("risk_truth", score,
                   f"{hashed}/{total} decisions carry a hashed provenance "
                   f"snapshot; {blocked} guard block(s)",
                   {"decisions_7d": total, "hashed": hashed,
                    "blocked": blocked})


async def _position_truth(db, acct_id: str) -> dict:
    from soak_campaign import _account_invariants
    since = (_now_dt() - timedelta(days=7)).isoformat()
    inv = await _account_invariants(db, acct_id, since)
    if not inv.get("trades_24h"):
        return _pillar("position_truth", None,
                       "no trades in the last 7 days", inv)
    score = 100.0
    if inv.get("duplicate_executions"):
        score -= 40
    if inv.get("unconfirmed_ghosts"):
        score -= 40
    score -= min(20, (inv.get("rejects_24h") or 0) * 5)
    ur = inv.get("unknown_rate")
    if ur is not None:
        score -= min(20, ur * 50)
    detail = ("no duplicates, no ghosts, executions traced"
              if score >= 95 else "reconciliation anomalies detected")
    return _pillar("position_truth", max(0.0, score), detail, inv)


async def _execution_alpha(db, user_id: str) -> dict:
    since = (_now_dt() - timedelta(days=30)).isoformat()
    n = await db.execution_alpha_decisions.count_documents(
        {"user_id": user_id, "at": {"$gte": since}})
    if not n:
        return _pillar("execution_alpha", None,
                       "no execution-alpha decisions in 30 days — gate "
                       "engages on latency-critical strategies",
                       {"decisions_30d": 0})
    return _pillar("execution_alpha", 100.0,
                   f"gate active — {n} pre-trade execution decision(s) "
                   "logged in 30 days", {"decisions_30d": n})


async def _digital_twin(db, user_id: str, acct_id: str) -> dict:
    from digital_twin import twin_summary
    s = await twin_summary(db, user_id)
    accounts = s.get("accounts") or []
    mine = next((a for a in accounts
                 if str(a.get("account_id")) == acct_id), None)
    covered = len(accounts)
    if not covered:
        return _pillar("digital_twin", None,
                       "no closed auto-trades to replay in the twin yet",
                       {"accounts_covered": 0})
    replayed = int((s.get("totals") or {}).get("intercepts_replayed") or 0)
    score = 100.0 if mine else 75.0
    return _pillar("digital_twin", score,
                   f"twin replaying {covered} account(s), "
                   f"{replayed} intercepts replayed"
                   + ("" if mine else " (this account not yet covered)"),
                   {"accounts_covered": covered,
                    "intercepts_replayed": replayed,
                    "this_account": bool(mine)})


_DECAY_SCORE = {"HEALTHY": 100.0, "WATCH": 85.0, "DEGRADED": 60.0,
                "CRITICAL": 30.0, "RETIRED": 20.0}


async def _strategy_decay(db, user_id: str) -> dict:
    from strategy_decay import evaluate_all
    rows = await evaluate_all(db, user_id)
    if not rows:
        return _pillar("strategy_decay", None,
                       "no closed trades in 90 days — decay monitor idle",
                       {"scopes": 0})
    states = {str(r.get("scope")): str(r.get("state") or "HEALTHY")
              for r in rows}
    worst = min(states.values(),
                key=lambda st: _DECAY_SCORE.get(st, 30.0))
    return _pillar("strategy_decay", _DECAY_SCORE.get(worst, 30.0),
                   f"{len(states)} strategy scope(s) monitored — "
                   f"worst state {worst}",
                   {"scopes": len(states), "states": states})


async def _broker_intel(db, account: dict) -> dict:
    from broker_intel import score_account
    s = await score_account(db, account)
    score = s.get("score")
    if score is None:
        return _pillar("broker_intel", None,
                       "not enough execution telemetry for a broker score",
                       s)
    return _pillar("broker_intel", float(score),
                   f"broker execution score {score}/100 from live "
                   "spread/slippage/fill telemetry",
                   {k: v for k, v in s.items() if k != "components"})


def grade(pillars: list) -> dict:
    scored = [p["score"] for p in pillars if p["score"] is not None]
    coverage = len(scored)
    overall = round(sum(scored) / coverage, 1) if scored else None
    if overall is None:
        tier = "UNCERTIFIED"
    elif overall >= 85 and coverage >= 5:
        tier = "CERTIFIED_A"
    elif overall >= 70 and coverage >= 4:
        tier = "CERTIFIED_B"
    else:
        tier = "PROVISIONAL"
    return {"overall_score": overall, "pillars_scored": coverage,
            "pillars_total": len(pillars), "tier": tier}


async def pillar_scores(db, user_id: str, account: dict) -> dict:
    acct_id = str(account["_id"])
    pillars = []
    for fn, args in ((_risk_truth, (db, acct_id)),
                     (_position_truth, (db, acct_id)),
                     (_execution_alpha, (db, user_id)),
                     (_digital_twin, (db, user_id, acct_id)),
                     (_strategy_decay, (db, user_id)),
                     (_broker_intel, (db, account))):
        try:
            pillars.append(await fn(*args))
        except Exception as e:  # noqa: BLE001 — one pillar never kills all
            name = fn.__name__.lstrip("_")
            pillars.append(_pillar(name, None, f"unavailable: {e}", {}))
    return {"account_id": acct_id,
            "account_label": account.get("label"),
            "at": _now_dt().isoformat(),
            "pillars": pillars, **grade(pillars)}


# ─────────────────── public hash-chained certificates ─────────────────────

def _mask(num) -> str:
    s = str(num or "")
    return ("•" * max(0, len(s) - 3)) + s[-3:] if s else "—"


async def issue_public(db, user: dict, account: dict) -> dict:
    from modules.pamm.strategy_guard import GIT_COMMIT, GUARD_VERSION
    from soak_campaign import evidence_hash
    acct_id = str(account["_id"])
    # issuance cap — public certs are append-only records, keep them scarce
    day_ago = (_now_dt() - timedelta(days=1)).isoformat()
    cap = int(os.environ.get("CERT_ISSUE_DAILY_CAP", "10"))
    recent = await db.public_certificates.count_documents(
        {"account_id": acct_id, "issued_at": {"$gte": day_ago}})
    if recent >= cap:
        raise ValueError(f"issuance cap reached — max {cap} certificates "
                         "per account per 24h")
    scores = await pillar_scores(db, user["id"], account)
    prev = await db.public_certificates.find_one(
        {"account_id": acct_id}, sort=[("seq", -1)])
    seq = int(prev["seq"]) + 1 if prev else 1
    prev_hash = prev["hash"] if prev else "genesis"
    now = _now_dt()
    doc = {"cert_id": "STC-" + secrets.token_hex(5).upper(),
           "seq": seq, "prev_hash": prev_hash,
           "subject": account.get("label"),
           "broker": account.get("broker"),
           "account_ref": _mask(account.get("account_number")),
           "tier": scores["tier"],
           "overall_score": scores["overall_score"],
           "pillars_scored": scores["pillars_scored"],
           "pillars": [{"pillar": p["pillar"], "score": p["score"],
                        "status": p["status"], "detail": p["detail"]}
                       for p in scores["pillars"]],
           "issued_at": now.isoformat(),
           "expires_at": (now + timedelta(days=VALID_DAYS)).isoformat(),
           "revoked": False,
           "provenance": {"git_commit": GIT_COMMIT,
                          "guard_policy_version": GUARD_VERSION},
           # internal linkage — stripped from the public view
           "account_id": acct_id, "user_id": user["id"]}
    doc["hash"] = evidence_hash(
        {k: v for k, v in doc.items()
         if k not in ("account_id", "user_id", "revoked")}, prev_hash)
    await db.public_certificates.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def _validity(cert: dict) -> dict:
    expired = str(cert.get("expires_at") or "") <= _now_dt().isoformat()
    return {"valid": not expired and not cert.get("revoked"),
            "expired": expired, "revoked": bool(cert.get("revoked"))}


def public_view(cert: dict) -> dict:
    pub = {k: v for k, v in cert.items()
           if k not in ("_id", "account_id", "user_id")}
    return {**pub, **_validity(cert),
            "verify": "hash = sha256(prev_hash + canonical JSON of the "
                      "certificate without hash/account_id/user_id/_id); "
                      "certificates for one account form an append-only "
                      "chain — any edit breaks the hash."}


def verify_certificate(cert: dict) -> bool:
    """Re-verifies the ISSUED content — revocation is a status change and
    intentionally not part of the issuance hash."""
    from soak_campaign import evidence_hash
    body = {k: v for k, v in cert.items()
            if k not in ("hash", "_id", "account_id", "user_id",
                         "valid", "expired", "verify", "hash_verified",
                         "revoked", "revoked_at", "revoked_reason")}
    return evidence_hash(body, cert.get("prev_hash") or "genesis") \
        == cert.get("hash")


async def revoke_public(db, cert_id: str, user: dict, reason: str) -> dict:
    q = {"cert_id": cert_id, "revoked": False}
    if user.get("role") != "admin":
        q["user_id"] = user["id"]
    else:
        from auth import require_admin
        require_admin(user)  # cross-tenant revoke needs the MFA gate (SEC-001)
    r = await db.public_certificates.find_one_and_update(
        q, {"$set": {"revoked": True, "revoked_at": _now_dt().isoformat(),
                     "revoked_reason": reason}})
    return {"ok": bool(r), "cert_id": cert_id}
