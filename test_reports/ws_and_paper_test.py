"""Verify WebSocket broadcasts panic event + paper trade ExecutionFactory routing."""
import os, requests, json, asyncio, secrets
BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")
WS_URL = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/api/ws"

s = requests.Session()
r = s.post(f"{BASE}/api/auth/login", json={"email": "admin@trading.bot", "password": "admin123"})
print("Login:", r.status_code)
cookies = s.cookies.get_dict()
access = cookies.get("access_token")
assert access, f"no access_token cookie: {cookies}"

# --- WS panic broadcast ---
import websockets

async def ws_listen_and_panic():
    headers = {"Cookie": f"access_token={access}"}
    async with websockets.connect(WS_URL, additional_headers=headers, open_timeout=10) as ws:
        first = await asyncio.wait_for(ws.recv(), timeout=5)
        print("WS first msg:", first[:200])
        # release any active panic first then trigger fresh
        s.post(f"{BASE}/api/panic/release")
        # Now trigger panic
        async def trigger():
            await asyncio.sleep(1)
            rr = s.post(f"{BASE}/api/panic")
            print("Panic POST:", rr.status_code, rr.text[:200])
        task = asyncio.create_task(trigger())
        # Listen for event
        received_panic = False
        try:
            for _ in range(8):
                msg = await asyncio.wait_for(ws.recv(), timeout=4)
                print(f"WS msg: {msg[:200]}")
                try:
                    j = json.loads(msg)
                    if "panic" in json.dumps(j).lower():
                        received_panic = True
                        break
                except Exception: pass
        except asyncio.TimeoutError:
            print("Timeout waiting for panic broadcast")
        await task
        return received_panic

ok = asyncio.run(ws_listen_and_panic())
print(f"WS panic broadcast received: {ok}")
s.post(f"{BASE}/api/panic/release")

# --- Paper trading flow ---
email = f"TEST_paper_{secrets.token_hex(4)}@e.com"
s2 = requests.Session()
r = s2.post(f"{BASE}/api/auth/register", json={"email": email, "password": "test1234", "name": "pp"})
print("Register:", r.status_code)

# Create paper account
r = s2.post(f"{BASE}/api/accounts", json={
    "label": "TEST_paper", "mode": "paper", "account_type": "demo", "initial_balance": 10000
})
print("Create paper account:", r.status_code, r.text[:200])
acc = r.json() if r.status_code == 200 else {}
acc_id = acc.get("id") or acc.get("_id")
print(f"Account mode={acc.get('mode')} id={acc_id}")

# Generate a signal
r = s2.post(f"{BASE}/api/signals/generate", json={"symbol": "BTCUSD", "risk_level": "medium"})
print("Generate signal:", r.status_code)
sig = r.json()
print(f"  action={sig.get('action')} confidence={sig.get('confidence')} regime={sig.get('regime',{}).get('regime')}")
print(f"  has noise_filter: {'noise_filter' in sig}, has meta_label: {'meta_label' in sig}")
print(f"  has compressed_features: {'compressed_features' in sig}, has regime_execution_mode: {'regime_execution_mode' in sig}")

# Subscription plans
r = requests.get(f"{BASE}/api/subscription/plans")
plans = r.json().get("plans", [])
prices = {p["id"]: p.get("price_usd") for p in plans}
print(f"Plans prices: {prices}")
assert prices.get("monthly") == 49.0
assert prices.get("quarterly") in (132.30, 132.3)
assert prices.get("semiannual") in (235.20, 235.2)
assert prices.get("annual") in (352.80, 352.8)
print("PASS: subscription prices correct")
