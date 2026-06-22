"""Iter-12 tests: Settings (Profile/Change-Password), 2FA enroll/verify/disable,
2FA-gated login, and Capital-Preservation Guards persistence on /api/bot/config.

Cleans up created users (regex ^qa_2fa_) at the end of the session.
"""
import os
import uuid
import time
import pyotp
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@trading.bot")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "ai_trading_bot")


def _fresh_email(prefix="qa_2fa"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}@example.com"


def _register(email: str, password: str = "pass123"):
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json={"email": email, "password": password, "name": "QA"}, timeout=15)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module", autouse=True)
def _cleanup_qa_users():
    yield
    try:
        client = MongoClient(MONGO_URL)
        db = client[DB_NAME]
        db.users.delete_many({"email": {"$regex": "^qa_2fa_"}})
        client.close()
    except Exception as e:
        print(f"cleanup error: {e}")


# ---------- Profile ----------
class TestProfileUpdate:
    def test_profile_update_changes_name(self):
        s = _register(_fresh_email())
        r = s.put(f"{API}/auth/profile", json={"name": "Renamed"}, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Renamed"
        assert "two_factor_enabled" in body
        assert body["two_factor_enabled"] is False
        # persisted on /me
        me = s.get(f"{API}/auth/me", timeout=10).json()
        assert me["name"] == "Renamed"


# ---------- Change Password ----------
class TestChangePassword:
    def test_wrong_current_password_401(self):
        email = _fresh_email()
        s = _register(email, "pass123")
        r = s.post(f"{API}/auth/change-password",
                   json={"current_password": "wrongPW", "new_password": "newpass456"},
                   timeout=10)
        assert r.status_code == 401

    def test_same_password_400(self):
        email = _fresh_email()
        s = _register(email, "pass123")
        r = s.post(f"{API}/auth/change-password",
                   json={"current_password": "pass123", "new_password": "pass123"},
                   timeout=10)
        assert r.status_code == 400

    def test_change_password_success_and_login_swap(self):
        email = _fresh_email()
        s = _register(email, "pass123")
        r = s.post(f"{API}/auth/change-password",
                   json={"current_password": "pass123", "new_password": "newpass456"},
                   timeout=10)
        assert r.status_code == 200
        assert r.json().get("ok") is True

        # OLD password must fail
        s2 = requests.Session()
        r_old = s2.post(f"{API}/auth/login", json={"email": email, "password": "pass123"}, timeout=10)
        assert r_old.status_code == 401

        # NEW password must work
        s3 = requests.Session()
        r_new = s3.post(f"{API}/auth/login", json={"email": email, "password": "newpass456"}, timeout=10)
        assert r_new.status_code == 200


# ---------- 2FA enroll / verify / status / disable ----------
class TestTwoFactor:
    def test_status_initially_disabled(self):
        s = _register(_fresh_email())
        r = s.get(f"{API}/auth/2fa/status", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["recovery_codes_remaining"] == 0

    def test_enroll_returns_secret_uri_qr_and_reissues_on_repeat(self):
        s = _register(_fresh_email())
        r = s.post(f"{API}/auth/2fa/enroll", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "secret" in body and len(body["secret"]) >= 16
        assert body["otpauth_uri"].startswith("otpauth://")
        assert body["qr_png_data_url"].startswith("data:image/png;base64,")
        first_secret = body["secret"]

        # Re-enroll (pre-verify) — should re-issue a new pending secret
        r2 = s.post(f"{API}/auth/2fa/enroll", timeout=10)
        assert r2.status_code == 200
        second_secret = r2.json()["secret"]
        assert second_secret != first_secret, "re-enroll should issue a NEW pending secret"

    def test_verify_enroll_wrong_code_401(self):
        s = _register(_fresh_email())
        r1 = s.post(f"{API}/auth/2fa/enroll", timeout=10)
        assert r1.status_code == 200
        r2 = s.post(f"{API}/auth/2fa/verify-enroll", json={"code": "000000"}, timeout=10)
        assert r2.status_code == 401

    def test_verify_enroll_correct_code_enables_and_returns_recovery_codes(self):
        s = _register(_fresh_email())
        r1 = s.post(f"{API}/auth/2fa/enroll", timeout=10)
        secret = r1.json()["secret"]
        code = pyotp.TOTP(secret).now()
        r2 = s.post(f"{API}/auth/2fa/verify-enroll", json={"code": code}, timeout=10)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["ok"] is True
        assert isinstance(body["recovery_codes"], list)
        assert len(body["recovery_codes"]) == 8
        # all plaintext, all distinct
        assert len(set(body["recovery_codes"])) == 8

        # status now enabled with 8 codes remaining
        r3 = s.get(f"{API}/auth/2fa/status", timeout=10).json()
        assert r3["enabled"] is True
        assert r3["recovery_codes_remaining"] == 8

        # /me reports two_factor_enabled
        me = s.get(f"{API}/auth/me", timeout=10).json()
        assert me["two_factor_enabled"] is True


# ---------- Login with 2FA ----------
class TestLoginWith2FA:
    def _enroll_2fa_user(self):
        email = _fresh_email()
        s = _register(email, "pass123")
        r1 = s.post(f"{API}/auth/2fa/enroll", timeout=10)
        secret = r1.json()["secret"]
        code = pyotp.TOTP(secret).now()
        r2 = s.post(f"{API}/auth/2fa/verify-enroll", json={"code": code}, timeout=10)
        assert r2.status_code == 200
        recovery = r2.json()["recovery_codes"]
        return email, secret, recovery

    def test_login_without_totp_returns_401_with_2fa_required_detail(self):
        email, _, _ = self._enroll_2fa_user()
        s2 = requests.Session()
        r = s2.post(f"{API}/auth/login", json={"email": email, "password": "pass123"}, timeout=10)
        assert r.status_code == 401
        assert "2fa code required" in (r.json().get("detail", "") or "").lower()

    def test_login_with_correct_totp_succeeds(self):
        email, secret, _ = self._enroll_2fa_user()
        s2 = requests.Session()
        # Need a fresh TOTP — small wait to avoid clock-edge flakiness
        time.sleep(1)
        code = pyotp.TOTP(secret).now()
        r = s2.post(f"{API}/auth/login",
                    json={"email": email, "password": "pass123", "totp_code": code},
                    timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["two_factor_enabled"] is True

    def test_login_with_recovery_code_consumes_one(self):
        email, _, recovery = self._enroll_2fa_user()
        # use the first recovery code as totp_code
        s2 = requests.Session()
        r = s2.post(f"{API}/auth/login",
                    json={"email": email, "password": "pass123", "totp_code": recovery[0]},
                    timeout=10)
        assert r.status_code == 200, r.text
        # remaining count drops to 7
        st = s2.get(f"{API}/auth/2fa/status", timeout=10).json()
        assert st["recovery_codes_remaining"] == 7

    def test_login_with_invalid_totp_returns_401(self):
        email, _, _ = self._enroll_2fa_user()
        s2 = requests.Session()
        r = s2.post(f"{API}/auth/login",
                    json={"email": email, "password": "pass123", "totp_code": "000000"},
                    timeout=10)
        assert r.status_code == 401


# ---------- 2FA disable ----------
class TestTwoFactorDisable:
    def _enroll_2fa(self, password="pass123"):
        email = _fresh_email()
        s = _register(email, password)
        r1 = s.post(f"{API}/auth/2fa/enroll", timeout=10)
        secret = r1.json()["secret"]
        code = pyotp.TOTP(secret).now()
        r2 = s.post(f"{API}/auth/2fa/verify-enroll", json={"code": code}, timeout=10)
        recovery = r2.json()["recovery_codes"]
        return s, email, secret, recovery

    def test_disable_wrong_password_401(self):
        s, _, secret, _ = self._enroll_2fa()
        time.sleep(1)
        code = pyotp.TOTP(secret).now()
        r = s.post(f"{API}/auth/2fa/disable",
                   json={"current_password": "WRONGPW", "code": code}, timeout=10)
        assert r.status_code == 401

    def test_disable_wrong_code_401(self):
        s, _, _, _ = self._enroll_2fa()
        r = s.post(f"{API}/auth/2fa/disable",
                   json={"current_password": "pass123", "code": "000000"}, timeout=10)
        assert r.status_code == 401

    def test_disable_correct_totp_succeeds_and_status_flips(self):
        s, _, secret, _ = self._enroll_2fa()
        time.sleep(1)
        code = pyotp.TOTP(secret).now()
        r = s.post(f"{API}/auth/2fa/disable",
                   json={"current_password": "pass123", "code": code}, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        st = s.get(f"{API}/auth/2fa/status", timeout=10).json()
        assert st["enabled"] is False
        assert st["recovery_codes_remaining"] == 0

    def test_disable_with_recovery_code_succeeds(self):
        s, _, _, recovery = self._enroll_2fa()
        r = s.post(f"{API}/auth/2fa/disable",
                   json={"current_password": "pass123", "code": recovery[0]}, timeout=10)
        assert r.status_code == 200
        st = s.get(f"{API}/auth/2fa/status", timeout=10).json()
        assert st["enabled"] is False


# ---------- Capital-Preservation Guards persistence ----------
class TestCapitalGuards:
    def test_bot_config_persists_new_guard_fields(self):
        s = _register(_fresh_email())
        payload = {
            "anti_tilt_enabled": True,
            "anti_tilt_consecutive_losses": 5,
            "anti_tilt_freeze_hours": 8,
            "trade_of_day_cap": 2,
            "asia_session_skip_xau": False,
        }
        r = s.put(f"{API}/bot/config", json=payload, timeout=10)
        assert r.status_code == 200, r.text
        cfg = s.get(f"{API}/bot/config", timeout=10).json()
        assert cfg["anti_tilt_enabled"] is True
        assert cfg["anti_tilt_consecutive_losses"] == 5
        assert cfg["anti_tilt_freeze_hours"] == 8
        assert cfg["trade_of_day_cap"] == 2
        assert cfg["asia_session_skip_xau"] is False

    def test_bot_config_default_guard_values(self):
        s = _register(_fresh_email())
        cfg = s.get(f"{API}/bot/config", timeout=10).json()
        # Defaults per models.py
        assert cfg.get("anti_tilt_enabled") in (True, False)
        assert isinstance(cfg.get("anti_tilt_consecutive_losses"), int)
        assert isinstance(cfg.get("anti_tilt_freeze_hours"), int)
        assert isinstance(cfg.get("trade_of_day_cap"), int)
        assert isinstance(cfg.get("asia_session_skip_xau"), bool)


# ---------- Admin regression: still no 2FA + still works ----------
class TestAdminNo2FA:
    def test_admin_login_and_no_2fa(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=10)
        assert r.status_code == 200, r.text
        me = s.get(f"{API}/auth/me", timeout=10).json()
        assert me["email"] == ADMIN_EMAIL
        assert me.get("two_factor_enabled") in (False, None)
