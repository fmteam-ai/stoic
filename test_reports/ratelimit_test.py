"""Quick test: Co-Pilot rate limit - 31st rapid chat in 5min should 429."""
import os, requests
BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")

s = requests.Session()
# Use a fresh user so we don't interfere with admin sessions
import time, secrets
email = f"TEST_rl_{secrets.token_hex(4)}@e.com"
r = s.post(f"{BASE}/api/auth/register", json={"email": email, "password": "test1234", "name": "rl"})
assert r.status_code == 200, r.text

statuses = []
for i in range(35):
    r = s.post(f"{BASE}/api/copilot/chat", json={"message": f"hi {i}"})
    statuses.append(r.status_code)
    if r.status_code == 429:
        print(f"Got 429 at request #{i+1}")
        break

print("Statuses:", statuses)
print("Count 200:", statuses.count(200), "Count 429:", statuses.count(429))
assert 429 in statuses, "Expected 429 within 35 rapid requests"
print("PASS rate-limit kicked in")
