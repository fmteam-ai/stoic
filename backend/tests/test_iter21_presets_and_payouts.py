"""Iter21 regression suite: Strategy Presets (built-in + custom), Affiliate
Payout self-service flow, and a Copilot smoke check.

All tests hit the live HTTP API on REACT_APP_BACKEND_URL via httpOnly cookies,
exactly as the frontend would. The one exception is the admin-process-payout
end-to-end test, which requires a synthetic `affiliate_payout_requests` doc
to be seeded directly into MongoDB (the only way to exercise the process
endpoint without manually accumulating $50 of commissions first).
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import time
import uuid
import requests
import pytest
from pymongo import MongoClient
from bson import ObjectId

def _read_env():
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        return None


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _read_env() or "").rstrip("/")
if not BASE_URL:
    raise RuntimeError("REACT_APP_BACKEND_URL not set")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "ai_trading_bot")


# ---------- Helpers ----------------------------------------------------------

def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    return s


def _register_unique() -> tuple[requests.Session, str]:
    from helpers import register_and_login
    email = f"iter21_{uuid.uuid4().hex[:10]}@iter21test.com"
    return register_and_login(email, "Kd5#Zt9mW2xVpR7c", name="Iter21 Tester"), email


@pytest.fixture(scope="module")
def admin_session():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def user_a():
    s, email = _register_unique()
    return s, email


@pytest.fixture(scope="module")
def user_b():
    s, email = _register_unique()
    return s, email


@pytest.fixture(scope="module")
def mongo_db():
    cli = MongoClient(MONGO_URL)
    return cli[DB_NAME]


# ---------- BUILT-IN PRESETS ------------------------------------------------

EXPECTED_ORDER = ["adaptive", "sniper", "scalper", "fast_scalp",
                  "trend_rider", "breakout", "mean_reversion", "balanced"]


class TestBuiltinPresets:
    def test_list_presets_shape_and_order(self, user_a):
        s, _ = user_a
        r = s.get(f"{API}/bot/presets", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "presets" in body and "custom" in body
        presets = body["presets"]
        assert len(presets) == 8
        keys = [p["key"] for p in presets]
        assert keys == EXPECTED_ORDER
        # Each must have the documented fields
        for p in presets:
            for field in ("key", "label", "tagline", "description", "icon", "color", "config"):
                assert field in p, f"missing field {field} in {p.get('key')}"
            assert isinstance(p["config"], dict)
            # Config must NOT include risk_level / symbols / drawdown
            forbidden = {"risk_level", "symbols", "max_drawdown_pct"}
            assert not (set(p["config"].keys()) & forbidden), \
                f"{p['key']} leaked forbidden field: {set(p['config'].keys()) & forbidden}"

    def test_apply_sniper_preset(self, user_a):
        s, _ = user_a
        # Read existing config first so we can verify risk_level/symbols are untouched
        before = s.get(f"{API}/bot/config", timeout=15).json()
        r = s.post(f"{API}/bot/preset/sniper", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] == "sniper"
        assert body["label"] == "Sniper"
        cfg = body["config"]
        assert cfg["min_confidence_override"] == 75
        assert cfg["trade_of_day_cap"] == 1
        assert cfg["active_preset"] == "sniper"

        # Untouched fields verified via /bot/config
        after = s.get(f"{API}/bot/config", timeout=15).json()
        assert after["active_preset"] == "sniper"
        assert after["min_confidence_override"] == 75
        assert after["trade_of_day_cap"] == 1
        # Preserve risk_level and symbols
        if "risk_level" in before:
            assert after.get("risk_level") == before.get("risk_level")
        if "symbols" in before:
            assert after.get("symbols") == before.get("symbols")

    def test_apply_invalid_preset_returns_404(self, user_a):
        s, _ = user_a
        r = s.post(f"{API}/bot/preset/does_not_exist", timeout=15)
        assert r.status_code == 404, r.text
        assert "not found" in r.json().get("detail", "").lower()


# ---------- CUSTOM (USER) PRESETS ------------------------------------------

class TestUserPresets:
    """Save/list/apply/delete custom presets, plus validations."""

    @pytest.fixture(scope="class")
    def created_preset(self, user_a):
        s, _ = user_a
        name = f"TEST_Preset_{uuid.uuid4().hex[:6]}"
        r = s.post(f"{API}/bot/my-presets",
                   json={"name": name, "description": "desc"}, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        return body, name

    def test_save_custom_preset_response_shape(self, created_preset):
        body, name = created_preset
        assert body["name"] == name
        assert body["description"] == "desc"
        assert body["is_custom"] is True
        assert "id" in body and len(body["id"]) > 0
        assert "created_at" in body and body["created_at"]
        # config is sanitized to behaviour-knob whitelist only
        forbidden = {"risk_level", "symbols", "max_drawdown_pct"}
        assert not (set(body["config"].keys()) & forbidden), \
            f"custom preset leaked forbidden fields: {set(body['config'].keys()) & forbidden}"

    def test_save_preset_empty_name_400(self, user_a):
        s, _ = user_a
        r = s.post(f"{API}/bot/my-presets", json={"name": "  "}, timeout=15)
        assert r.status_code == 400
        assert "name" in r.json().get("detail", "").lower()

    def test_save_preset_duplicate_name_400(self, user_a, created_preset):
        s, _ = user_a
        _, name = created_preset
        r = s.post(f"{API}/bot/my-presets", json={"name": name}, timeout=15)
        assert r.status_code == 400
        assert "already have a preset" in r.json().get("detail", "").lower()

    def test_list_presets_includes_custom(self, user_a, created_preset):
        s, _ = user_a
        body, _ = created_preset
        r = s.get(f"{API}/bot/presets", timeout=15)
        assert r.status_code == 200
        ids = [c["id"] for c in r.json().get("custom", [])]
        assert body["id"] in ids

    def test_apply_custom_preset(self, user_a, created_preset):
        s, _ = user_a
        body, _ = created_preset
        key = f"custom:{body['id']}"
        r = s.post(f"{API}/bot/preset/{key}", timeout=15)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["applied"] == key
        assert out["config"]["active_preset"] == key

    def test_apply_custom_invalid_id_404(self, user_a):
        s, _ = user_a
        # Valid-shaped ObjectId that doesn't exist
        fake = "0" * 24
        r = s.post(f"{API}/bot/preset/custom:{fake}", timeout=15)
        assert r.status_code == 404
        # malformed id also 404
        r2 = s.post(f"{API}/bot/preset/custom:notanid", timeout=15)
        assert r2.status_code == 404

    def test_preset_limit_max_10(self, user_a, mongo_db):
        """Saving an 11th preset must 400 with a limit/max message."""
        s, _ = user_a
        # Find user_id via /auth/me
        me = s.get(f"{API}/auth/me", timeout=15).json()
        uid = me["id"]
        # Wipe and reseed 10 presets for this user
        mongo_db.user_presets.delete_many({"user_id": uid})
        for i in range(10):
            r = s.post(f"{API}/bot/my-presets",
                       json={"name": f"TEST_Limit_{i}_{uuid.uuid4().hex[:4]}"}, timeout=15)
            assert r.status_code == 200, r.text
        # 11th must fail
        r = s.post(f"{API}/bot/my-presets",
                   json={"name": f"TEST_Limit_overflow_{uuid.uuid4().hex[:4]}"}, timeout=15)
        assert r.status_code == 400, r.text
        detail = r.json().get("detail", "").lower()
        assert ("limit" in detail) or ("max" in detail)
        # Cleanup so other tests aren't affected
        mongo_db.user_presets.delete_many({"user_id": uid})

    def test_delete_custom_preset(self, user_a):
        s, _ = user_a
        # Create one specifically to delete
        name = f"TEST_DeleteMe_{uuid.uuid4().hex[:6]}"
        r = s.post(f"{API}/bot/my-presets", json={"name": name}, timeout=15)
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        # Delete
        d = s.delete(f"{API}/bot/my-presets/{pid}", timeout=15)
        assert d.status_code == 200, d.text
        assert d.json().get("deleted") is True
        # Not in list anymore
        r2 = s.get(f"{API}/bot/presets", timeout=15)
        ids = [c["id"] for c in r2.json().get("custom", [])]
        assert pid not in ids
        # Delete again → 404
        d2 = s.delete(f"{API}/bot/my-presets/{pid}", timeout=15)
        assert d2.status_code == 404

    def test_delete_nonexistent_preset_404(self, user_a):
        s, _ = user_a
        r = s.delete(f"{API}/bot/my-presets/{'0'*24}", timeout=15)
        assert r.status_code == 404


# ---------- PRESET USER ISOLATION ------------------------------------------

class TestPresetIsolation:
    def test_user_b_cannot_see_apply_or_delete_user_a_presets(self, user_a, user_b):
        sa, _ = user_a
        sb, _ = user_b
        # User A creates a preset
        name = f"TEST_AOnly_{uuid.uuid4().hex[:6]}"
        ra = sa.post(f"{API}/bot/my-presets", json={"name": name}, timeout=15)
        assert ra.status_code == 200, ra.text
        pid = ra.json()["id"]
        try:
            # User B's custom[] must NOT include this id
            rb = sb.get(f"{API}/bot/presets", timeout=15)
            assert rb.status_code == 200
            b_ids = [c["id"] for c in rb.json().get("custom", [])]
            assert pid not in b_ids
            # User B apply → 404
            r_apply = sb.post(f"{API}/bot/preset/custom:{pid}", timeout=15)
            assert r_apply.status_code == 404
            # User B delete → 404
            r_del = sb.delete(f"{API}/bot/my-presets/{pid}", timeout=15)
            assert r_del.status_code == 404
        finally:
            # Cleanup
            sa.delete(f"{API}/bot/my-presets/{pid}", timeout=15)


# ---------- AFFILIATE PAYOUT FLOW ------------------------------------------

class TestAffiliatePayout:
    def test_payout_request_no_affiliate_returns_404(self, user_b):
        s, _ = user_b
        r = s.post(f"{API}/affiliate/request-payout", timeout=15)
        assert r.status_code == 404, r.text
        assert "no active affiliate" in r.json().get("detail", "").lower()

    def test_payout_request_admin_zero_balance_400(self, admin_session, mongo_db):
        # Ensure admin's unpaid_balance is 0 so we hit the min-payout gate
        mongo_db.affiliates.update_one(
            {"user_id_lookup": "admin"},  # noop write target
            {"$set": {"_noop": True}},
            upsert=False,
        )
        # Find admin affiliate by code MI77QK and zero it
        mongo_db.affiliates.update_one(
            {"code": "MI77QK"},
            {"$set": {"unpaid_balance_usd": 0.0, "active": True}},
        )
        # Also make sure no pending payout request blocks the test
        admin_user = mongo_db.users.find_one({"email": ADMIN_EMAIL})
        if admin_user:
            aff = mongo_db.affiliates.find_one({"user_id": str(admin_user["_id"])})
            if aff:
                mongo_db.affiliate_payout_requests.delete_many(
                    {"affiliate_id": str(aff["_id"]), "status": "pending"}
                )
        r = admin_session.post(f"{API}/affiliate/request-payout", timeout=15)
        assert r.status_code == 400, r.text
        detail = r.json().get("detail", "").lower()
        assert "minimum payout" in detail or "$50" in detail

    def test_list_my_payout_requests_returns_array(self, admin_session, user_b):
        # Admin has an affiliate row → should return {requests:[...]}
        r_admin = admin_session.get(f"{API}/affiliate/payout-requests", timeout=15)
        assert r_admin.status_code == 200
        assert "requests" in r_admin.json()
        assert isinstance(r_admin.json()["requests"], list)
        # Non-affiliate user → empty list
        sb, _ = user_b
        r_b = sb.get(f"{API}/affiliate/payout-requests", timeout=15)
        assert r_b.status_code == 200
        assert r_b.json().get("requests") == []

    def test_admin_payout_requests_non_admin_403(self, user_b):
        s, _ = user_b
        r = s.get(f"{API}/admin/affiliate/payout-requests", timeout=15)
        assert r.status_code == 403, r.text
        assert "admin only" in r.json().get("detail", "").lower()

    def test_admin_payout_requests_admin_200(self, admin_session):
        r = admin_session.get(f"{API}/admin/affiliate/payout-requests", timeout=15)
        assert r.status_code == 200
        assert "requests" in r.json()

    def test_admin_process_payout_invalid_id_400(self, admin_session):
        r = admin_session.post(f"{API}/admin/affiliate/payout-requests/badid/process", timeout=15)
        assert r.status_code in (400, 404), r.text

    def test_admin_process_payout_end_to_end(self, admin_session, mongo_db):
        """Seed a payout_request + a pending commission directly in Mongo, then
        hit the process endpoint and verify (a) status=paid, (b) commission
        flipped, (c) affiliate unpaid_balance zeroed.
        """
        # Look up admin's affiliate doc (code MI77QK)
        aff = mongo_db.affiliates.find_one({"code": "MI77QK"})
        assert aff, "Admin affiliate (MI77QK) not found — cannot run E2E payout test"
        aff_id = str(aff["_id"])
        # Set a non-zero unpaid balance, plus seed a pending commission and a pending payout request
        mongo_db.affiliates.update_one({"_id": aff["_id"]}, {"$set": {"unpaid_balance_usd": 75.0}})
        # Clean any prior test artefacts
        mongo_db.affiliate_commissions.delete_many({"affiliate_id": aff_id, "_test_iter21": True})
        mongo_db.affiliate_payout_requests.delete_many({"affiliate_id": aff_id, "_test_iter21": True})
        comm_id = mongo_db.affiliate_commissions.insert_one({
            "affiliate_id": aff_id,
            "amount_usd": 75.0,
            "status": "pending",
            "_test_iter21": True,
        }).inserted_id
        req_id = mongo_db.affiliate_payout_requests.insert_one({
            "affiliate_id": aff_id,
            "affiliate_code": "MI77QK",
            "user_id": aff.get("user_id"),
            "user_email": ADMIN_EMAIL,
            "amount_usd": 75.0,
            "status": "pending",
            "_test_iter21": True,
        }).inserted_id

        # Hit the process endpoint
        r = admin_session.post(
            f"{API}/admin/affiliate/payout-requests/{req_id}/process", timeout=15
        )
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

        # Verify in MongoDB
        req_doc = mongo_db.affiliate_payout_requests.find_one({"_id": req_id})
        assert req_doc["status"] == "paid"
        assert req_doc.get("processed_at")
        assert req_doc.get("processed_by") == ADMIN_EMAIL

        comm_doc = mongo_db.affiliate_commissions.find_one({"_id": comm_id})
        assert comm_doc["status"] == "paid"
        assert comm_doc.get("payout_request_id") == str(req_id)

        aff_after = mongo_db.affiliates.find_one({"_id": aff["_id"]})
        assert float(aff_after.get("unpaid_balance_usd") or 0.0) == 0.0

        # Cleanup
        mongo_db.affiliate_commissions.delete_many({"_test_iter21": True})
        mongo_db.affiliate_payout_requests.delete_many({"_test_iter21": True})


# ---------- COPILOT SMOKE ---------------------------------------------------

class TestCopilotSmoke:
    def test_copilot_chat_returns_session_answer_snapshot(self, admin_session):
        r = admin_session.post(
            f"{API}/copilot/chat",
            json={"message": "What's my current bot status?"},
            timeout=60,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "session_id" in body and body["session_id"]
        assert "answer" in body and isinstance(body["answer"], str) and len(body["answer"]) > 0
        assert "snapshot" in body and isinstance(body["snapshot"], dict)
        assert "bot_config" in body["snapshot"]
        assert "recent_signals" in body["snapshot"]

        # Second message in same session must succeed
        sid = body["session_id"]
        time.sleep(0.5)
        r2 = admin_session.post(
            f"{API}/copilot/chat",
            json={"message": "And the last signal?", "session_id": sid},
            timeout=60,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["session_id"] == sid
