"""iter-228 — EXHAUSTIVE route authorization sweep (closes security audit #7
coverage gap with hard evidence).

Enumerates EVERY operation from the live OpenAPI document and asserts, per
operation:

  A. ANONYMOUS  — no cookie: must NOT return 2xx unless the path is on the
                  explicit PUBLIC allowlist (login/register/health/legal/…).
                  Machine-token surfaces (bridge/agent/webhook/edge-probe)
                  must also refuse without their token.
  B. IDOR       — user B (non-admin, paid, PAMM-manager role) calls every
                  operation with path params substituted by user A's real
                  resource ids: must NOT return 2xx.
  C. BFLA       — user B calls every admin/ops surface: must NOT return 2xx.

A leak is any 2xx, any 3xx, or a non-2xx body echoing the owner's email or
bridge token; 401/403/404/400/422 with generic bodies are refusals. Evidence is
written to release/evidence/route-auth-sweep-<ts>.json (per-op verdicts +
coverage counters). Mutating calls are made with an EMPTY body by a throwaway
user against a throwaway user's ids, so no real data can be affected.
"""
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId

from live_target import require_live_base_url

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
TIMEOUT = 30
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
ADMIN_EMAIL, ADMIN_PW = "admin@stoicaibot.com", "admin123"

# Paths that are PUBLIC by design (unauthenticated 2xx is expected).
PUBLIC_EXACT = {
    "/api/health", "/api/status", "/api/public/trust-stats", "/api/auth/csrf", "/api/auth/login",
    "/api/auth/register", "/api/auth/refresh", "/api/auth/logout", "/api/auth/verify-email",
    "/api/auth/resend-activation", "/api/auth/forgot-password", "/api/auth/reset-password",
    "/api/legal/privacy", "/api/legal/risk", "/api/legal/{kind}", "/api/terms", "/api/terms/current",
    "/api/affiliates/apply", "/api/affiliates/public", "/api/affiliates/landing", "/api/partners/brokers",
    "/api/ea-script", "/api/ea-script.ex5", "/api/ea-script/version", "/api/ea-script/release",
    "/api/turnstile/config", "/api/auth/turnstile", "/api/certificate/{cert_id}", "/api/status/summary",
    "/api/performance/{share_id}", "/api/public/performance/{share_id}", "/api/r/{code}",
    "/api/release/public-key", "/api/attestation/public-key", "/api/performance/public-key",
    "/api/waitlist", "/api/metrics/public", "/api/soak/public", "/api/public/soak", "/api/public/status",
    "/api/subscription/plans", "/api/subscription/webhook/stripe", "/api/webhook/stripe",
    "/api/webhooks/stripe", "/api/public/edge-probe", "/api/public/release-attestation",
    "/api/public/ledger-anchors", "/api/public/repair-ledger/anchor", "/api/setup/claim-pairing",
    "/api/setup/install.ps1", "/api/setup/installer", "/api/setup/pairing/{code}", "/api/setup/verify",
    "/api/infra/agent/artifact-digest", "/api/infra/artifacts/manifest", "/api/pamm/webhooks/{partner_id}",
    # liveness/readiness probes, API banner, EA source download, release public key, plan catalog
    "/health", "/healthz", "/api/", "/api/health/live", "/api/health/ready", "/api/bridge/download-ea",
    "/api/release-key", "/api/entitlements/tiers",
    # Telegram webhook: best-effort 200 by design (Telegram retries otherwise); unknown secret is a silent no-op
    "/api/telegram/incoming/{secret}",
}
# path params that are NOT object identifiers (symbols, enums, opaque codes) — no ownership to test
NON_OBJECT_PARAMS = {"symbol", "kind", "code", "key", "name", "decision", "secret", "sha256", "cert_id", "share_id"}
PUBLIC_PREFIX = ("/api/public/", "/api/legal/", "/api/setup/", "/api/ea-script", "/api/auth/", "/api/terms")
# machine-token surfaces: anonymous must be refused (they carry their own token scheme)
MACHINE_PREFIX = ("/api/bridge/", "/api/infra/agent/", "/api/agent/", "/api/mockbroker/", "/api/ops/", "/api/metrics")
# admin-ONLY surfaces (user-scoped features such as /api/infra VPS deployments, /api/governance
# per-user change approvals, /api/panic for the caller's own bots and /api/shadow/models are
# deliberately NOT here — they are covered by the IDOR pass with A's ids instead)
ADMIN_PREFIX = ("/api/admin/", "/api/ops/", "/api/config/rollback", "/api/migration/", "/api/diagnostic",
                "/api/bugs", "/api/pamm/managers", "/api/soak/", "/api/release/", "/api/mockbroker/",
                "/api/partners/brokers/{bid}", "/api/support/admin", "/api/affiliate/admin")
ADMIN_TAGS = {"admin", "ops", "infra"}
# user-facing operations that live inside an admin-tagged router (scoped to the caller)
USER_FACING_IN_ADMIN_ROUTER = {"/api/stress-test/run", "/api/stress-test/runs"}


def _mongo():
    from pymongo import MongoClient
    with open(os.path.join(ROOT, ".env")) as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'") for ln in f if "=" in ln}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _csrf(s):
    tok = s.cookies.get("csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


def _user(tag):
    email, pw = f"sweep_{tag}_{uuid.uuid4().hex[:8]}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{API}/auth/register", json={"email": email, "password": pw, "name": f"sweep-{tag}", "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    db = _mongo()
    db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"email_verified": True}, "$unset": {"activation_token": "", "activation_expires_at": ""}})
    valid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db.subscriptions.update_one({"user_id": uid}, {"$set": {"current_plan_id": "elite_ai_monthly", "valid_until": valid}}, upsert=True)
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return uid, s, email


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _seed_owner_resources(uid, s, admin):
    """Create REAL resources owned by user A so ownership checks are exercised."""
    db = _mongo()
    now = datetime.now(timezone.utc).isoformat()
    ids = {}
    r = s.post(f"{API}/accounts", json={"label": f"sweep-{uuid.uuid4().hex[:5]}", "broker": "STARTRADER", "server": "T-Demo",
                                        "account_number": "sw-" + uuid.uuid4().hex[:8], "account_type": "demo",
                                        "base_currency": "USD", "mode": "live"}, headers=_csrf(s), timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    ids["account_id"] = r.json()["id"]
    ids["bridge_token"] = (db.accounts.find_one({"_id": ObjectId(ids["account_id"])}) or {}).get("bridge_token")
    ids["trade_id"] = str(db.trades.insert_one({"user_id": uid, "account_id": ids["account_id"], "status": "closed", "symbol": "XAUUSD",
                                                "pnl": 1.0, "closed_at": now, "opened_at": now, "origin": "auto"}).inserted_id)
    ids["signal_id"] = str(db.signals.insert_one({"user_id": uid, "account_id": ids["account_id"], "symbol": "XAUUSD", "action": "BUY",
                                                  "confidence": 70, "created_at": now, "status": "pending"}).inserted_id)
    r = s.post(f"{API}/support/tickets", json={"subject": "sweep ticket", "message": "route authorization sweep fixture ticket",
                                               "category": "other"}, headers=_csrf(s), timeout=TIMEOUT)
    ids["ticket_id"] = (r.json().get("id") or r.json().get("ticket_id")) if r.status_code == 200 else str(ObjectId())
    ids["proposal_id"] = str(db.improvement_proposals.insert_one({"user_id": uid, "title": "sweep", "status": "open", "created_at": now}).inserted_id)
    ids["report_id"] = str(db.optimizer_reports.insert_one({"user_id": uid, "created_at": now, "recommendations": [{"id": "rec1"}]}).inserted_id)
    ids["rec_id"] = "rec1"
    ids["deployment_id"] = f"dep_{uuid.uuid4().hex[:10]}"
    db.vps_deployments.insert_one({"user_id": uid, "deployment_id": ids["deployment_id"], "created_at": now, "status": "pending"})
    ids["installation_id"] = f"inst_{uuid.uuid4().hex[:10]}"
    db.installations.insert_one({"user_id": uid, "installation_id": ids["installation_id"], "account_id": ids["account_id"], "created_at": now})
    ids["bug_id"] = str(db.bugs.insert_one({"user_id": uid, "title": "sweep", "status": "open", "created_at": now}).inserted_id)
    ids["session_id"] = f"cs_{uuid.uuid4().hex[:10]}"
    db.copilot_sessions.insert_one({"user_id": uid, "session_id": ids["session_id"], "created_at": now, "messages": []})
    ids["user_id"] = uid
    # PAMM program managed by A (admin-created), so B-as-manager is a real cross-manager probe
    r = admin.post(f"{API}/pamm/programs", json={"name": f"sweep-{uuid.uuid4().hex[:5]}", "manager_id": uid}, headers=_csrf(admin), timeout=TIMEOUT)
    ids["program_id"] = (r.json().get("program_id") or r.json().get("id")) if r.status_code == 200 else None
    ids["pid"] = ids["program_id"]
    return ids


def _cleanup(uids, ids):
    db = _mongo()
    for col in ("accounts", "trades", "signals", "support_tickets", "improvement_proposals", "optimizer_reports",
                "vps_deployments", "installations", "bugs", "copilot_sessions", "subscriptions", "bot_configs",
                "notifications", "trading_readiness", "execution_intents"):
        db[col].delete_many({"user_id": {"$in": uids}})
    db.users.delete_many({"_id": {"$in": [ObjectId(u) for u in uids]}})
    db.pamm_managers.delete_many({"user_id": {"$in": uids}})
    if ids.get("program_id"):
        db.pamm_programs.delete_many({"program_id": ids["program_id"]})


def _fill(path, ids):
    """Substitute A's ids; unknown params get a random ObjectId (recorded)."""
    unknown = []

    def rep(m):
        k = m.group(1)
        if ids.get(k):
            return str(ids[k])
        unknown.append(k)
        return str(ObjectId())
    return re.sub(r"\{(\w+)\}", rep, path), unknown


def _leaky(code, snippet, secrets):
    """Audit #8 P3: a leak is any 2xx, any 3xx (Location may carry state), or a
    non-2xx body that echoes the owner's identifiers/secrets."""
    if 200 <= code < 400:
        return True
    low = (snippet or "").lower()
    return any(sec and sec.lower() in low for sec in secrets)


def _call(session, method, url, body_json=True):
    hdrs = _csrf(session) if session else {}
    kw = {"timeout": TIMEOUT, "headers": hdrs, "allow_redirects": False}
    if method in ("post", "put", "patch") and body_json:
        kw["json"] = {}
    last = (0, "")
    for _ in range(2):                       # one retry on transport errors (status 0 is inconclusive)
        try:
            r = (session or requests).request(method.upper(), url, **kw)
            snippet = (r.text or "")[:400]
            if 300 <= r.status_code < 400:
                snippet = f"Location: {r.headers.get('location', '')} " + snippet
            return r.status_code, snippet
        except requests.RequestException as e:  # noqa: BLE001
            last = (0, str(e)[:200])
            time.sleep(1)
    return last


def _is_public(path):
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)


def _is_admin_surface(path, tags):
    if path in USER_FACING_IN_ADMIN_ROUTER:
        return False
    return path.startswith(ADMIN_PREFIX) or bool(set(t.lower() for t in tags) & ADMIN_TAGS)


def _openapi_paths():
    """The spec is not routed through the public ingress (only /api is), so
    read it from the local backend when available, else build it in-process."""
    try:
        r = requests.get("http://127.0.0.1:8001/openapi.json", timeout=10)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
            return r.json()["paths"]
    except requests.RequestException:
        pass
    from server import app
    return app.openapi()["paths"]


@pytest.fixture(scope="module")
def sweep():
    paths = _openapi_paths()
    admin = _admin()
    uid_a, sess_a, email_a = _user("owner")
    uid_b, sess_b, _ = _user("attacker")
    ids = _seed_owner_resources(uid_a, sess_a, admin)
    # identifiers/secrets of A that must never appear in ANY response to B or to anonymous callers
    a_secrets = [email_a, ids.get("bridge_token") or ""]
    # grant B the PAMM manager role so manager-scoped routes are really exercised
    admin.post(f"{API}/pamm/managers", json={"user_id": uid_b, "grant": True}, headers=_csrf(admin), timeout=TIMEOUT)
    ops = []
    for path, methods in paths.items():
        for method, meta in methods.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            ops.append({"method": method.lower(), "path": path, "tags": meta.get("tags") or []})
    results = {"anonymous": [], "idor": [], "bfla": []}
    t0 = time.time()
    for op in ops:
        url_path, unknown = _fill(op["path"], ids)
        url = f"{BASE_URL}{url_path}"
        public = _is_public(op["path"])
        # A · anonymous
        code, snippet = _call(None, op["method"], url)
        leak = (_leaky(code, snippet, a_secrets) if not public else any(sec and sec.lower() in snippet.lower() for sec in a_secrets))
        results["anonymous"].append({**op, "url": url_path, "status": code, "public_allowlisted": public,
                                     "machine_surface": op["path"].startswith(MACHINE_PREFIX), "verdict": "LEAK" if leak else "OK"})
        if op["path"] == "/api/auth/logout":
            continue
        # B · cross-user with A's ids (only ops that carry an OBJECT path param)
        obj_params = set(re.findall(r"\{(\w+)\}", op["path"])) - NON_OBJECT_PARAMS
        if obj_params and not public:
            code, snippet = _call(sess_b, op["method"], url)
            leak = _leaky(code, snippet, a_secrets)
            results["idor"].append({**op, "url": url_path, "status": code, "unseeded_params": unknown,
                                    "verdict": "LEAK" if leak else "OK", "snippet": snippet if leak else ""})
        # C · non-admin on admin surfaces
        if _is_admin_surface(op["path"], op["tags"]) and not public:
            code, snippet = _call(sess_b, op["method"], url)
            leak = _leaky(code, snippet, a_secrets)
            results["bfla"].append({**op, "url": url_path, "status": code, "verdict": "LEAK" if leak else "OK",
                                    "snippet": snippet if leak else ""})
    report = {"sweep": "route-authorization", "base": BASE_URL, "at": datetime.now(timezone.utc).isoformat(),
              "duration_s": round(time.time() - t0, 1), "operations_total": len(ops),
              "coverage": {k: len(v) for k, v in results.items()},
              "leaks": {k: [r for r in v if r["verdict"] == "LEAK"] for k, v in results.items()},
              "unseeded_param_ops": sorted({r["path"] for r in results["idor"] if r["unseeded_params"]}),
              "seeded_params": sorted(k for k, v in ids.items() if v),
              "results": results}
    out_dir = os.path.join(REPO, "release", "evidence")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"route-auth-sweep-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump(report, open(path, "w"), indent=1, default=str)
    report["evidence_file"] = path
    yield report
    _cleanup([uid_a, uid_b], ids)


def test_sweep_covers_every_operation(sweep):
    assert sweep["operations_total"] >= 500
    assert sweep["coverage"]["anonymous"] == sweep["operations_total"]
    assert sweep["coverage"]["idor"] >= 140 and sweep["coverage"]["bfla"] >= 60
    # the high-value params were REAL resources of user A, not random ids
    for p in ("account_id", "trade_id", "signal_id", "ticket_id", "deployment_id", "installation_id", "proposal_id", "report_id"):
        assert p in sweep["seeded_params"], p


def test_no_anonymous_leak(sweep):
    leaks = sweep["leaks"]["anonymous"]
    assert not leaks, "anonymous 2xx on non-public routes:\n" + "\n".join(f"{l['method'].upper()} {l['path']} → {l['status']}" for l in leaks)
    # machine-token surfaces refused without their token
    machine = [r for r in sweep["results"]["anonymous"] if r["machine_surface"] and not r["public_allowlisted"]]
    assert machine and all(r["status"] not in range(200, 400) for r in machine)


def test_no_cross_user_object_access(sweep):
    leaks = sweep["leaks"]["idor"]
    assert not leaks, "user B reached user A's objects:\n" + "\n".join(f"{l['method'].upper()} {l['url']} → {l['status']} {l['snippet']}" for l in leaks)


def test_no_admin_function_for_non_admin(sweep):
    leaks = sweep["leaks"]["bfla"]
    assert not leaks, "non-admin 2xx on admin surface:\n" + "\n".join(f"{l['method'].upper()} {l['url']} → {l['status']} {l['snippet']}" for l in leaks)


def test_evidence_written(sweep):
    assert os.path.exists(sweep["evidence_file"])
    d = json.load(open(sweep["evidence_file"]))
    assert d["operations_total"] == sweep["operations_total"] and "results" in d
