"""M122 live smoke (preview backend): server-suggest → create account → heartbeat with a BOM-garbage token (401)
→ install-progress says REJECTED → PATCH server (step-up bypass) → adopt-reported-server flow → cleanup."""
import os
import sys
import time

import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
EMAIL = os.environ["TEST_ADMIN_EMAIL"]
PW = os.environ["TEST_ADMIN_PASSWORD"]
BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")
RL = os.environ.get("RATE_LIMIT_BYPASS_TOKEN", "")

s = requests.Session()
s.headers.update({"X-Step-Up-Bypass": BYPASS, "X-RateLimit-Bypass": RL})
r = s.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PW}, timeout=30)
assert r.status_code == 200, r.text
tok = r.json().get("access_token") or r.json().get("token")
if tok:
    s.headers["Authorization"] = f"Bearer {tok}"
if s.cookies.get("csrf_token"):
    s.headers["X-CSRF-Token"] = s.cookies.get("csrf_token")


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        sys.exit(1)


sg = s.get(f"{BASE}/api/accounts/server-suggest", params={"server": "OnEquity-Dem", "broker": "OnEquity"}, timeout=30)
check("server-suggest typo", sg.status_code == 200 and sg.json()["known"] is False and "OnEquity-Server" in sg.json()["suggestions"], sg.text[:200])
sg2 = s.get(f"{BASE}/api/accounts/server-suggest", params={"server": "OnEquity-Server", "broker": "OnEquity"}, timeout=30)
check("server-suggest known", sg2.json()["known"] is True, sg2.text[:120])

login = str(90000000 + int(time.time()) % 9000000)
acc = s.post(f"{BASE}/api/accounts", json={"label": "m122-smoke", "broker": "OnEquity", "server": "OnEquity-Demo",
                                           "account_number": login, "account_type": "demo", "mode": "live"}, timeout=30)
check("create account", acc.status_code == 200, acc.text[:200])
aid = acc.json()["id"]
try:
    # P2.1 — heartbeat with the garbage the old EA sent (the comment line) → 401 → mark on the login
    hb = requests.post(f"{BASE}/api/bridge/heartbeat", json={
        "bridge_token": "# STOIC bridge token — keep this file confidential.", "balance": 1, "equity": 1,
        "account_login": int(login), "broker_server": "OnEquity-Server", "ea_version": "1.62"}, timeout=30)
    check("garbage token → 401", hb.status_code == 401 and hb.json()["detail"]["code"] == "invalid_bridge_token", hb.text[:200])
    ip = s.get(f"{BASE}/api/setup/install-progress/{aid}", timeout=30).json()
    hbstep = next(x for x in ip["steps"] if x["id"] == "heartbeat")
    check("install-progress REJECTED", hbstep["status"] == "blocked" and hbstep["detail"].startswith("Heartbeats arriving but REJECTED"), hbstep["detail"][:120])
    check("hint not WebRequest", "WebRequest is NOT the problem" in hbstep["hint"] and ip["bridge_rejected"]["token_len"] == 51)

    # P2 — real token heartbeat reporting a different server name for the SAME login
    rt = s.post(f"{BASE}/api/accounts/{aid}/rotate-token", timeout=30)
    check("rotate token", rt.status_code == 200, rt.text[:200])
    btok = rt.json()["bridge_token"]
    hb2 = requests.post(f"{BASE}/api/bridge/heartbeat", json={
        "bridge_token": btok, "balance": 1000, "equity": 1000, "account_login": int(login),
        "broker_server": "OnEquity-Server", "ea_version": "1.63", "installation_id": "inst_0123456789ab"}, timeout=30)
    check("valid heartbeat 200", hb2.status_code == 200, hb2.text[:200])
    a = s.get(f"{BASE}/api/accounts", timeout=30).json()
    me = next(x for x in (a if isinstance(a, list) else a.get("accounts", [])) if x["id"] == aid)
    check("rejection mark cleared by a valid heartbeat", not me.get("bridge_rejected"), str(me.get("bridge_rejected")))
    check("EA reported server recorded", (me.get("ea_identity") or {}).get("broker_server") == "OnEquity-Server", str(me.get("ea_identity")))

    # PATCH server validation + step-up gate
    bad = s.patch(f"{BASE}/api/accounts/{aid}", json={"server": "x"}, timeout=30)
    check("PATCH invalid server → 400", bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_server_name", bad.text[:200])
    nomfa = requests.patch(f"{BASE}/api/accounts/{aid}", json={"server": "OnEquity-Live"}, timeout=30,
                           headers={k: v for k, v in s.headers.items() if k != "X-Step-Up-Bypass"}, cookies=s.cookies)
    check("PATCH server without step-up → 403", nomfa.status_code == 403, nomfa.text[:200])

    ad = s.post(f"{BASE}/api/accounts/{aid}/adopt-reported-server", timeout=30)
    check("adopt-reported-server", ad.status_code == 200 and ad.json()["server"] == "OnEquity-Server", ad.text[:200])
    a = s.get(f"{BASE}/api/accounts", timeout=30).json()
    me = next(x for x in (a if isinstance(a, list) else a.get("accounts", [])) if x["id"] == aid)
    check("server + expected_identity updated, chain reset",
          me["server"] == "OnEquity-Server" and me["expected_identity"]["broker_server"] == "OnEquity-Server"
          and not me.get("verified_identity") and me.get("broker_account_mismatch") is False, str({k: me.get(k) for k in ("server", "expected_identity", "verified_identity", "broker_account_mismatch")}))
    again = s.post(f"{BASE}/api/accounts/{aid}/adopt-reported-server", timeout=30)
    check("adopt again → unchanged no-op", again.status_code == 200 and again.json().get("unchanged") is True, again.text[:200])

    ed = s.patch(f"{BASE}/api/accounts/{aid}", json={"server": "OnEquity-Live", "label": "m122-smoke-2"}, timeout=30)
    check("PATCH server + label", ed.status_code == 200 and ed.json()["server"] == "OnEquity-Live" and ed.json()["label"] == "m122-smoke-2", ed.text[:200])
    pp = s.patch(f"{BASE}/api/accounts/{aid}", json={"label": "m122-smoke-3"}, timeout=30)
    check("PATCH label only still works", pp.status_code == 200 and pp.json()["label"] == "m122-smoke-3", pp.text[:200])
    print("ALL PASS")
finally:
    s.delete(f"{BASE}/api/accounts/{aid}?force=true", timeout=30)
