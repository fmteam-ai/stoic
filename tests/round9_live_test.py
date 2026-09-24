"""
Round 9 live HTTP verification against preview URL.
Focus: turnstile config/break-glass, authority decision/inventory, register invariant,
release readiness, cleanup break-glass to safe state.
"""
import os
import time
import uuid
import json
import secrets
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL") or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].split()[0]
BASE = BASE.rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASS = "TUIofiuh5am4CEvlO1ICLTJAW2Zxc"

results = {"pass": [], "fail": []}

def rec(name, ok, detail=""):
    (results["pass"] if ok else results["fail"]).append(f"{name}: {detail}")
    print(f"{'PASS' if ok else 'FAIL'} - {name} :: {detail}")

s = requests.Session()

# --- 1. Public turnstile-config anonymous
r = requests.get(f"{BASE}/api/auth/turnstile-config", timeout=15)
try:
    body = r.json()
except Exception:
    body = {}
keys = set(body.keys())
expected = {"state", "code", "site_key", "degraded_login"}
rec("turnstile-config status 200", r.status_code == 200, str(r.status_code))
rec("turnstile-config keys match", expected.issubset(keys) and "enabled" not in keys, f"keys={sorted(keys)}")
rec("turnstile-config state disabled", body.get("state") == "disabled", str(body.get("state")))

# --- 2. Anonymous should not see break-glass endpoints
r = requests.get(f"{BASE}/api/admin/settings/turnstile/break-glass", timeout=15)
rec("break-glass GET anon 401/403", r.status_code in (401, 403), str(r.status_code))
r = requests.post(f"{BASE}/api/admin/settings/turnstile/break-glass", json={"incident_id": "x"}, timeout=15)
rec("break-glass POST anon 401/403", r.status_code in (401, 403), str(r.status_code))

# --- Login admin
r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=20)
rec("admin login", r.status_code == 200, f"{r.status_code}")
if r.status_code != 200:
    print("LOGIN FAILED, aborting"); print(r.text[:400])
    raise SystemExit(1)

token = None
try:
    j = r.json()
    token = j.get("access_token") or j.get("token")
except Exception:
    pass
if token:
    s.headers.update({"Authorization": f"Bearer {token}"})
# Bootstrap CSRF token so cookie-authenticated mutating requests pass
try:
    rc = s.get(f"{BASE}/api/auth/csrf", timeout=15)
    csrf_tok = s.cookies.get("csrf_token")
    if csrf_tok:
        s.headers.update({"x-csrf-token": csrf_tok})
except Exception as e:
    print("csrf bootstrap failed:", e)

# --- 3. Admin GET break-glass
r = s.get(f"{BASE}/api/admin/settings/turnstile/break-glass", timeout=15)
try: b = r.json()
except: b = {}
rec("admin break-glass GET 200", r.status_code == 200, str(r.status_code))
rec("admin break-glass keys", all(k in b for k in ["active","promotion_blocked","pending_review","record"]),
    f"keys={sorted(b.keys())}")

# --- 4. Admin POST with incomplete body -> 400 break_glass_refused
r = s.post(f"{BASE}/api/admin/settings/turnstile/break-glass", json={"incident_id":"x"}, timeout=15)
try: b = r.json()
except: b = {}
detail = b.get("detail") if isinstance(b.get("detail"), dict) else b
code = (detail or {}).get("code") or b.get("code")
errs = (detail or {}).get("errors") or b.get("errors")
rec("break-glass refused 400", r.status_code == 400, str(r.status_code))
rec("break-glass refused code", code == "break_glass_refused", str(code))
rec("break-glass refused errors list", isinstance(errs, list) and len(errs) > 0, str(errs)[:120])

# --- 5. Full lifecycle
lifecycle_ok = True
payload = {
    "incident_id": f"INC-QA-{uuid.uuid4().hex[:6]}",
    "approver": "qa-approver@stoic.test",
    "reason": "QA drill: verifying governed break-glass lifecycle end to end",
    "scope": ["login"],
    "ttl_minutes": 5,
}
r = s.post(f"{BASE}/api/admin/settings/turnstile/break-glass", json=payload, timeout=15)
try: b = r.json()
except: b = {}
rec("bg activate 200", r.status_code == 200, f"{r.status_code} body={str(b)[:200]}")
rec("bg activate active:true", b.get("active") is True, str(b.get("active")))
rec("bg activate promotion_blocked", b.get("promotion_blocked") is True, str(b.get("promotion_blocked")))

# readiness while active
r = s.get(f"{BASE}/api/ops/release-readiness", timeout=15)
try: rd = r.json()
except: rd = {}
checks = rd.get("checks", {})
bg_check = checks.get("turnstile_break_glass", {})
rec("readiness bg.ok False when active", bg_check.get("ok") is False, str(bg_check))

# deactivate
r = s.post(f"{BASE}/api/admin/settings/turnstile/break-glass/deactivate", json={"note":"qa done"}, timeout=15)
try: b = r.json()
except: b = {}
rec("bg deactivate 200", r.status_code == 200, f"{r.status_code}")
rec("bg deactivate active:false", b.get("active") is False, str(b.get("active")))
rec("bg deactivate pending_review:true", b.get("pending_review") is True, str(b.get("pending_review")))

# review empty
r = s.post(f"{BASE}/api/admin/settings/turnstile/break-glass/review", json={"note":"x"}, timeout=15)
try: b = r.json()
except: b = {}
detail = b.get("detail") if isinstance(b.get("detail"), dict) else b
code = (detail or {}).get("code") or b.get("code")
rec("bg review empty 400", r.status_code == 400, f"{r.status_code}")
rec("bg review empty code review_note_required", code == "review_note_required", str(code))

# review with note
r = s.post(f"{BASE}/api/admin/settings/turnstile/break-glass/review",
           json={"note":"QA post-incident review completed, no findings"}, timeout=15)
try: b = r.json()
except: b = {}
rec("bg review 200", r.status_code == 200, f"{r.status_code}")
rec("bg review promotion_blocked:false", b.get("promotion_blocked") is False, str(b.get("promotion_blocked")))

# readiness now ok
r = s.get(f"{BASE}/api/ops/release-readiness", timeout=15)
try: rd = r.json()
except: rd = {}
bg_check = rd.get("checks", {}).get("turnstile_break_glass", {})
rec("readiness bg.ok True after review", bg_check.get("ok") is True, str(bg_check))

# readiness contains required checks
required_checks = ["turnstile_break_glass","turnstile_config","canonical_decision","inventory","rc_lock"]
present = {k: (k in rd.get("checks",{})) for k in required_checks}
rec("readiness has all checks", all(present.values()), str(present))
if "canonical_decision" in rd.get("checks",{}):
    rec("readiness canonical_decision.state present", "state" in rd["checks"]["canonical_decision"],
        str(rd["checks"]["canonical_decision"])[:200])
if "inventory" in rd.get("checks",{}):
    rec("readiness inventory.counts present", "counts" in rd["checks"]["inventory"],
        str(rd["checks"]["inventory"])[:200])
if "rc_lock" in rd.get("checks",{}):
    rlk = rd["checks"]["rc_lock"]
    rec("readiness rc_lock present/ok", rlk.get("present") is True and rlk.get("ok") is True, str(rlk))

# --- 6. Authority decision
r = s.get(f"{BASE}/api/authority/decision", timeout=15)
try: b = r.json()
except: b = {}
rec("authority/decision 200", r.status_code == 200, f"{r.status_code}")
state = b.get("state")
rec("authority state valid", state in ["READY","DEGRADED","CLOSE_ONLY","BLOCKED","EMERGENCY"], str(state))
rec("authority reason_codes present", isinstance(b.get("reason_codes"), list), str(type(b.get("reason_codes"))))
rec("authority blockers present", isinstance(b.get("blockers"), list), str(type(b.get("blockers"))))
rec("authority accounts is list", isinstance(b.get("accounts"), list), str(type(b.get("accounts"))))
rec("authority dominance correct", b.get("dominance") == ["EMERGENCY","BLOCKED","CLOSE_ONLY","DEGRADED","READY"],
    str(b.get("dominance")))
nea = b.get("new_exposure_allowed")
expected_nea = state in ("READY","DEGRADED")
rec("authority new_exposure_allowed consistent", nea == expected_nea, f"state={state} nea={nea}")

# /api/authority
r = s.get(f"{BASE}/api/authority", timeout=15)
try: b2 = r.json()
except: b2 = {}
rec("authority root 200", r.status_code == 200, f"{r.status_code}")
dec = b2.get("decision") or {}
rec("authority root decision.state matches", dec.get("state") == state, f"{dec.get('state')} vs {state}")
rec("authority root enforced_level==level", b2.get("enforced_level") == b2.get("level"),
    f"{b2.get('enforced_level')} vs {b2.get('level')}")

# anon
r = requests.get(f"{BASE}/api/authority/decision", timeout=15)
rec("authority/decision anon 401/403", r.status_code in (401,403), str(r.status_code))

# --- 7. Inventory
r = s.get(f"{BASE}/api/authority/inventory", timeout=15)
try: b = r.json()
except: b = {}
rec("authority/inventory 200", r.status_code == 200, f"{r.status_code}")
counts = b.get("counts") or {}
required_count_keys = ["configured","live_configured","enabled","live_enabled","bots_enabled","connected","fresh","tradable"]
rec("inventory counts has separate keys", all(k in counts for k in required_count_keys),
    f"counts_keys={sorted(counts.keys())}")
accounts = b.get("accounts") or []
if accounts:
    row = accounts[0]
    rec("inventory account row keys", all(k in row for k in ["immutable_id","environment","enabled","bot_enabled","tradable"]),
        f"row_keys={sorted(row.keys())}")
else:
    rec("inventory has accounts (may be empty)", True, "no rows")

r = s.post(f"{BASE}/api/authority/inventory/expectation", json={"accounts":"x"}, timeout=15)
rec("inventory expectation bad 400", r.status_code == 400, f"{r.status_code}")

# non-admin -> 403: create a temp user, login as it, then delete
temp_email = f"qa-r9-{secrets.token_hex(4)}@example.com"
temp_password = "Kd5#Zt9mW2xVpR7c"
r_reg1 = requests.post(f"{BASE}/api/auth/register",
                       json={"email": temp_email, "password": temp_password, "name": "QA R9",
                             "terms_agreed": True}, timeout=20)

# --- 8. Register invariant: same email twice
t1_start = time.time()
r1 = requests.post(f"{BASE}/api/auth/register",
                   json={"email": temp_email, "password": temp_password, "name": "QA R9",
                         "terms_agreed": True}, timeout=20)
t1 = time.time() - t1_start
t2_start = time.time()
r2 = requests.post(f"{BASE}/api/auth/register",
                   json={"email": temp_email, "password": temp_password, "name": "QA R9",
                         "terms_agreed": True}, timeout=20)
t2 = time.time() - t2_start
try: j1 = r1.json()
except: j1 = {}
try: j2 = r2.json()
except: j2 = {}
rec("register 1 200", r1.status_code == 200, f"{r1.status_code}")
rec("register 2 200", r2.status_code == 200, f"{r2.status_code}")
expected_keys = {"id","email","name","email_verified","message"}
k1, k2 = set(j1.keys()), set(j2.keys())
rec("register 1 exact keys", k1 == expected_keys, f"keys={sorted(k1)}")
rec("register 2 exact keys", k2 == expected_keys, f"keys={sorted(k2)}")
rec("register no activation keys", "activation_email_sent" not in k1 and "activation_email_error" not in k1
    and "activation_email_sent" not in k2 and "activation_email_error" not in k2, "ok")
rec("register ids differ", j1.get("id") != j2.get("id"), f"{j1.get('id')} vs {j2.get('id')}")
rec("register timing within ~0.5s", abs(t1 - t2) <= 0.7, f"t1={t1:.3f} t2={t2:.3f}")

# non-admin inventory expectation
# Mark temp user email_verified=True in DB so we can login for non-admin 403 test
try:
    from pymongo import MongoClient as _MC
    import re as _re
    _env = open("/app/backend/.env").read()
    _murl = _re.search(r"MONGO_URL\s*=\s*[\"']?([^\"'\s]+)", _env).group(1)
    _dbn = _re.search(r"DB_NAME\s*=\s*[\"']?([^\"'\s]+)", _env).group(1)
    _mc = _MC(_murl)
    _mc[_dbn]["users"].update_many({"email": temp_email}, {"$set": {"email_verified": True}})
except Exception as _e:
    print("verify patch failed:", _e)

s2 = requests.Session()
r = s2.post(f"{BASE}/api/auth/login", json={"email": temp_email, "password": temp_password}, timeout=20)
if r.status_code == 200:
    j = r.json(); tk = j.get("access_token") or j.get("token")
    if tk: s2.headers.update({"Authorization": f"Bearer {tk}"})
    try:
        s2.get(f"{BASE}/api/auth/csrf", timeout=15)
        ct = s2.cookies.get("csrf_token")
        if ct: s2.headers.update({"x-csrf-token": ct})
    except Exception:
        pass
    r = s2.post(f"{BASE}/api/authority/inventory/expectation", json={"accounts":[]}, timeout=15)
    rec("inventory expectation non-admin 403", r.status_code == 403, f"{r.status_code}")
else:
    rec("temp user login (for 403 test)", False, f"login {r.status_code} body={r.text[:200]}")

# --- Cleanup: delete temp user in Mongo
try:
    from pymongo import MongoClient
    import re
    env = open("/app/backend/.env").read()
    murl = re.search(r"MONGO_URL\s*=\s*[\"']?([^\"'\s]+)", env).group(1)
    dbn = re.search(r"DB_NAME\s*=\s*[\"']?([^\"'\s]+)", env).group(1)
    mc = MongoClient(murl)
    res = mc[dbn]["users"].delete_many({"email": temp_email})
    rec("cleanup temp user", res.deleted_count >= 1, f"deleted={res.deleted_count}")
except Exception as e:
    rec("cleanup temp user", False, str(e))

print("\n=== SUMMARY ===")
print(f"PASS: {len(results['pass'])}  FAIL: {len(results['fail'])}")
for f in results["fail"]:
    print("  FAIL:", f)

with open("/app/test_reports/round9_live_results.json","w") as f:
    json.dump(results, f, indent=2)
