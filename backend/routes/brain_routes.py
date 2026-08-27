"""STOIC Brain API — /api/brain (Phase A: regime 2.0, router,
uncertainty, meta decisions, portfolio factor brain)."""
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/brain", tags=["brain"])


async def _owned_account(db, user, account_id: str) -> dict:
    """SEC-001 fix — resolve an account WITH ownership enforced; 404 when
    the account is not the caller's (admins may inspect any account)."""
    from route_utils import parse_object_id
    q = {"_id": parse_object_id(account_id)}
    if user.get("role") != "admin":
        q["user_id"] = user["id"]
    acc = await db.accounts.find_one(q)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return acc


@router.get("/regime")
async def regime_state_ep(symbol: str = Query("XAUUSD"),
                          account_id: str | None = None,
                          user=Depends(get_current_user)):
    from regime_intelligence import market_state
    db = get_db()
    if account_id:
        await _owned_account(db, user, account_id)
    return await market_state(db, user["id"], symbol,
                              account_id=account_id)


@router.get("/router")
async def router_ep(symbol: str = Query("XAUUSD"),
                    user=Depends(get_current_user)):
    from regime_intelligence import market_state
    from strategy_router import route
    db = get_db()
    state = await market_state(db, user["id"], symbol)
    fp = state.get("fingerprint_key") or "UNKNOWN"
    routing = await route(db, user["id"], fp)
    return {"market_state": state, **routing}


@router.get("/meta/recent")
async def meta_recent_ep(limit: int = 20,
                         user=Depends(get_current_user)):
    db = get_db()
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    lim = max(1, min(int(limit), 100))
    return {"decisions": [d async for d in db.meta_decisions.find(
        q, {"_id": 0}).sort("at", -1).limit(lim)]}


@router.get("/portfolio")
async def portfolio_brain_ep(account_id: str,
                             user=Depends(get_current_user)):
    import portfolio_risk
    db = get_db()
    acc = await _owned_account(db, user, account_id)
    equity = float(acc.get("equity") or 0)
    owner_id = str(acc.get("user_id") or user["id"])
    snap = await portfolio_risk.snapshot(db, str(acc["_id"]), equity,
                                         user_id=owner_id)
    snap["factors"] = portfolio_risk.factor_exposure(
        snap["open_positions"], equity)
    return snap


# ───────────────────────── Phase B — v59 brain endpoints ─────────────────

@router.get("/decisions")
async def decisions_list_ep(limit: int = 20,
                            user=Depends(get_current_user)):
    db = get_db()
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    lim = max(1, min(int(limit), 100))
    return {"decisions": [d async for d in db.decision_contexts.find(
        q, {"_id": 0}).sort("at", -1).limit(lim)]}


@router.get("/decisions/{decision_id}")
async def decision_ep(decision_id: str, user=Depends(get_current_user)):
    from decision_context import get_decision
    db = get_db()
    doc = await get_decision(db, decision_id, user_id=user["id"],
                             admin=user.get("role") == "admin")
    if not doc:
        raise HTTPException(status_code=404, detail="Decision not found")
    return doc


@router.get("/memory")
async def memory_ep(symbol: str = Query("XAUUSD"),
                    user=Depends(get_current_user)):
    from market_memory import recall, verdict
    from regime_intelligence import market_state
    db = get_db()
    state = await market_state(db, user["id"], symbol)
    mem = await recall(db, user["id"], symbol, state.get("vector"),
                       session=state.get("session"))
    return {"market_state": {"fingerprint_key":
                             state.get("fingerprint_key"),
                             "labels": state.get("labels"),
                             "session": state.get("session"),
                             "available": state.get("available")},
            "memory": mem, "verdict": verdict(mem)}


@router.get("/strategy-health")
async def strategy_health_ep(user=Depends(get_current_user)):
    from strategy_decay import evaluate_all
    db = get_db()
    return {"strategies": await evaluate_all(db, user["id"])}


@router.get("/degraded")
async def degraded_ep(user=Depends(get_current_user)):
    from degraded_intelligence import status
    out = await status(get_db())
    # SEC-002 — internal error strings are admin-only; regular users get
    # the mode + per-subsystem booleans and fallback policy only.
    if user.get("role") != "admin":
        for sub in out.get("subsystems", {}).values():
            sub.pop("last_error", None)
    return out


@router.get("/costs")
async def costs_ep(symbol: str = Query("XAUUSD"),
                   scope: str | None = None,
                   account_id: str | None = None,
                   user=Depends(get_current_user)):
    from transaction_costs import expected_cost_r
    db = get_db()
    if account_id:
        # SEC (iter-210) — BOLA: account_id must belong to the caller
        await _owned_account(db, user, account_id)
    return await expected_cost_r(db, user["id"], symbol,
                                 scope=scope, account_id=account_id)


@router.post("/challenger/{model_id}/qualify")
async def qualify_challenger_ep(model_id: str,
                                user=Depends(get_current_user)):
    from champion_challenger2 import QualifyRateLimited, qualify
    try:
        return await qualify(get_db(), user["id"], model_id)
    except QualifyRateLimited as e:
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited",
                    "message": "Qualification replays are CPU-intensive"
                               " — please retry shortly",
                    "retry_in_s": round(e.retry_in_s)})
    except ValueError:
        raise HTTPException(status_code=404,
                            detail="shadow model not found")
    except Exception:
        raise HTTPException(status_code=400,
                            detail="qualification failed — invalid model"
                                   " id or replay error")


# ───────────────────── v60 — Execution Intelligence ─────────────────────

@router.get("/broker-matrix")
async def broker_matrix_ep(days: int = Query(30, ge=1, le=90),
                           user=Depends(get_current_user)):
    from broker_intel import execution_matrix
    return await execution_matrix(get_db(), user["id"], days=days)


@router.get("/execution-alpha/recent")
async def execution_alpha_recent_ep(limit: int = 20,
                                    user=Depends(get_current_user)):
    db = get_db()
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    lim = max(1, min(int(limit), 100))
    return {"decisions": [d async for d in db.execution_alpha_decisions
            .find(q, {"_id": 0}).sort("at", -1).limit(lim)]}


@router.get("/twin/recent")
async def twin_recent_ep(limit: int = 20,
                         user=Depends(get_current_user)):
    db = get_db()
    q = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    lim = max(1, min(int(limit), 100))
    return {"verdicts": [d async for d in db.pretrade_twin
            .find(q, {"_id": 0}).sort("at", -1).limit(lim)]}


# ───────────────── iter-211 — intelligence scopes & reporting ────────────

@router.get("/health")
async def intel_health_ep(scope: str = Query("global"),
                          account_id: str | None = None,
                          user=Depends(get_current_user)):
    """Intelligence health at global / regional / account scope."""
    from intel_scopes import account_health, global_health, regional_health
    db = get_db()
    if scope == "account":
        if not account_id:
            raise HTTPException(status_code=400,
                                detail="account_id required for "
                                       "scope=account")
        acc = await _owned_account(db, user, account_id)
        return await account_health(db, acc)
    if scope == "regional":
        uid = None if user.get("role") == "admin" else user["id"]
        return await regional_health(db, user_id=uid)
    if scope != "global":
        raise HTTPException(status_code=400,
                            detail="scope must be global|regional|account")
    return await global_health(db, admin=user.get("role") == "admin")


@router.get("/report")
async def trade_intelligence_report_ep(days: int = Query(30, ge=1, le=90),
                                       user=Depends(get_current_user)):
    """Unified Trade Intelligence Report — decisions → execution →
    outcomes → learning health for the calling user."""
    from trade_intelligence import report
    return await report(get_db(), user["id"], days=days)


@router.get("/interventions")
async def interventions_ep(days: int = Query(30, ge=1, le=90),
                           user=Depends(get_current_user)):
    from intervention_metrics import effectiveness
    return await effectiveness(get_db(), user["id"], days=days)


@router.get("/coverage")
async def conformal_coverage_ep(symbol: str | None = None,
                                scope: str | None = None,
                                user=Depends(get_current_user)):
    """Realized conformal coverage — overall + segmented by
    strategy/symbol/session/regime (iter-212)."""
    from uncertainty_engine import (_sample_r, coverage_segments,
                                    realized_coverage)
    db = get_db()
    rs = await _sample_r(db, user["id"], scope, symbol)
    if len(rs) < 20:
        overall = {"evaluated": 0, "coverage": None, "ok": True,
                   "note": f"only {len(rs)} comparable trades"}
    else:
        overall = realized_coverage(list(reversed(rs)))
    segments = await coverage_segments(db, user["id"])
    return {**overall, "segments": segments}


@router.get("/value-ledger")
async def value_ledger_ep(days: int = Query(30, ge=1, le=90),
                          user=Depends(get_current_user)):
    """AI Value Ledger — observed vs estimated vs unobservable effects."""
    from value_ledger import ledger
    return await ledger(get_db(), user["id"], days=days)
