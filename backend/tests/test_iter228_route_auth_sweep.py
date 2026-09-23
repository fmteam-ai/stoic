"""iter-228/230 — EXHAUSTIVE route authorization sweep with hard safety
boundaries (security audit #7 coverage gap; audit round 7 P1 corrections).

Safety boundary (evaluated BEFORE any login or mutation):
  * target must advertise a deployment-SIGNED environment marker
    (/api/health: environment ∈ staging|ephemeral-test|preview…, env_sig
    verified with LEDGER_ANCHOR_KEY) — production hostnames/DB names refused;
  * ALLOW_MUTATING_AUTH_SWEEP=true and a unique AUTH_SWEEP_RUN_TOKEN;
  * admin credentials from the environment only (never literals; known
    defaults refused).
Fixtures are namespaced by the run token, every created object is recorded
in a SIGNED manifest (release/evidence/route-auth-sweep-manifest-*.json) and
cleanup is idempotent from that manifest — also after a forced interruption.

Checks per operation (from the OpenAPI document, 600+ paths):
  A. ANONYMOUS  — 2xx/3xx only allowed for operations in the EXACT public
                  manifest (tests/public_routes_manifest.json), with the
                  expected status. No prefix exemptions.
  B. IDOR       — user B (non-admin, paid, PAMM-manager) with user A's REAL
                  seeded ids: never 2xx/3xx, never echoing A's identifiers.
  C. BFLA       — user B on admin surfaces: never 2xx/3xx.
Every sensitive parameterized operation must be seeded (or carry an explicit
release-blocking waiver) — unseeded sensitive routes FAIL.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId

from live_target import admin_credentials, require_live_base_url

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
TIMEOUT = 30
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)
from environment_marker import mutation_guard  # noqa: E402

PUBLIC_MANIFEST = json.load(open(os.path.join(ROOT, "tests", "public_routes_manifest.json")))["routes"]
PUBLIC = {(r["method"].upper(), r["path"]): set(r["expected_status"]) for r in PUBLIC_MANIFEST}
# machine-token surfaces: anonymous must be refused (they carry their own token scheme)
MACHINE_PREFIX = ("/api/bridge/", "/api/infra/agent/", "/api/agent/", "/api/mockbroker/", "/api/ops/", "/api/metrics")
ADMIN_PREFIX = ("/api/admin/", "/api/ops/", "/api/config/rollback", "/api/migration/", "/api/diagnostic",
                "/api/bugs", "/api/pamm/managers", "/api/soak/", "/api/release/", "/api/mockbroker/",
                "/api/partners/brokers/{bid}", "/api/support/admin", "/api/affiliate/admin")
ADMIN_TAGS = {"admin", "ops", "infra"}
USER_FACING_IN_ADMIN_ROUTER = {"/api/stress-test/run", "/api/stress-test/runs",
                               "/api/partners/brokers/{bid}/click"}      # click-tracker on the GLOBAL broker catalog
# params that are NOT user-owned object identifiers (symbols, enums, opaque codes, global catalog ids)
NON_OBJECT_PARAMS = {"symbol", "kind", "code", "key", "name", "decision", "secret", "sha256", "cert_id", "share_id", "scenario", "bid"}
SENSITIVE_PREFIX = ("/api/admin", "/api/ops", "/api/governance", "/api/execution", "/api/safety-blocks", "/api/infra",
                    "/api/shadow", "/api/brain", "/api/pamm", "/api/postmortem", "/api/api-keys", "/api/strategies",
                    "/api/nl", "/api/bot", "/api/partners", "/api/trace", "/api/repair-ledger", "/api/affiliate")
# explicit, release-blocking waivers: param → reason (must stay EMPTY for a release)
SENSITIVE_PARAM_WAIVERS: dict = {}


def _env_file():
    with open(os.path.join(ROOT, ".env")) as f:
        return {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'") for ln in f if "=" in ln}


def _mongo():
    from pymongo import MongoClient
    cfg = _env_file()
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _csrf(s):
    tok = s.cookies.get("csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


def _runner_env():
    env = dict(os.environ)
    for k, v in _env_file().items():                # preview: backend/.env is the deployment's env
        env.setdefault(k, v)
    return env


def preflight(base_url: str, env: dict) -> list:
    """Pre-login refusal. Pure given the health document (unit-testable)."""
    try:
        r = requests.get(f"{base_url}/api/health", timeout=15)
        marker = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        marker = {}
    problems = mutation_guard(base_url, marker, env, allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN")
    try:
        admin_credentials(strict=True)
    except RuntimeError as e:
        problems.append(str(e))
    return problems


class Manifest:
    """Signed record of every created/changed object → idempotent cleanup."""

    def __init__(self, run_token: str, key: str):
        self.run_token, self.key, self.items = run_token, key, []
        self.path = os.path.join(REPO, "release", "evidence", f"route-auth-sweep-manifest-{run_token[:12]}.json")

    def add(self, collection: str, query: dict, kind: str = "created"):
        self.items.append({"collection": collection, "query": {k: str(v) if isinstance(v, ObjectId) else v for k, v in query.items()},
                           "kind": kind, "at": datetime.now(timezone.utc).isoformat()})
        self.flush()

    def body(self):
        return {"manifest": "route-auth-sweep", "run_token": self.run_token, "base": BASE_URL, "items": self.items}

    def flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        body = self.body()
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        body["signature"] = hmac.new(self.key.encode(), payload, hashlib.sha256).hexdigest() if self.key else None
        body["evidence_hash"] = hashlib.sha256(payload).hexdigest()
        json.dump(body, open(self.path, "w"), indent=1)


def cleanup_from_manifest(path: str) -> int:
    """Idempotent: deletes every recorded object; safe to run repeatedly and
    after a forced interruption. Returns rows removed."""
    body = json.load(open(path))
    db = _mongo()
    removed = 0
    for it in body["items"]:
        q = dict(it["query"])
        if "_id" in q:
            try:
                q["_id"] = ObjectId(q["_id"])
            except Exception:  # noqa: BLE001
                pass
        removed += db[it["collection"]].delete_many(q).deleted_count
    body["cleaned_at"] = datetime.now(timezone.utc).isoformat()
    body["removed_last_run"] = removed
    json.dump(body, open(path, "w"), indent=1)
    return removed


def _user(tag, run_token, manifest):
    email, pw = f"sweep_{run_token[:8]}_{tag}_{uuid.uuid4().hex[:6]}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{API}/auth/register", json={"email": email, "password": pw, "name": f"sweep-{tag}", "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    manifest.add("users", {"_id": uid})
    manifest.add("subscriptions", {"user_id": uid})
    db = _mongo()
    db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"email_verified": True}, "$unset": {"activation_token": "", "activation_expires_at": ""}})
    valid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db.subscriptions.update_one({"user_id": uid}, {"$set": {"current_plan_id": "elite_ai_monthly", "valid_until": valid}}, upsert=True)
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return uid, s, email


def _admin():
    em, pw = admin_credentials(strict=True)
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": em, "password": pw}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _seed_owner_resources(uid, s, admin, run_token, manifest):
    """REAL resources owned by user A — every sensitive parameterized route
    gets a real foreign object, namespaced by the run token."""
    db = _mongo()
    now = datetime.now(timezone.utc).isoformat()
    ns = f"sweep-{run_token[:8]}"
    ids = {"user_id": uid}

    def ins(col, doc, param=None, id_field=None):
        oid = ObjectId()
        doc = {"_id": oid, "user_id": uid, "created_at": now, "sweep_run": run_token, **doc}
        if id_field:
            doc[id_field] = doc.get(id_field) or str(oid)
        db[col].insert_one(doc)
        manifest.add(col, {"_id": oid})
        if param:
            ids[param] = doc[id_field] if id_field else str(oid)
        return doc

    r = s.post(f"{API}/accounts", json={"label": f"{ns}-acct", "broker": "STARTRADER", "server": "T-Demo",
                                        "account_number": "sw-" + uuid.uuid4().hex[:8], "account_type": "demo",
                                        "base_currency": "USD", "mode": "live"}, headers=_csrf(s), timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    ids["account_id"] = r.json()["id"]
    manifest.add("accounts", {"_id": ids["account_id"]})
    manifest.add("bot_configs", {"user_id": uid}); manifest.add("trading_readiness", {"user_id": uid}); manifest.add("notifications", {"user_id": uid})
    ids["bridge_token"] = (db.accounts.find_one({"_id": ObjectId(ids["account_id"])}) or {}).get("bridge_token")
    acc = ids["account_id"]
    ins("trades", {"account_id": acc, "status": "closed", "symbol": "XAUUSD", "pnl": 1.0, "closed_at": now, "opened_at": now, "origin": "auto"}, "trade_id")
    ins("signals", {"account_id": acc, "symbol": "XAUUSD", "action": "BUY", "confidence": 70, "status": "pending"}, "signal_id")
    r = s.post(f"{API}/support/tickets", json={"subject": f"{ns} ticket", "message": "route authorization sweep fixture ticket", "category": "other"},
               headers=_csrf(s), timeout=TIMEOUT)
    ids["ticket_id"] = (r.json().get("id") or r.json().get("ticket_id")) if r.status_code == 200 else str(ObjectId())
    manifest.add("support_tickets", {"user_id": uid})
    ins("improvement_proposals", {"title": ns, "status": "open"}, "proposal_id")
    ins("optimizer_reports", {"recommendations": [{"id": "rec1"}]}, "report_id"); ids["rec_id"] = "rec1"
    ins("vps_deployments", {"status": "pending", "deployment_id": f"dep_{uuid.uuid4().hex[:10]}"}, "deployment_id", "deployment_id")
    ins("installations", {"account_id": acc, "installation_id": f"inst_{uuid.uuid4().hex[:10]}"}, "installation_id", "installation_id")
    ins("bugs", {"title": ns, "status": "open"}, "bug_id")
    ins("copilot_sessions", {"session_id": f"cs_{uuid.uuid4().hex[:10]}", "messages": []}, "session_id", "session_id")
    # audit round 7 — the previously UNSEEDED sensitive params
    ins("safety_blocks", {"account_id": acc, "reason": ns, "active": True}, "block_id")
    chg = ins("governed_changes", {"status": "pending", "kind": "risk_change", "change_id": None}, "change_id", "change_id")
    db.pamm_change_requests.insert_one({"_id": chg["_id"], "user_id": uid, "manager_id": uid, "status": "pending", "change_id": chg["change_id"], "sweep_run": run_token})
    manifest.add("pamm_change_requests", {"_id": chg["_id"]})
    ins("meta_decisions", {"account_id": acc, "decision": "hold"}, "decision_id", "decision_id")
    db.decision_contexts.insert_one({"user_id": uid, "decision_id": ids["decision_id"], "sweep_run": run_token}); manifest.add("decision_contexts", {"decision_id": ids["decision_id"]})
    ins("loss_reviews", {"account_id": acc, "guards": [{"guard_id": "g1"}], "guard_id": None}, "guard_id", "guard_id")
    ins("execution_intents", {"account_id": acc, "status": "filled", "intent_id": None, "symbol": "XAUUSD"}, "intent_id", "intent_id")
    ins("api_keys", {"key_id": None, "label": ns, "revoked": False}, "key_id", "key_id")
    ins("shadow_models", {"model_id": None, "status": "shadow"}, "model_id", "model_id")
    db.tuning_proposals.insert_one({"user_id": uid, "model_id": ids["model_id"], "status": "pending", "sweep_run": run_token}); manifest.add("tuning_proposals", {"model_id": ids["model_id"]})
    ins("bot_presets", {"preset_id": None, "name": ns}, "preset_id", "preset_id")
    ins("strategies", {"strategy_id": None, "name": ns}, "strategy_id", "strategy_id")
    ins("traces", {"trace_id": None}, "trace_id", "trace_id")
    ins("conditional_triggers", {"active": True, "account_id": acc}, "trigger_id")
    ins("vps_agents", {"agent_id": None, "deployment_id": ids["deployment_id"], "status": "online"}, "agent_id", "agent_id")
    ins("mt5_discovered", {"discovery_id": None, "deployment_id": ids["deployment_id"]}, "discovery_id", "discovery_id")
    ins("broker_installers", {"installer_id": None, "deployment_id": ids["deployment_id"], "status": "pending"}, "installer_id", "installer_id")
    aff = ins("affiliates", {"status": "pending", "code": f"SW{uuid.uuid4().hex[:6].upper()}"}, "aff_id")
    ids["app_id"] = ids["aff_id"]
    ins("affiliate_commissions", {"affiliate_id": ids["aff_id"], "amount": 1.0, "status": "pending"}, "cid")
    ins("affiliate_payout_requests", {"affiliate_id": ids["aff_id"], "amount": 1.0, "status": "requested"}, "rid")
    ins("ops_alerts", {"kind": "sweep_fixture", "severity": "info", "message": ns, "acked_at": None, "synthetic": True}, "alert_id")
    ins("repair_ledger", {"correlation_id": None, "action": "sweep_fixture", "seq": -1}, "correlation_id", "correlation_id")
    ins("pamm_join_requests", {"request_id": None, "status": "pending", "investor_id": uid}, "request_id", "request_id")
    ins("pamm_partners", {"partner_id": None, "name": ns}, "partner_id", "partner_id")
    ins("partner_brokers", {"bid": None, "name": ns}, "bid", "bid")
    ins("broker_registry", {"broker_id": None, "name": ns}, "broker_id", "broker_id")
    r = admin.post(f"{API}/pamm/programs", json={"name": f"{ns}-prog", "manager_id": uid}, headers=_csrf(admin), timeout=TIMEOUT)
    ids["program_id"] = (r.json().get("program_id") or r.json().get("id")) if r.status_code == 200 else None
    if ids["program_id"]:
        manifest.add("pamm_programs", {"program_id": ids["program_id"]})
    ids["pid"] = ids["program_id"]
    ids["decision"] = "approve"          # enum param: B trying to APPROVE A's PAMM requests must be refused
    return ids


def _fill(path, ids):
    unknown = []

    def rep(m):
        k = m.group(1)
        if ids.get(k):
            return str(ids[k])
        unknown.append(k)
        return str(ObjectId())
    return re.sub(r"\{(\w+)\}", rep, path), unknown


def _leaky(code, snippet, secrets):
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
    for _ in range(2):
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


def _is_admin_surface(path, tags):
    if path in USER_FACING_IN_ADMIN_ROUTER:
        return False
    return path.startswith(ADMIN_PREFIX) or bool(set(t.lower() for t in tags) & ADMIN_TAGS)


def _openapi_paths():
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
    env = _runner_env()
    env.setdefault("ALLOW_MUTATING_AUTH_SWEEP", "true")                 # preview runner; CI sets it explicitly
    env.setdefault("AUTH_SWEEP_RUN_TOKEN", f"run_{uuid.uuid4().hex}")
    problems = preflight(BASE_URL, env)
    assert not problems, "SWEEP REFUSED before login: " + " | ".join(problems)
    run_token = env["AUTH_SWEEP_RUN_TOKEN"]
    manifest = Manifest(run_token, env.get("LEDGER_ANCHOR_KEY", ""))
    paths = _openapi_paths()
    admin = _admin()
    uid_a, sess_a, email_a = _user("owner", run_token, manifest)
    uid_b, sess_b, _ = _user("attacker", run_token, manifest)
    ids = _seed_owner_resources(uid_a, sess_a, admin, run_token, manifest)
    admin.post(f"{API}/pamm/managers", json={"user_id": uid_b, "grant": True}, headers=_csrf(admin), timeout=TIMEOUT)
    manifest.add("pamm_managers", {"user_id": uid_b})
    a_secrets = [email_a, ids.get("bridge_token") or ""]
    ops = [{"method": m.lower(), "path": p, "tags": meta.get("tags") or []}
           for p, methods in paths.items() for m, meta in methods.items() if m.lower() in ("get", "post", "put", "patch", "delete")]
    results = {"anonymous": [], "idor": [], "bfla": []}
    unseeded_sensitive = []
    t0 = time.time()
    for op in ops:
        url_path, unknown = _fill(op["path"], ids)
        url = f"{BASE_URL}{url_path}"
        key = (op["method"].upper(), op["path"])
        public_expected = PUBLIC.get(key)
        code, snippet = _call(None, op["method"], url)
        if public_expected is not None:
            leak = (200 <= code < 400 and code not in public_expected) or any(s and s.lower() in snippet.lower() for s in a_secrets)
        else:
            leak = _leaky(code, snippet, a_secrets)
        results["anonymous"].append({**op, "url": url_path, "status": code, "public_manifest": public_expected is not None,
                                     "machine_surface": op["path"].startswith(MACHINE_PREFIX), "verdict": "LEAK" if leak else "OK",
                                     "snippet": snippet if leak else ""})
        if op["path"] == "/api/auth/logout":
            continue
        obj_params = set(re.findall(r"\{(\w+)\}", op["path"])) - NON_OBJECT_PARAMS
        if obj_params and public_expected is None:
            code, snippet = _call(sess_b, op["method"], url)
            leak = _leaky(code, snippet, a_secrets)
            results["idor"].append({**op, "url": url_path, "status": code, "unseeded_params": unknown,
                                    "verdict": "LEAK" if leak else "OK", "snippet": snippet if leak else ""})
            unknown_obj = [u for u in unknown if u not in NON_OBJECT_PARAMS]
            if unknown_obj and op["path"].startswith(SENSITIVE_PREFIX) and not all(u in SENSITIVE_PARAM_WAIVERS for u in unknown_obj):
                unseeded_sensitive.append({"op": f"{op['method'].upper()} {op['path']}", "params": unknown_obj})
        if _is_admin_surface(op["path"], op["tags"]) and public_expected is None:
            code, snippet = _call(sess_b, op["method"], url)
            leak = _leaky(code, snippet, a_secrets)
            results["bfla"].append({**op, "url": url_path, "status": code, "verdict": "LEAK" if leak else "OK", "snippet": snippet if leak else ""})
    report = {"sweep": "route-authorization", "base": BASE_URL, "run_token": run_token, "at": datetime.now(timezone.utc).isoformat(),
              "duration_s": round(time.time() - t0, 1), "operations_total": len(ops),
              "coverage": {k: len(v) for k, v in results.items()},
              "leaks": {k: [r for r in v if r["verdict"] == "LEAK"] for k, v in results.items()},
              "unseeded_sensitive": unseeded_sensitive,
              "unseeded_param_ops": sorted({r["path"] for r in results["idor"] if r["unseeded_params"]}),
              "seeded_params": sorted(k for k, v in ids.items() if v), "public_manifest_size": len(PUBLIC),
              "manifest_file": manifest.path, "results": results}
    out_dir = os.path.join(REPO, "release", "evidence")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"route-auth-sweep-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    json.dump(report, open(path, "w"), indent=1, default=str)
    report["evidence_file"] = path
    yield report
    removed = cleanup_from_manifest(manifest.path)
    report["cleanup_removed"] = removed
    assert cleanup_from_manifest(manifest.path) == 0          # idempotent: second pass finds nothing


def test_preflight_refuses_production_targets():
    """Acceptance: a production-tagged host / unsigned marker / missing flag is refused before login."""
    env = _runner_env()
    good_marker = requests.get(f"{BASE_URL}/api/health", timeout=15).json()
    ok_env = {**env, "ALLOW_MUTATING_AUTH_SWEEP": "true", "AUTH_SWEEP_RUN_TOKEN": "x" * 32}
    assert mutation_guard(BASE_URL, good_marker, ok_env, allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN") == []
    p = mutation_guard("https://app.stoicaibot.com", {**good_marker, "environment": "production"}, ok_env,
                       allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN")
    assert any("production hostname" in x for x in p) and any("environment='production'" in x for x in p)
    assert any("signature" in x for x in mutation_guard(BASE_URL, {**good_marker, "env_sig": "0" * 64}, ok_env,
                                                        allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN"))
    assert any("ALLOW_MUTATING_AUTH_SWEEP" in x for x in mutation_guard(BASE_URL, good_marker, {**ok_env, "ALLOW_MUTATING_AUTH_SWEEP": ""},
                                                                        allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN"))
    assert any("run token" in x for x in mutation_guard(BASE_URL, good_marker, {**ok_env, "AUTH_SWEEP_RUN_TOKEN": "short"},
                                                        allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN"))
    assert any("production database" in x for x in mutation_guard(BASE_URL, good_marker, {**ok_env, "DB_NAME": "stoic_prod"},
                                                                  allow_flag="ALLOW_MUTATING_AUTH_SWEEP", token_var="AUTH_SWEEP_RUN_TOKEN"))
    # missing credentials → refused pre-network by preflight()
    no_creds = {k: v for k, v in ok_env.items() if k not in ("TEST_ADMIN_EMAIL", "TEST_ADMIN_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD")}
    saved = {k: os.environ.pop(k) for k in ("TEST_ADMIN_EMAIL", "TEST_ADMIN_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD") if k in os.environ}
    try:
        assert any("not set" in x for x in preflight(BASE_URL, no_creds))
    finally:
        os.environ.update(saved)


def test_manifest_cleanup_survives_forced_interruption():
    """Acceptance: objects recorded before a 'crash' are removed by a later idempotent cleanup."""
    env = _runner_env()
    tok = f"crash_{uuid.uuid4().hex}"
    m = Manifest(tok, env.get("LEDGER_ANCHOR_KEY", ""))
    db = _mongo()
    oid = ObjectId()
    db.strategies.insert_one({"_id": oid, "user_id": tok, "name": "crash-fixture", "sweep_run": tok})
    m.add("strategies", {"_id": oid})
    body = json.load(open(m.path))
    assert body["signature"] and len(body["signature"]) == 64
    # simulate the interruption: no cleanup ran; a fresh process recovers from the manifest
    assert db.strategies.count_documents({"_id": oid}) == 1
    assert cleanup_from_manifest(m.path) == 1
    assert db.strategies.count_documents({"_id": oid}) == 0
    assert cleanup_from_manifest(m.path) == 0
    os.remove(m.path)


def test_sweep_covers_every_operation(sweep):
    assert sweep["operations_total"] >= 500
    assert sweep["coverage"]["anonymous"] == sweep["operations_total"]
    assert sweep["coverage"]["idor"] >= 140 and sweep["coverage"]["bfla"] >= 60
    for p in ("account_id", "trade_id", "signal_id", "ticket_id", "deployment_id", "installation_id", "proposal_id", "report_id",
              "block_id", "change_id", "intent_id", "key_id", "model_id", "agent_id", "alert_id", "strategy_id", "trigger_id"):
        assert p in sweep["seeded_params"], p


def test_no_unseeded_sensitive_parameterized_routes(sweep):
    assert not sweep["unseeded_sensitive"], "sensitive routes without a real foreign object (seed or waive explicitly):\n" + \
        "\n".join(f"{u['op']} params={u['params']}" for u in sweep["unseeded_sensitive"])
    assert SENSITIVE_PARAM_WAIVERS == {}, "release-blocking waivers present"


def test_no_anonymous_leak(sweep):
    leaks = sweep["leaks"]["anonymous"]
    assert not leaks, "anonymous 2xx/3xx outside the exact public manifest:\n" + "\n".join(
        f"{l['method'].upper()} {l['path']} → {l['status']} {l['snippet'][:80]}" for l in leaks)
    machine = [r for r in sweep["results"]["anonymous"] if r["machine_surface"] and not r["public_manifest"]]
    assert machine and all(r["status"] not in range(200, 400) for r in machine)


def test_no_cross_user_object_access(sweep):
    leaks = sweep["leaks"]["idor"]
    assert not leaks, "user B reached user A's objects:\n" + "\n".join(f"{l['method'].upper()} {l['url']} → {l['status']} {l['snippet']}" for l in leaks)


def test_no_admin_function_for_non_admin(sweep):
    leaks = sweep["leaks"]["bfla"]
    assert not leaks, "non-admin 2xx on admin surface:\n" + "\n".join(f"{l['method'].upper()} {l['url']} → {l['status']} {l['snippet']}" for l in leaks)


def test_evidence_and_signed_manifest_written(sweep):
    d = json.load(open(sweep["evidence_file"]))
    assert d["operations_total"] == sweep["operations_total"] and "results" in d
    m = json.load(open(sweep["manifest_file"]))
    assert m["run_token"] == sweep["run_token"] and len(m["items"]) >= 30 and m["signature"]
