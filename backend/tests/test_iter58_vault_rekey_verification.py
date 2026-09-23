from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-58 · Independent verification of Phase 1 hardening + vault re-key fix.

Focus areas (from review_request):
  1. Admin cookie-auth login + GET /api/auth/csrf bootstrap.
  2. Credentials reveal: /api/accounts/{id}/credentials/reveal returns 200 with
     investor_password decrypted (this is the KEY_VAULT_MASTER re-key fix — the
     symptom before the migration was 500 'Could not decrypt stored credentials').
  3. Telegram test endpoint: /api/notifications/telegram/test returns 200 or 400
     (never 500 'Could not decrypt bot token').
  4. Strict BotConfigUpdate validation: PUT /api/bot/config rejects invalid
     payloads (422) and accepts a valid round-trip payload (200).
  5. Login rate limiting: 6+ failed logins for a throwaway email → 429.
  6. Health endpoints: /api/health, /api/health/live, /api/health/ready.
  7. Refresh rotation: login → refresh → new tokens issued (200).
"""
import os
import uuid
import pytest
import requests
from live_target import require_live_base_url

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
BYPASS = os.environ.get("RATE_LIMIT_BYPASS_TOKEN")

pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
def _headers(csrf=None, bypass=True, extra=None):
    h = {"Content-Type": "application/json"}
    if csrf:
        h["X-CSRF-Token"] = csrf
    if bypass and BYPASS:
        h["X-RateLimit-Bypass"] = BYPASS
    if extra:
        h.update(extra)
    return h


def _admin_session():
    """Login as admin and return a requests.Session pre-populated with cookies
    plus the csrf token string."""
    s = requests.Session()
    r = s.post(
        f"{API}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers=_headers(),
        timeout=15,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie missing after login"
    return s, csrf


# ─────────── 6 · Health endpoints ───────────
class TestHealthEndpoints:
    def test_health_sanitized_200(self):
        r = requests.get(f"{API}/health", timeout=10)
        assert r.status_code == 200
        j = r.json()
        assert j.get("status") == "ok"
        assert j.get("db") in ("connected", "ok")
        # Confirm no exception internals leaked (SEC hardening)
        for k in ("exception", "traceback", "error_detail", "stack"):
            assert k not in j, f"health leaks internals via '{k}': {j}"

    def test_health_live_200(self):
        r = requests.get(f"{API}/health/live", timeout=10)
        assert r.status_code == 200

    def test_health_ready_200_with_db_check(self):
        r = requests.get(f"{API}/health/ready", timeout=10)
        assert r.status_code == 200, r.text
        j = r.json()
        checks = j.get("checks", {})
        assert checks, f"ready payload missing 'checks': {j}"
        assert checks.get("db") in (True, "ok", "connected"), checks


# ─────────── 1 · Admin login + CSRF bootstrap ───────────
class TestAdminLoginAndCSRFBootstrap:
    def test_admin_login_sets_all_three_cookies(self):
        r = requests.post(
            f"{API}/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers=_headers(),
            timeout=15,
        )
        assert r.status_code == 200, r.text
        assert r.cookies.get("access_token"), "access_token missing"
        assert r.cookies.get("refresh_token"), "refresh_token missing"
        assert r.cookies.get("csrf_token"), "csrf_token missing"

    def test_csrf_bootstrap_endpoint(self):
        # Anonymous — GET /api/auth/csrf must set a csrf_token cookie
        r = requests.get(f"{API}/auth/csrf", timeout=10)
        assert r.status_code == 200, r.text
        assert r.cookies.get("csrf_token"), "csrf bootstrap did not set cookie"
        j = r.json()
        assert j.get("ok") is True


# ─────────── 2 · Credentials reveal (KEY_VAULT_MASTER re-key verification) ───────────
class TestCredentialsRevealVaultReKey:
    def _first_account_with_investor_creds(self, sess):
        """Return the account_id of the admin's first account that has an
        investor credential blob stored. Uses /api/accounts."""
        r = sess.get(f"{API}/accounts", timeout=15)
        assert r.status_code == 200, r.text
        items = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        # /api/accounts payload only tells us whether creds are stored via a
        # boolean-ish field; we need one that has investor creds. Fall back to
        # a direct DB query if the API doesn't expose that flag.
        for a in items:
            aid = a.get("id") or a.get("_id") or a.get("account_id")
            has_inv = a.get("has_investor_creds") or a.get("credentials_stored") \
                or (isinstance(a.get("creds"), dict) and a["creds"].get("investor"))
            if has_inv and aid:
                return aid
        # Fallback: hit DB directly
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
        import motor.motor_asyncio, asyncio
        async def _find():
            c = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = c[os.environ["DB_NAME"]]
            admin = await db.users.find_one({"email": ADMIN_EMAIL})
            acct = await db.accounts.find_one(
                {"user_id": str(admin["_id"]), "creds.investor": {"$exists": True}}
            )
            c.close()
            return str(acct["_id"]) if acct else None
        return asyncio.get_event_loop().run_until_complete(_find())

    def test_reveal_returns_decrypted_investor_password(self):
        sess, csrf = _admin_session()
        aid = self._first_account_with_investor_creds(sess)
        assert aid, "no admin account with stored investor creds found"
        r = sess.post(
            f"{API}/accounts/{aid}/credentials/reveal",
            json={"password": ADMIN_PASSWORD, "include_master": False},
            headers=_headers(csrf=csrf),
            timeout=20,
        )
        # The CORE assertion of the review_request: MUST NOT be 500 'Could
        # not decrypt stored credentials'.
        assert r.status_code != 500, \
            f"vault decrypt FAILED (rekey migration bug): 500 → {r.text}"
        assert r.status_code == 200, f"reveal returned {r.status_code}: {r.text}"
        body = r.json()
        assert "investor_password" in body, body
        assert body["investor_password"] is not None, \
            "investor_password came back None — decryption returned empty"
        assert isinstance(body["investor_password"], str) \
            and len(body["investor_password"]) > 0, body

    def test_reveal_wrong_password_returns_403_not_500(self):
        sess, csrf = _admin_session()
        aid = self._first_account_with_investor_creds(sess)
        assert aid
        r = sess.post(
            f"{API}/accounts/{aid}/credentials/reveal",
            json={"password": "wrong-password-xyz", "include_master": False},
            headers=_headers(csrf=csrf),
            timeout=15,
        )
        assert r.status_code == 403, f"expected 403 not {r.status_code}: {r.text}"


# ─────────── 3 · Telegram test endpoint (bot token decrypt) ───────────
class TestTelegramTokenDecrypt:
    def test_telegram_test_endpoint_does_not_500_on_decrypt(self):
        sess, csrf = _admin_session()
        r = sess.post(
            f"{API}/notifications/telegram/test",
            headers=_headers(csrf=csrf),
            timeout=20,
        )
        # Must NOT be 500 "Could not decrypt bot token".
        # 200 (message sent), 400 (chat_id or telegram API rejection),
        # 502 (network error) are all acceptable outcomes.
        assert r.status_code != 500, \
            f"telegram token DECRYPT FAILED (rekey bug): 500 → {r.text}"
        assert r.status_code in (200, 400, 502), \
            f"unexpected status {r.status_code}: {r.text}"
        # If we do get 500, also confirm the message specifically wasn't
        # the vault decrypt error (belt-and-suspenders — the assert above
        # already trips first).
        if r.status_code == 500:
            body = r.text.lower()
            assert "could not decrypt" not in body, body


# ─────────── 4 · Strict BotConfigUpdate validation ───────────
class TestBotConfigStrictValidation:
    def _current_config(self, sess):
        r = sess.get(f"{API}/bot/config", timeout=15)
        assert r.status_code == 200, r.text
        return r.json()

    def test_negative_max_concurrent_trades_rejected_422(self):
        sess, csrf = _admin_session()
        r = sess.put(
            f"{API}/bot/config",
            json={"max_concurrent_trades": -1},
            headers=_headers(csrf=csrf),
            timeout=15,
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    def test_out_of_range_daily_drawdown_rejected_422(self):
        sess, csrf = _admin_session()
        r = sess.put(
            f"{API}/bot/config",
            json={"daily_drawdown_pct": -5.0},
            headers=_headers(csrf=csrf),
            timeout=15,
        )
        assert r.status_code == 422, f"expected 422 for -5.0, got {r.status_code}: {r.text}"

    def test_bad_enum_daily_profit_target_action_rejected_422(self):
        sess, csrf = _admin_session()
        r = sess.put(
            f"{API}/bot/config",
            json={"daily_profit_target_action": "TURBO_LOCK"},
            headers=_headers(csrf=csrf),
            timeout=15,
        )
        assert r.status_code == 422, \
            f"expected 422 for bad enum, got {r.status_code}: {r.text}"

    def test_negative_partial_close_fraction_rejected(self):
        sess, csrf = _admin_session()
        r = sess.put(
            f"{API}/bot/config",
            json={"partial_close_fraction": -0.1},
            headers=_headers(csrf=csrf),
            timeout=15,
        )
        assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"

    def test_valid_partial_update_accepted_and_roundtrips(self):
        """Send a small valid patch, confirm 200 + values persisted. Then
        restore the prior value so this test is non-destructive."""
        sess, csrf = _admin_session()
        current = self._current_config(sess)
        # Pick a safe field: min_confidence_override (0-95). Toggle by +1
        # within valid range then restore.
        original = int(current.get("min_confidence_override", 0) or 0)
        new_val = 3 if original != 3 else 5
        try:
            r = sess.put(
                f"{API}/bot/config",
                json={"min_confidence_override": new_val},
                headers=_headers(csrf=csrf),
                timeout=15,
            )
            assert r.status_code == 200, f"valid update failed: {r.status_code} {r.text}"
            after = self._current_config(sess)
            assert int(after.get("min_confidence_override", 0)) == new_val, \
                f"value did not persist: expected {new_val}, got {after.get('min_confidence_override')}"
        finally:
            # Restore
            sess.put(
                f"{API}/bot/config",
                json={"min_confidence_override": original},
                headers=_headers(csrf=csrf),
                timeout=15,
            )


# ─────────── 5 · Login rate limiting ───────────
class TestLoginRateLimit:
    def test_repeated_failed_logins_return_429_within_six_attempts(self):
        # Use a throwaway email so we never lock a real user.
        # Login lockout is intentionally NOT bypassable — DO NOT send bypass.
        email = f"iter58_ratelimit_{uuid.uuid4().hex[:10]}@example.com"
        seen_429 = False
        for i in range(1, 9):
            r = requests.post(
                f"{API}/auth/login",
                json={"email": email, "password": "wrong-pass"},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if r.status_code == 429:
                seen_429 = True
                # Attempt count should be within the first 6 tries
                assert i <= 6, f"429 arrived at attempt {i} (>6)"
                break
            assert r.status_code in (401, 403), \
                f"attempt {i}: unexpected status {r.status_code}: {r.text}"
        assert seen_429, "never got 429 within 8 failed attempts"


# ─────────── 7 · Refresh token rotation ───────────
class TestRefreshRotation:
    def test_login_refresh_yields_new_tokens(self):
        sess, csrf = _admin_session()
        old_refresh = sess.cookies.get("refresh_token")
        old_access = sess.cookies.get("access_token")
        assert old_refresh and old_access
        r = sess.post(
            f"{API}/auth/refresh",
            headers=_headers(csrf=csrf),
            timeout=15,
        )
        assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text}"
        new_refresh = sess.cookies.get("refresh_token")
        new_access = sess.cookies.get("access_token")
        assert new_refresh, "no refresh_token cookie after refresh"
        assert new_access, "no access_token cookie after refresh"
        assert new_refresh != old_refresh, "refresh_token was not rotated"
        # Access token may or may not be identical depending on iss-time
        # rounding — but almost always changes because jti / iat differ.
        # We assert only the refresh rotation, which is the security-critical
        # property.


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
