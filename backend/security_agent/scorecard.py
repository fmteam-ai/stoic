"""Observe scorecard — "would this rule have hit a real user?" per rule (R1–R8).

Every proposal the agent recorded in the window (status would_have_done in
observe mode, done / refused_* in enforce) is re-checked against what the
targeted principal actually did around that moment:

  block_ip (R1/R6/R8)      → a session was CREATED from that IP (successful login) or a
                             valid bridge heartbeat came from it within ±24 h → real user
  lock_login / lock_otp    → the account owner logged in successfully within ±24 h
  revoke_sessions / user   → always a real user (the owner loses every device)
  suspend_bridge_token     → the account kept heartbeating validly after the proposal
  freeze_new_entries       → the account placed bot trades within ±24 h

A rule is "safe to enforce" when it has enough proposals and none of them would
have hit a real user; "review" when at least one would have; "insufficient data"
otherwise. The verdicts are evidence for the operator, never an automatic switch.
"""
from datetime import datetime, timedelta, timezone

from security_agent import rules

MIN_PROPOSALS = 5
WINDOW_H = 24


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _ip_hit(db, ip: str, lo: str, hi: str) -> str | None:
    s = await db.auth_sessions.find_one({"ip": ip, "created_at": {"$gte": lo, "$lte": hi}}, {"user_id": 1})
    if s:
        return f"login session from {ip} (user {str(s.get('user_id'))[-6:]})"
    acc = await db.accounts.find_one({"hb_sightings": {"$elemMatch": {"ip": ip, "at": {"$gte": lo, "$lte": hi}}}}, {"label": 1})
    if acc:
        return f"valid EA heartbeat from {ip} (account {acc.get('label') or str(acc['_id'])[-6:]})"
    return None


async def _account_login_hit(db, email: str, lo: str, hi: str) -> str | None:
    u = await db.users.find_one({"email": (email or "").lower()}, {"_id": 1})
    if not u:
        return None
    s = await db.auth_sessions.find_one({"user_id": str(u["_id"]), "created_at": {"$gte": lo, "$lte": hi}}, {"ip": 1})
    return f"owner of {email} logged in successfully (ip {s.get('ip')})" if s else None


async def _token_hit(db, account_id: str, at: str, hi: str) -> str | None:
    acc = await db.accounts.find_one({"_id": _oid(account_id), "hb_sightings": {"$elemMatch": {"at": {"$gte": at, "$lte": hi}}}}, {"label": 1})
    return f"account {acc.get('label') or account_id[-6:]} kept heartbeating with the valid token" if acc else None


async def _freeze_hit(db, account_id: str, lo: str, hi: str) -> str | None:
    t = await db.trades.find_one({"account_id": str(account_id), "origin": "auto", "opened_at": {"$gte": lo, "$lte": hi}}, {"symbol": 1})
    return f"bot opened {t.get('symbol')} on this account in the window" if t else None


def _oid(v):
    from bson import ObjectId
    try:
        return ObjectId(str(v))
    except Exception:  # noqa: BLE001
        return v


async def real_user_hit(db, action_row: dict) -> str | None:
    """Evidence string when the proposal would have hit a legitimate user, else None."""
    at = _parse(action_row.get("at")) or datetime.now(timezone.utc)
    lo, hi = _iso(at - timedelta(hours=WINDOW_H)), _iso(at + timedelta(hours=WINDOW_H))
    act, kind, target = action_row.get("action"), action_row.get("target_kind"), str(action_row.get("target") or "")
    if act == "block_ip":
        return await _ip_hit(db, target, lo, hi)
    if act in ("lock_login", "lock_otp"):
        return await _account_login_hit(db, target, lo, hi)
    if act == "revoke_sessions" or kind == "user":
        return "every device of this user would have been signed out"
    if act == "suspend_bridge_token":
        return await _token_hit(db, target, _iso(at), hi)
    if act == "freeze_new_entries":
        return await _freeze_hit(db, target, lo, hi)
    return None


async def build(db, cfg: dict, days: int = 14) -> dict:
    since = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    rows = await db.security_actions.find({"kind": "containment", "at": {"$gte": since}}).sort("at", -1).limit(2000).to_list(length=2000)
    per: dict = {r: {"rule": r, "title": rules.RULES[r]["title"], "action": rules.RULES[r]["action"],
                     "enabled": r in (cfg.get("rules_enabled") or []),
                     "proposals": 0, "would_have_done": 0, "done": 0, "refused": 0,
                     "real_user_hits": 0, "examples": [], "targets": set()} for r in rules.RULES}
    for a in rows:
        r = per.get(a.get("rule"))
        if not r:
            continue
        r["proposals"] += 1
        st = str(a.get("status") or "")
        if st == "would_have_done":
            r["would_have_done"] += 1
        elif st in ("done", "expired", "undone"):
            r["done"] += 1
        elif st.startswith("refused"):
            r["refused"] += 1
        r["targets"].add(str(a.get("target")))
        hit = await real_user_hit(db, a)
        if hit:
            r["real_user_hits"] += 1
            if len(r["examples"]) < 5:
                r["examples"].append({"at": a.get("at"), "target": a.get("target"), "status": st, "evidence": hit})
    out = []
    for r in per.values():
        n, hits = r["proposals"], r["real_user_hits"]
        if n < MIN_PROPOSALS:
            verdict, why = "insufficient_data", f"{n} proposal(s) in {days} d — need ≥ {MIN_PROPOSALS}"
        elif hits == 0:
            verdict, why = "safe_to_enforce", f"{n} proposals, none would have hit a real user"
        else:
            verdict, why = "review", f"{hits} of {n} proposals would have hit a real user"
        r.update({"verdict": verdict, "why": why, "distinct_targets": len(r.pop("targets")),
                  "false_positive_rate": round(hits / n, 3) if n else None})
        out.append(r)
    return {"days": days, "since": since, "mode": cfg.get("mode"), "min_proposals": MIN_PROPOSALS,
            "window_h": WINDOW_H, "rules": out,
            "built_at": _iso(datetime.now(timezone.utc))}
