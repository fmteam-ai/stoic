"""P1-4 / round 6 P1 — READ-ONLY production reconciliation: prints and
SIGNS the actual account/bot/EA truth so the release decision can attach it.

  docker compose exec -T backend python ops/production_reconcile.py \
      --expect 6/3/3 --scope-user <tenant user id> [--expect-ids a,b,c]

Hard requirements (enforced when APP_ENV=production or --strict):
  * explicit tenant scope (--scope-user / RECONCILE_SCOPE_USER_ID) — never
    "all accounts in the database";
  * synthetic / test / deleted accounts excluded via the canonical
    classifier (synthetic_data.is_synthetic_account) and REPORTED;
  * GIT_SHA must be known (never "unknown");
  * a DEDICATED signing key (LEDGER_ANCHOR_KEY) — no JWT_SECRET fallback,
    unsigned evidence exits non-zero;
  * --expect N/N/N validated by format; --expect-ids requires the EXACT
    identity set (enabled == bots == fresh EA == expected ids).
Never writes to the database. Exit 0 = every gate PASSED.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:  # noqa: BLE001
    pass
from secrets_loader import resolve_file_secrets  # noqa: E402 — Docker-secret deployments (*_FILE → value)
resolve_file_secrets()

FRESH_S = 600
EXPECT_RE = re.compile(r"^\d{1,4}/\d{1,4}/\d{1,4}$")


def _age(hb, now):
    try:
        return (now - datetime.fromisoformat(str(hb).replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def preflight(args, env: dict) -> list:
    """Pure gate evaluation (unit-testable): returns the list of violations."""
    strict = args.strict or env.get("APP_ENV", "").lower() == "production"
    problems = []
    if args.expect and not EXPECT_RE.match(args.expect):
        problems.append(f"--expect '{args.expect}' malformed (want accounts/enabled/bots e.g. 6/3/3)")
    if strict:
        if not args.scope_user:
            problems.append("production scope required: --scope-user <tenant user id> (or RECONCILE_SCOPE_USER_ID)")
        if not args.expect:
            problems.append("production requires --expect N/N/N (the approved topology policy)")
        if (env.get("GIT_SHA") or "unknown") == "unknown":
            problems.append("GIT_SHA unknown — evidence must be bound to the exact release build")
        if not env.get("LEDGER_ANCHOR_KEY"):
            problems.append("LEDGER_ANCHOR_KEY absent — dedicated evidence signing key is mandatory (no JWT_SECRET fallback)")
    return problems


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", help="accounts/enabled/bots e.g. 6/3/3")
    ap.add_argument("--expect-ids", help="comma-separated EXACT set of enabled account ids")
    ap.add_argument("--scope-user", default=os.environ.get("RECONCILE_SCOPE_USER_ID"),
                    help="tenant/portfolio owner user id (mandatory in production)")
    ap.add_argument("--user-id", dest="scope_user_legacy", help=argparse.SUPPRESS)
    ap.add_argument("--strict", action="store_true", help="apply production gates regardless of APP_ENV")
    a = ap.parse_args()
    if a.scope_user_legacy and not a.scope_user:
        a.scope_user = a.scope_user_legacy
    strict = a.strict or os.environ.get("APP_ENV", "").lower() == "production"
    problems = preflight(a, dict(os.environ))
    if problems:
        print(json.dumps({"report": "production-reconciliation", "result": "REFUSED", "problems": problems}, indent=1))
        print("RECONCILE REFUSED: " + " | ".join(problems), file=sys.stderr)
        return 2
    from database import get_db
    from broker_env import broker_environment
    from synthetic_data import is_synthetic_account
    db = get_db()
    now = datetime.now(timezone.utc)
    q = {"status": {"$ne": "deleted"}}
    if a.scope_user:
        q["user_id"] = a.scope_user
    raw = await db.accounts.find(q).to_list(length=20000)
    excluded = [{"id": str(x["_id"]), "label": x.get("label"), "reason": "synthetic_or_test_account"}
                for x in raw if is_synthetic_account(x)]
    accs = [x for x in raw if not is_synthetic_account(x)]
    bots = {b["account_id"]: b for b in await db.bot_configs.find({"active": True}, {"account_id": 1, "strategy": 1}).to_list(length=1000)}
    rows, enabled, bot_ids, fresh = [], set(), set(), set()
    for acc in accs:
        aid = str(acc["_id"])
        flag = acc.get("trading_enabled")
        age = _age(acc.get("last_heartbeat"), now)
        vi = acc.get("verified_identity") or {}
        row = {"id": aid, "label": acc.get("label"), "user_id": acc.get("user_id"),
               "environment": broker_environment(acc), "trading_enabled": flag,
               "trading_enabled_is_bool": isinstance(flag, bool), "explicitly_enabled": flag is True,
               "verified_identity": {"account_number": vi.get("account_number"), "broker_server": vi.get("broker_server")},
               "ea_version": acc.get("ea_version"), "policy_version": acc.get("policy_version"),
               "heartbeat_age_s": None if age is None else round(age), "ea_fresh": age is not None and age <= FRESH_S,
               "bot_enabled": aid in bots, "bot_strategy": (bots.get(aid) or {}).get("strategy")}
        rows.append(row)
        if flag is True:
            enabled.add(aid)
            if aid in bots:
                bot_ids.add(aid)
            if row["ea_fresh"]:
                fresh.add(aid)
    totals = {"accounts": len(rows), "explicitly_enabled": len(enabled),
              "bots_enabled_on_enabled_accounts": len(bot_ids), "fresh_ea_on_enabled": len(fresh),
              "bots_on_disabled_accounts": sorted(set(bots) & {r["id"] for r in rows if not r["explicitly_enabled"]}),
              "non_boolean_flags": [r["id"] for r in rows if not r["trading_enabled_is_bool"]],
              "enabled_ids": sorted(enabled), "bot_ids": sorted(bot_ids), "fresh_ids": sorted(fresh),
              "sets_identical": enabled == bot_ids == fresh,
              "environments_enabled": sorted({r["environment"] for r in rows if r["explicitly_enabled"]}),
              "excluded_synthetic": len(excluded)}
    body = {"report": "production-reconciliation", "read_only": True, "at": now.isoformat(),
            "build": os.environ.get("GIT_SHA") or "unknown", "strict": strict,
            "scope": {"user_id": a.scope_user} if a.scope_user else "all non-deleted accounts (NOT production-grade)",
            "excluded": excluded, "accounts": rows, "totals": totals}
    ok, fails = True, []
    if a.expect:
        n_acc, n_en, n_bot = (int(x) for x in a.expect.split("/"))
        checks = {"accounts": totals["accounts"] == n_acc, "explicitly_enabled": totals["explicitly_enabled"] == n_en,
                  "bots_enabled_on_enabled_accounts": totals["bots_enabled_on_enabled_accounts"] == n_bot,
                  "no_bots_on_disabled_accounts": not totals["bots_on_disabled_accounts"],
                  "all_flags_boolean": not totals["non_boolean_flags"], "sets_identical": totals["sets_identical"]}
        if strict:
            checks["enabled_environments_all_live"] = totals["environments_enabled"] in ([], ["LIVE"])
        body["expected"], body["checks"] = a.expect, checks
        fails = [k for k, v in checks.items() if not v]
        ok = not fails
    if a.expect_ids:
        want = sorted({x.strip() for x in a.expect_ids.split(",") if x.strip()})
        body["expected_ids"] = want
        body["checks_ids"] = {"enabled_ids_exact": sorted(enabled) == want, "bot_ids_exact": sorted(bot_ids) == want,
                              "fresh_ids_exact": sorted(fresh) == want}
        idf = [k for k, v in body["checks_ids"].items() if not v]
        fails += idf
        ok = ok and not idf
    body["result"] = ("PASS" if ok else "FAIL") if (a.expect or a.expect_ids) else "REPORTED"
    body["failed_checks"] = fails
    key = os.environ.get("LEDGER_ANCHOR_KEY") or (None if strict else os.environ.get("JWT_SECRET"))
    payload = json.dumps(body, sort_keys=True, default=str, separators=(",", ":")).encode()
    body["evidence_hash"] = hashlib.sha256(payload).hexdigest()
    body["signature"] = hmac.new(key.encode(), payload, hashlib.sha256).hexdigest() if key else None
    body["signature_key"] = "LEDGER_ANCHOR_KEY" if os.environ.get("LEDGER_ANCHOR_KEY") else ("JWT_SECRET (non-production fallback)" if key else None)
    if body["signature"] is None:
        ok = False
        body["result"] = "FAIL"
        body["failed_checks"].append("evidence_unsigned")
    out_dir = os.environ.get("DRILL_EVIDENCE_DIR", "/tmp/drills")
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"reconcile-{now.strftime('%Y%m%dT%H%M%SZ')}.json")
        json.dump(body, open(path, "w"), indent=1, default=str)
        body["evidence_file"] = path
    except OSError:
        pass
    print(json.dumps(body, indent=1, default=str))
    print(f"RECONCILE {body['result']} accounts={totals['accounts']} enabled={totals['explicitly_enabled']} "
          f"bots={totals['bots_enabled_on_enabled_accounts']} fresh={totals['fresh_ea_on_enabled']} "
          f"excluded_synthetic={len(excluded)} signed={body['signature'] is not None}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
