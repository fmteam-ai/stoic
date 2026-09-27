"""Broker statement reconciliation ledger (audit round 13 P2-05).

A public performance attestation is only honest when the platform's own books
equal the broker's SIGNED statement to the cent. Per account + period the
ledger stores: the verified statement (Ed25519 signature over the canonical
statement body by the statement-attestation key), the platform totals recomputed
from broker_deals / account_cashflows, every field-level discrepancy, and the
cash-flow-adjusted return under a VERSIONED formula. Any discrepancy, missing or
stale period, unknown account/environment or unreconciled deal sets the ledger
status to something other than RECONCILED — and the attestation gate withholds.
"""
import hashlib
import hmac
import re

from pymongo.errors import DuplicateKeyError
import json
import os
from datetime import datetime, timedelta, timezone

STATEMENT_SCHEMA = "stoic.broker-statement/v1"
RETURN_FORMULA_VERSION = "simple-dietz-v1"          # (E1 - E0 - F) / (E0 + 0.5·F), F = net external cash flow
MONEY_FIELDS = ("opening_balance", "closing_balance", "closing_equity", "unrealized_pnl", "trading_pnl",
                "commission", "swap", "deposits", "withdrawals", "corrections", "fx_conversion")
STATEMENT_MAX_AGE_DAYS = int(os.environ.get("STATEMENT_MAX_AGE_DAYS") or 35)
STATEMENT_MIN_PERIOD_DAYS = int(os.environ.get("STATEMENT_MIN_PERIOD_DAYS") or 7)
STATEMENT_MAX_PERIOD_DAYS = int(os.environ.get("STATEMENT_MAX_PERIOD_DAYS") or 35)
PERIOD_JOIN_TOLERANCE_H = 24
TOLERANCE = 0.005                                    # to the cent


def cents(v) -> float:
    return round(float(v or 0) + 0.0, 2)


def statement_body(st: dict) -> bytes:
    fields = ("schema", "statement_id", "account_id", "broker_login", "currency", "period_from", "period_to",
              "issuer", "issued_at", *MONEY_FIELDS)
    body = {k: st.get(k) for k in fields}
    for k in MONEY_FIELDS:
        body[k] = cents(body[k])
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def statement_problems(st: dict, signature_hex: str) -> list:
    problems = []
    if st.get("schema") != STATEMENT_SCHEMA:
        problems.append(f"schema must be {STATEMENT_SCHEMA}")
    for k in ("statement_id", "account_id", "broker_login", "currency", "period_from", "period_to", "issuer", "issued_at"):
        if not str(st.get(k) or "").strip():
            problems.append(f"{k} required")
    try:
        pf, pt = datetime.fromisoformat(str(st["period_from"])), datetime.fromisoformat(str(st["period_to"]))
        if pf.tzinfo is None or pt.tzinfo is None or pt <= pf:
            problems.append("period_from/period_to must be aware and ordered")
        else:
            days = (pt - pf).total_seconds() / 86400
            if days < STATEMENT_MIN_PERIOD_DAYS:
                problems.append(f"statement period shorter than {STATEMENT_MIN_PERIOD_DAYS} days")
            if days > STATEMENT_MAX_PERIOD_DAYS:
                problems.append(f"statement period longer than {STATEMENT_MAX_PERIOD_DAYS} days")
            ia = datetime.fromisoformat(str(st["issued_at"]))
            now = datetime.now(timezone.utc)
            if ia.tzinfo is None:
                problems.append("issued_at must be timezone-aware")
            elif ia > now + timedelta(minutes=5):
                problems.append("issued_at is in the future")
            elif ia < pt:
                problems.append("issued_at precedes period_to")
            elif (now - ia).days > STATEMENT_MAX_AGE_DAYS:
                problems.append(f"issued_at older than {STATEMENT_MAX_AGE_DAYS} days")
    except (KeyError, ValueError, TypeError):
        problems.append("period_from/period_to/issued_at invalid")
    for k in MONEY_FIELDS:
        if k not in st:
            problems.append(f"{k} required")
            continue
        try:
            float(st.get(k) or 0)
        except (TypeError, ValueError):
            problems.append(f"{k} must be numeric")
    if not problems:
        # internal identity of the statement itself
        lhs = cents(st["opening_balance"]) + cents(st["trading_pnl"]) + cents(st["commission"]) + cents(st["swap"]) \
            + cents(st["deposits"]) - cents(st["withdrawals"]) + cents(st["corrections"]) + cents(st["fx_conversion"])
        if abs(lhs - cents(st["closing_balance"])) > TOLERANCE:
            problems.append(f"statement does not balance: computed closing {lhs:.2f} != {cents(st['closing_balance']):.2f}")
        if abs(cents(st["closing_balance"]) + cents(st["unrealized_pnl"]) - cents(st["closing_equity"])) > TOLERANCE:
            problems.append("closing_equity != closing_balance + unrealized_pnl")
    for k in ("account_id", "statement_id", "currency", "issuer"):
        if k in st and not isinstance(st.get(k), str):
            problems.append(f"{k} must be a string")
    # SEC-001 (round 13 audit) — statement authenticity must come from an INDEPENDENT broker /
    # statement-attestation key; never the platform release key. Absent key ⇒ fail closed.
    pinned = (os.environ.get("STATEMENT_ATTESTATION_PUBLIC_KEY_B64") or "").strip()
    if not pinned:
        problems.append("STATEMENT_ATTESTATION_PUBLIC_KEY_B64 not configured — statements cannot be authenticated")
        return problems
    try:
        from release_signing import public_key_b64, verify_hex
        try:
            if pinned == public_key_b64():
                problems.append("statement-attestation key must differ from the platform release key")
                return problems
        except Exception:  # noqa: BLE001 — no release key configured here; independence holds trivially
            pass
        if not signature_hex or not verify_hex(statement_body(st), str(signature_hex), pinned):
            problems.append("statement signature does not verify against the statement-attestation key")
    except Exception:  # noqa: BLE001
        problems.append("statement signature does not verify against the statement-attestation key")
    return problems


def _ts(v):
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return datetime.fromtimestamp(int(float(v)), tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def platform_totals(db, user_id: str, account_id: str, period_from: str, period_to: str) -> dict:
    """Recompute the platform's books for the period from broker deals + recorded cash flows."""
    pf, pt = datetime.fromisoformat(period_from), datetime.fromisoformat(period_to)
    tot = {"trading_pnl": 0.0, "commission": 0.0, "swap": 0.0, "deals": 0, "unreconciled_deals": 0, "unknown_deals": 0}
    async for d in db.broker_deals.find({"user_id": user_id, "account_id": account_id},
                                        {"deal_time": 1, "occurred_at": 1, "profit": 1, "commission": 1, "swap": 1,
                                         "deal_entry": 1, "financial_reconciliation_status": 1}):
        when = _ts(d.get("deal_time")) or _ts(d.get("occurred_at"))
        if when is None:
            tot["unknown_deals"] += 1
            continue
        if not (pf <= when < pt):
            continue
        tot["deals"] += 1
        tot["trading_pnl"] += float(d.get("profit") or 0)
        tot["commission"] += float(d.get("commission") or 0)
        tot["swap"] += float(d.get("swap") or 0)
        if d.get("financial_reconciliation_status") not in ("complete", "not_in_scope"):
            tot["unreconciled_deals"] += 1
    flows = {"deposits": 0.0, "withdrawals": 0.0, "corrections": 0.0, "fx_conversion": 0.0}
    async for f in db.account_cashflows.find({"user_id": user_id, "account_id": account_id}):
        when = _ts(f.get("at"))
        if when and pf <= when < pt and f.get("kind") in flows:
            flows[f["kind"]] += float(f.get("amount") or 0)
    return {**{k: cents(v) for k, v in tot.items() if k in ("trading_pnl", "commission", "swap")},
            **{k: cents(v) for k, v in flows.items()},
            "deals": tot["deals"], "unreconciled_deals": tot["unreconciled_deals"], "unknown_deals": tot["unknown_deals"]}


def cash_flow_adjusted_return(st: dict) -> float | None:
    e0, e1 = cents(st["opening_balance"]), cents(st["closing_equity"])
    flow = cents(st["deposits"]) - cents(st["withdrawals"]) + cents(st["corrections"]) + cents(st["fx_conversion"])
    denom = e0 + 0.5 * flow
    if denom <= 0:
        return None
    return round((e1 - e0 - flow) / denom * 100.0, 4)


async def reconcile(db, user_id: str, statement: dict, signature_hex: str, actor_email: str) -> dict:
    """Verify, recompute, compare to the cent, persist the ledger row (idempotent per statement_id)."""
    from fastapi import HTTPException
    problems = statement_problems(statement, signature_hex)
    if problems:
        raise HTTPException(status_code=400, detail={"code": "statement_rejected", "problems": problems})
    acc = await db.accounts.find_one({"_id": _oid(statement["account_id"]), "user_id": user_id})
    if not acc:
        raise HTTPException(status_code=404, detail={"code": "unknown_account"})
    env = _environment(acc)
    # audit r14 P1-03 — identity binding: the signed broker_login must be the
    # account's VERIFIED broker identity and the issuer must be the account's
    # registered broker (or one of its registry server names).
    from identity_model import authoritative_account_number
    verified_login = str(authoritative_account_number(acc) or "").strip()
    if not verified_login or str(statement["broker_login"]).strip() != verified_login:
        raise HTTPException(status_code=400, detail={
            "code": "statement_rejected", "problems": ["broker_login does not match the account's verified broker identity"]})
    issuer = str(statement["issuer"]).strip().lower()
    known = {str(acc.get("broker") or "").lower(), str(acc.get("broker_server") or "").lower(),
             str(((acc.get("verified_identity") or {}).get("broker_server")) or "").lower()} - {""}
    prof = await db.broker_profiles.find_one({"$or": [{"name": {"$regex": f"^{re.escape(acc.get('broker') or '')}$", "$options": "i"}},
                                                      {"server_names": {"$elemMatch": {"$regex": f"^{re.escape(issuer)}$", "$options": "i"}}}]}) \
        if acc.get("broker") or issuer else None
    if prof:
        known |= {str(prof.get("name") or "").lower(), *[str(x).lower() for x in (prof.get("server_names") or [])]}
    _norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())  # noqa: E731
    if not known or _norm(issuer) not in {_norm(k) for k in known}:
        raise HTTPException(status_code=400, detail={
            "code": "statement_rejected", "problems": ["issuer is not the account's registered broker"]})
    # continuous period coverage against the latest accepted statement
    prev = await db.reconciliation_ledger.find_one(
        {"user_id": user_id, "account_id": statement["account_id"], "statement_id": {"$ne": statement["statement_id"]}},
        sort=[("period_to", -1)])
    if prev:
        pf_new, pt_new = datetime.fromisoformat(statement["period_from"]), datetime.fromisoformat(statement["period_to"])
        pf_prev, pt_prev = datetime.fromisoformat(prev["period_from"]), datetime.fromisoformat(prev["period_to"])
        gap_h = (pf_new - pt_prev).total_seconds() / 3600
        if gap_h > PERIOD_JOIN_TOLERANCE_H:
            problem = f"coverage gap of {gap_h:.0f}h after the previous statement"
        elif pt_new <= pf_prev + timedelta(hours=PERIOD_JOIN_TOLERANCE_H):
            problem = "coverage gap: statement ends before the accepted coverage begins (out of sequence)"
        elif gap_h < -PERIOD_JOIN_TOLERANCE_H:
            problem = f"period overlaps the previous statement by {-gap_h:.0f}h"
        else:
            problem = None
        if problem:
            raise HTTPException(status_code=400, detail={"code": "statement_rejected", "problems": [problem]})
    totals = await platform_totals(db, user_id, statement["account_id"], statement["period_from"], statement["period_to"])
    discrepancies = []
    for k in ("trading_pnl", "commission", "swap", "deposits", "withdrawals", "corrections", "fx_conversion"):
        s, p = cents(statement[k]), totals[k]
        if abs(s - p) > TOLERANCE:
            discrepancies.append({"field": k, "statement": s, "platform": p, "delta": round(s - p, 2)})
    status = "RECONCILED"
    if discrepancies:
        status = "DISCREPANCY"
    if totals["unreconciled_deals"] or totals["unknown_deals"]:
        status = "UNRECONCILED_DEALS"
    if env != "LIVE":
        status = f"NON_LIVE_ENVIRONMENT:{env}"
    if totals["deals"] == 0 and cents(statement["trading_pnl"]) != 0:
        status = "NO_PLATFORM_DEALS"
    row = {"_id": f"{statement['account_id']}:{statement['statement_id']}", "user_id": user_id,
           "account_id": statement["account_id"], "statement_id": statement["statement_id"],
           "period_from": statement["period_from"], "period_to": statement["period_to"], "currency": statement["currency"],
           "environment": env, "status": status, "statement": {k: cents(statement[k]) for k in MONEY_FIELDS},
           "platform": totals, "discrepancies": discrepancies,
           "return_pct": cash_flow_adjusted_return(statement), "return_formula_version": RETURN_FORMULA_VERSION,
           "statement_sha256": hashlib.sha256(statement_body(statement)).hexdigest(), "signature_hex": signature_hex,
           "issuer": statement["issuer"], "recorded_by": actor_email, "recorded_at": datetime.now(timezone.utc).isoformat()}
    # audit r14 P1-03 — APPEND-ONLY: an existing (account, statement_id) row is
    # immutable. Same bytes → idempotent; different bytes → refused.
    existing = await db.reconciliation_ledger.find_one({"_id": row["_id"]})
    if existing:
        if existing.get("statement_sha256") == row["statement_sha256"]:
            return existing
        raise HTTPException(status_code=409, detail={
            "code": "statement_overwrite_refused",
            "message": "A different statement with this id is already on the ledger — ledger rows are append-only."})
    # SEC-001 (r14 audit) — PER-USER hash chain with a unique (user_id, ledger_seq)
    # index: two concurrent imports cannot fork the chain (the loser retries
    # on DuplicateKeyError) and no tenant learns another tenant's sequence.
    await db.reconciliation_ledger.create_index([("user_id", 1), ("ledger_seq", 1)], unique=True,
                                                partialFilterExpression={"ledger_seq": {"$exists": True}})
    key = (os.environ.get("LEDGER_ANCHOR_KEY") or "").encode()
    for _attempt in range(8):
        last = await db.reconciliation_ledger.find_one({"user_id": user_id, "ledger_root": {"$exists": True}},
                                                       sort=[("ledger_seq", -1)])
        row["ledger_seq"] = int((last or {}).get("ledger_seq") or 0) + 1
        row["ledger_root"] = hashlib.sha256(
            f"{(last or {}).get('ledger_root') or ''}|{row['statement_sha256']}|{row['status']}".encode()).hexdigest()
        # SEC-004 — domain-separated HMAC (never the raw root, never shared with /api/status)
        row["ledger_root_sig"] = (hmac.new(key, f"stoic-statement-ledger-v1|{user_id}|{row['ledger_seq']}|{row['ledger_root']}".encode(),
                                           hashlib.sha256).hexdigest() if key else None)
        try:
            await db.reconciliation_ledger.insert_one(row)
            return row
        except DuplicateKeyError as e:
            existing = await db.reconciliation_ledger.find_one({"_id": row["_id"]})
            if existing:
                return existing  # same statement raced with itself → idempotent
            if _attempt == 7:
                raise HTTPException(status_code=503, detail={"code": "ledger_busy",
                                                             "message": "Ledger sequence contention — retry."}) from e
    return row


def _environment(acc: dict) -> str:
    try:
        from routes.performance_routes import _attestation_environment
        return _attestation_environment(acc)
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def _oid(v):
    from bson import ObjectId
    try:
        return ObjectId(str(v))
    except Exception:  # noqa: BLE001
        return v


async def ledger_gate(db, user_id: str) -> list:
    """Attestation-gate reasons: every enabled account needs a current RECONCILED period."""
    reasons = []
    now = datetime.now(timezone.utc)
    async for a in db.accounts.find({"user_id": user_id, "trading_enabled": True, "status": {"$ne": "deleted"}}, {"_id": 1}):
        row = await db.reconciliation_ledger.find_one({"user_id": user_id, "account_id": str(a["_id"])}, sort=[("period_to", -1)])
        if not row:
            reasons.append("STATEMENT_LEDGER_MISSING")
            continue
        if row["status"] != "RECONCILED":
            reasons.append(f"STATEMENT_{row['status'].split(':')[0]}")
        try:
            if (now - datetime.fromisoformat(row["period_to"])).days > STATEMENT_MAX_AGE_DAYS:
                reasons.append("STATEMENT_PERIOD_STALE")
        except (ValueError, TypeError):
            reasons.append("STATEMENT_PERIOD_STALE")
    return sorted(set(reasons))


async def ledger_rows(db, user_id: str, account_id: str | None = None) -> list:
    q = {"user_id": user_id, **({"account_id": account_id} if account_id else {})}
    rows = await db.reconciliation_ledger.find(q, {"signature_hex": 0}).sort("period_to", -1).to_list(200)
    for r in rows:
        r["id"] = r.pop("_id")
    return rows
