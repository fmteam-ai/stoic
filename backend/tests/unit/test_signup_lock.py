"""Testing period — new member registrations and affiliate applications are closed by the admin toggle
(platform_state.signups) or SIGNUPS_CLOSED=true; existing users are unaffected."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Coll:
    def __init__(self):
        self.doc = None

    async def find_one(self, q, proj=None):
        return dict(self.doc) if self.doc and q.get("_id") == "signups" else None

    async def update_one(self, q, u, upsert=False):
        self.doc = {"_id": "signups", **(self.doc or {}), **u["$set"]}


class _Db:
    def __init__(self):
        self.platform_state = _Coll()


def test_toggle_closes_and_reopens_with_audit_fields_and_env_override():
    import signup_lock as sl
    db = _Db()
    with patch.dict(os.environ, {"SIGNUPS_CLOSED": ""}):
        assert _run(sl.is_closed(db)) is False
        s = _run(sl.set_closed(db, True, "admin@x", "Closed for the 4-week demo"))
        assert s["closed"] and s["updated_by"] == "admin@x" and s["message"] == "Closed for the 4-week demo"
        assert _run(sl.is_closed(db)) is True
        s = _run(sl.set_closed(db, False, "admin@x"))
        assert s["closed"] is False and _run(sl.is_closed(db)) is False
    with patch.dict(os.environ, {"SIGNUPS_CLOSED": "true"}):
        s = _run(sl.state(db))
        assert s["closed"] and s["env_closed"] and s["db_closed"] is False   # env forces closed, toggle cannot reopen
    exc = sl.closed_http_exception("member")
    assert exc.status_code == 403 and exc.detail["code"] == "signups_closed" and exc.detail["kind"] == "member"


def test_register_and_affiliate_apply_are_gated_before_any_side_effect():
    auth = open(os.path.join(ROOT, "backend", "routes", "auth_routes.py"), encoding="utf-8").read()
    reg = auth[auth.index('@router.post("/register")'):]
    assert reg.index("signup_lock.is_closed") < reg.index("require_turnstile") < reg.index("rate_limit(")
    assert '@router.get("/signups-status")' in auth
    aff = open(os.path.join(ROOT, "backend", "routes", "affiliate_routes.py"), encoding="utf-8").read()
    body = aff[aff.index('@router.post("/affiliate/apply")'):]
    assert body.index("signup_lock.is_closed") < body.index("_require_active_subscription")
    adm = open(os.path.join(ROOT, "backend", "routes", "admin_routes.py"), encoding="utf-8").read()
    assert '@router.post("/admin/settings/signups")' in adm and 'action="signups_" +' in adm
    for f, tid in (("Register.jsx", "register-closed"), ("Affiliate.jsx", "affiliate-closed"), ("AdminUsers.jsx", "signups-toggle")):
        assert tid in open(os.path.join(ROOT, "frontend", "src", "pages", f), encoding="utf-8").read(), f
