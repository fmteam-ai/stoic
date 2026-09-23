"""Pre-promotion evidence bundle (audit round 7 P1) — ONE signed, build-bound,
tenant-scoped document proving the production trading truth for the
candidate build. Read-only. Exit 0 only when EVERY gate passes.

  docker compose exec -T -e GIT_SHA=<sha> backend python ops/prepromotion_evidence.py \
      --scope-user <tenant user id> --expect 6/3/3 [--expect-ids a,b,c]

Gates (any failure → FAIL): signed topology reconciliation (production_reconcile
--strict), all enabled accounts LIVE, synthetic accounts enumerated+excluded,
EA heartbeat consensus (fresh, verified identity, one EA version, worker view
agrees), zero unresolved executions (UNKNOWN or aged) and zero position
mismatches, canonical authority FULL and UI/enforced levels agree per account,
Bot Health hard-cap inputs clear, performance attestation gate clear,
known GIT_SHA, dedicated signing key present.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:  # noqa: BLE001
    pass

FRESH_S = 600


def _h(v) -> str:
    return hashlib.sha256(str(v).encode()).hexdigest()[:16]


def _age(hb, now):
    try:
        return (now - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def preflight(args, env: dict) -> list:
    p = []
    if not args.scope_user:
        p.append("--scope-user (production tenant) is required")
    if not args.expect:
        p.append("--expect N/N/N (approved topology) is required")
    if (env.get("GIT_SHA") or "unknown") == "unknown":
        p.append("GIT_SHA unknown — evidence must be bound to the exact build")
    if not env.get("LEDGER_ANCHOR_KEY"):
        p.append("LEDGER_ANCHOR_KEY absent — dedicated signing key required")
    return p


async def collect(args) -> dict:
    from database import get_db
    from broker_env import broker_environment
    from synthetic_data import is_synthetic_account
    from execution_truth import execution_truth_check
    from trading_authority import compute_authority
    from state_contract import contract
    from routes.performance_routes import _attestation_gate
    db = get_db()
    now = datetime.now(timezone.utc)
    gates: dict = {}

    # 1 · signed topology reconciliation (same script the deploy gate runs)
    cmd = [sys.executable, os.path.join(HERE, "production_reconcile.py"), "--expect", args.expect, "--scope-user", args.scope_user, "--strict"]
    if args.expect_ids:
        cmd += ["--expect-ids", args.expect_ids]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env={**os.environ, "APP_ENV": os.environ.get("APP_ENV", "production")})
    try:
        recon = json.loads(r.stdout)
    except ValueError:
        recon = {"result": "ERROR", "stderr": r.stderr[-500:]}
    gates["topology_reconciliation"] = recon.get("result") == "PASS" and bool(recon.get("signature"))
    totals = recon.get("totals") or {}
    enabled_ids = totals.get("enabled_ids") or []

    # 2 · accounts: environments, synthetic exclusion, EA consensus
    accs = await db.accounts.find({"user_id": args.scope_user, "status": {"$ne": "deleted"}}).to_list(length=1000)
    synthetic = [{"id_hash": _h(a["_id"]), "label": a.get("label")} for a in accs if is_synthetic_account(a)]
    real = [a for a in accs if not is_synthetic_account(a)]
    enabled = [a for a in real if a.get("trading_enabled") is True]
    bots = {b["account_id"] for b in await db.bot_configs.find({"enabled": True, "user_id": args.scope_user}, {"account_id": 1}).to_list(length=100)}
    acct_rows = []
    for a in real:
        age = _age(a.get("last_heartbeat"), now)
        acct_rows.append({"id_hash": _h(a["_id"]), "enabled": a.get("trading_enabled") is True, "environment": broker_environment(a),
                          "bot_enabled": str(a["_id"]) in bots, "heartbeat_age_s": None if age is None else round(age),
                          "ea_fresh": age is not None and age <= FRESH_S, "ea_version": a.get("ea_version"),
                          "identity_verified": bool(a.get("verified_identity")), "open_positions_broker": a.get("open_positions")})
    en_rows = [x for x in acct_rows if x["enabled"]]
    gates["all_enabled_live"] = all(x["environment"] == "LIVE" for x in en_rows) and bool(en_rows)
    gates["ea_heartbeat_fresh_all_enabled"] = all(x["ea_fresh"] for x in en_rows)
    gates["ea_identity_verified_all_enabled"] = all(x["identity_verified"] for x in en_rows)
    gates["single_ea_version"] = len({x["ea_version"] for x in en_rows}) <= 1
    gates["bot_map_one_to_one"] = sorted(str(a["_id"]) for a in enabled) == sorted(b for b in bots if b in {str(a["_id"]) for a in real})
    ctr = await contract(db, args.scope_user)
    worker_view = ctr.get("totals") or {}
    gates["ea_count_agrees_with_worker_view"] = worker_view.get("eas_connected") == sum(1 for x in en_rows if x["ea_fresh"]) \
        and worker_view.get("accounts_enabled") == len(en_rows)

    # 3 · execution truth (exact) + position truth
    et = await execution_truth_check(db, account_ids=[str(a["_id"]) for a in enabled] or None)
    gates["zero_unresolved_executions"] = et["unresolved_executions"] == 0
    gates["zero_position_mismatches"] = et["position_mismatches"] == 0
    gates["no_missing_broker_snapshot"] = all(x["open_positions_broker"] is not None for x in en_rows)

    # 4 · canonical authority: platform FULL, per-account UI/enforced agreement
    plat = await compute_authority(db, None)
    per_acct = []
    for a in enabled:
        snap = await compute_authority(db, a)
        per_acct.append({"id_hash": _h(a["_id"]), "level": snap.get("level"), "enforced_level": snap.get("enforced_level"),
                         "hard_truth_fresh": snap.get("hard_truth_fresh"),
                         "domains": {k: v.get("level") for k, v in (snap.get("domains") or {}).items()}})
    gates["platform_authority_full"] = plat.get("level") == "FULL"
    gates["authority_ui_enforced_agree"] = all(p["level"] == p["enforced_level"] for p in per_acct)
    gates["hard_truth_fresh_all_enabled"] = all(p["hard_truth_fresh"] for p in per_acct) if per_acct else False

    # 5 · Bot Health hard-cap inputs (same sources the score uses)
    rows = ctr.get("rows") or ctr.get("accounts") or []
    cap_inputs = {"panic": [r.get("account_id") for r in rows if r.get("effective_state") == "PANIC"],
                  "blocked_or_disconnected": [r.get("account_id") for r in rows if r.get("effective_state") in ("BLOCKED", "DISCONNECTED") and r.get("bot_enabled")],
                  "position_truth_not_fresh": [r.get("account_id") for r in rows if r.get("position_truth") not in (None, "FRESH") and r.get("bot_enabled")]}
    gates["bot_health_caps_clear"] = not any(cap_inputs.values())

    # 6 · performance attestation gate (reconciled P&L, LIVE only, nothing synthetic)
    perf_reasons = await _attestation_gate(db, args.scope_user)
    gates["performance_reconciled_attestable"] = not perf_reasons

    body = {"bundle": "pre-promotion-evidence", "read_only": True, "at": now.isoformat(),
            "build": os.environ.get("GIT_SHA"), "tenant_hash": _h(args.scope_user), "expected_topology": args.expect,
            "accounts": acct_rows, "enabled_id_hashes": sorted(_h(i) for i in enabled_ids), "synthetic_excluded": synthetic,
            "topology_reconciliation": {k: recon.get(k) for k in ("result", "checks", "checks_ids", "failed_checks", "signature", "evidence_hash", "build")},
            "execution_truth": {k: et.get(k) for k in ("unresolved_executions", "unresolved_by_status", "unresolved_truncated",
                                                       "newest_unknown_age_s", "position_mismatches", "authority", "authority_reason", "sla_s", "checked_at")},
            "authority": {"platform": {"level": plat.get("level"), "enforced_level": plat.get("enforced_level"), "reason": plat.get("reason")},
                          "per_account": per_acct},
            "worker_view": worker_view, "bot_health_cap_inputs": cap_inputs, "performance_gate_reasons": perf_reasons,
            "gates": gates, "failed_gates": sorted(k for k, v in gates.items() if not v)}
    body["result"] = "PASS" if not body["failed_gates"] else "FAIL"
    return body


def sign(body: dict, key: str) -> dict:
    payload = json.dumps(body, sort_keys=True, default=str, separators=(",", ":")).encode()
    body["evidence_hash"] = hashlib.sha256(payload).hexdigest()
    body["signature"] = hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()
    body["signature_key"] = "LEDGER_ANCHOR_KEY"
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope-user", default=os.environ.get("RECONCILE_SCOPE_USER_ID"))
    ap.add_argument("--expect", default=os.environ.get("RECONCILE_EXPECT"))
    ap.add_argument("--expect-ids")
    a = ap.parse_args()
    problems = preflight(a, dict(os.environ))
    if problems:
        print(json.dumps({"bundle": "pre-promotion-evidence", "result": "REFUSED", "problems": problems}, indent=1))
        print("PREPROMOTION REFUSED: " + " | ".join(problems), file=sys.stderr)
        return 2
    body = sign(asyncio.run(collect(a)), os.environ["LEDGER_ANCHOR_KEY"])
    out_dir = os.environ.get("DRILL_EVIDENCE_DIR", "/tmp/drills")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"prepromotion-{body['build']}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        json.dump(body, open(path, "w"), indent=1, default=str)
        body["evidence_file"] = path
    except OSError:
        pass
    print(json.dumps(body, indent=1, default=str))
    print(f"PREPROMOTION {body['result']} failed_gates={body['failed_gates']}", file=sys.stderr)
    return 0 if body["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
