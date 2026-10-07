"""Preview E2E: set the Security Telegram keys through the Admin → Integrations API (vault), check status,
send one test alert, then clear the keys again so the preview stays silent. Reads creds from env."""
import os
import sys

import requests

BASE = os.environ["BASE"]
EMAIL, PW = os.environ["TEST_ADMIN_EMAIL"], os.environ["TEST_ADMIN_PASSWORD"]
TOKEN, CHAT = os.environ["TG_TOKEN"], os.environ["TG_CHAT"]

s = requests.Session()
r = s.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PW}, timeout=30)
print("login", r.status_code, r.text[:120])
assert r.status_code == 200, r.text
csrf = s.cookies.get("csrf_token")
H = {"X-CSRF-Token": csrf} if csrf else {}


def status():
    r = s.get(f"{BASE}/api/admin/security-alerts/status", timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def integ():
    r = s.get(f"{BASE}/api/admin/integrations", timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


st = status()
print("status before:", st["configured"], st["token_source"], "| hint:", st["hint"][:60])
assert not st["configured"] and "SET" in st["hint"]

# validation: 'bot' prefix refused
r = s.post(f"{BASE}/api/admin/integrations/secret", headers=H,
           json={"key": "SECURITY_AGENT_TELEGRAM_BOT_TOKEN", "value": "bot" + TOKEN, "password": PW}, timeout=30)
print("bad token →", r.status_code, r.text[:100])
assert r.status_code == 422

for key, val in (("SECURITY_AGENT_TELEGRAM_BOT_TOKEN", TOKEN), ("SECURITY_AGENT_TELEGRAM_CHAT_ID", CHAT)):
    r = s.post(f"{BASE}/api/admin/integrations/secret", headers=H, json={"key": key, "value": val, "password": PW}, timeout=30)
    print("set", key, r.status_code, r.text[:80])
    assert r.status_code == 200 and TOKEN not in r.text

keys = integ()["keys"]
print("integrations:", {k: (v["configured"], v["source"], v["display"]) for k, v in keys.items() if v["provider"] == "security_telegram"})
assert keys["SECURITY_AGENT_TELEGRAM_BOT_TOKEN"]["source"] == "vault" and TOKEN not in str(keys)

st = status()
print("status after:", st["configured"], st["token_source"], st["chat_id_masked"])
assert st["configured"] and st["token_source"] == "vault"

r = s.post(f"{BASE}/api/admin/security-alerts/test", headers=H, timeout=30)
print("test alert →", r.status_code, r.text[:120])
assert r.status_code == 200 and r.json()["ok"], r.text

if "--keep" not in sys.argv:
    for key in ("SECURITY_AGENT_TELEGRAM_BOT_TOKEN", "SECURITY_AGENT_TELEGRAM_CHAT_ID"):
        r = s.post(f"{BASE}/api/admin/integrations/secret", headers=H, json={"key": key, "value": "", "password": PW}, timeout=30)
        assert r.status_code == 200, r.text
    st = status()
    print("status after clear:", st["configured"], st["token_source"])
    assert not st["configured"]
print("PASS")
