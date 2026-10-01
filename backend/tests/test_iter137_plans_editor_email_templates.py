"""iter-137 — Admin → Integrations: Stripe plans editor (pricing / discounts /
currency / free trial) + transactional e-mail template catalog.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import subscription_plans as sp
import plan_settings


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


@pytest.fixture(autouse=True)
def _restore_catalog():
    snap = (dict(sp.TIER_BASE_CENTS), {d: x for d, _m, x in sp.DURATION_DISCOUNTS}, sp.CURRENCY, dict(plan_settings._state))
    yield
    sp.apply_pricing(snap[0], snap[1], snap[2])
    plan_settings._state.update(snap[3])


# ---------------------------------------------------------------- catalog override

def test_apply_pricing_rebuilds_matrix_in_place():
    plans_ref = sp.PLANS
    sp.apply_pricing({"starter": 4900}, {"annual": 50}, "eur")
    assert sp.PLANS is plans_ref
    p = sp.get_plan("starter_annual")
    assert p.amount_cents == round(4900 * 12 * 0.5)
    assert sp.get_plan("trader_monthly").amount_cents == 9900          # untouched tiers keep defaults
    pub = sp.get_plan("starter_monthly").to_public()
    assert pub["currency"] == "eur" and pub["currency_symbol"] == "€"
    assert sp.PLANS["starter_annual"].description.startswith("Save 50%")


def test_validate_rejects_bad_input():
    with pytest.raises(ValueError):
        plan_settings.validate({"base_cents": {"starter": 5000, "trader": 4900}})      # non-monotonic
    with pytest.raises(ValueError):
        plan_settings.validate({"discounts": {"annual": 95}})
    with pytest.raises(ValueError):
        plan_settings.validate({"currency": "xyz"})
    with pytest.raises(ValueError):
        plan_settings.validate({"trial_days": 500})
    with pytest.raises(ValueError):
        plan_settings.validate({"trial_tier": "gold"})
    clean = plan_settings.validate({"discounts": {"monthly": 30}})
    assert clean["discounts"]["monthly"] == 0                                           # monthly is always full price


def test_update_persists_applies_and_audits():
    async def _t():
        db = _db()
        before = await db.admin_audit_log.count_documents({"action": "plan_pricing_update"})
        out = await plan_settings.update(db, {"base_cents": {"starter": 4500, "trader": 9900, "professional": 19900,
                                                             "elite_ai": 39900}, "currency": "gbp", "trial_days": 7,
                                              "trial_tier": "professional"}, {"email": "t@test.local"})
        assert out["currency"] == "gbp" and out["base_cents"]["starter"] == 4500
        assert sp.get_plan("starter_monthly").amount_cents == 4500 and sp.CURRENCY == "gbp"
        cfg = plan_settings.trial_config()
        assert {k: cfg[k] for k in ("days", "tier", "enabled_at")} == {"days": 7, "tier": "professional", "enabled_at": out["trial_enabled_at"]}
        assert cfg["offer_version"] == out["pricing_version"] >= 1
        doc = await db.platform_state.find_one({"_id": plan_settings.DOC_ID})
        assert doc["base_cents"]["starter"] == 4500 and doc["trial_enabled_at"]
        assert await db.admin_audit_log.count_documents({"action": "plan_pricing_update"}) == before + 1
        # reload from DB reproduces the same catalog
        sp.apply_pricing({}, {}, "usd")
        assert sp.get_plan("starter_monthly").amount_cents == 3900
        assert await plan_settings.load(db)
        assert sp.get_plan("starter_monthly").amount_cents == 4500
        # disabling the trial clears its window; re-enabling starts a fresh one
        out2 = await plan_settings.update(db, {**out, "trial_days": 0}, {"email": "t@test.local"})
        assert out2["trial_enabled_at"] is None
        out3 = await plan_settings.update(db, {**out2, "trial_days": 15}, {"email": "t@test.local"})
        assert out3["trial_enabled_at"] and out3["trial_enabled_at"] >= out["trial_enabled_at"]
        # restore defaults in DB
        await plan_settings.update(db, {**plan_settings.current()["defaults"]}, {"email": "t@test.local"})
    _run(_t())


# ---------------------------------------------------------------- free trial

def test_trial_grant_only_for_new_signups():
    from subscription_service import _trial_grant
    now = datetime.now(timezone.utc)
    plan_settings._state.update({"trial_days": 15, "trial_tier": "trader",
                                 "trial_enabled_at": (now - timedelta(days=1)).isoformat()})
    g = _trial_grant(now)
    assert g["current_plan_id"] == "trial_trader" and g["trial"]["days"] == 15
    assert _trial_grant(now - timedelta(days=2)) is None            # created before trial enabled
    assert _trial_grant(now - timedelta(days=20)) is None           # would already be expired
    assert _trial_grant(None) is None
    plan_settings._state.update({"trial_days": 0})
    assert _trial_grant(now) is None


def test_new_user_gets_trial_tier_and_purchase_extends_after_it():
    from subscription_service import get_subscription, get_user_tier, is_active
    from bson import ObjectId
    now = datetime.now(timezone.utc)
    plan_settings._state.update({"trial_days": 15, "trial_tier": "trader",
                                 "trial_enabled_at": (now - timedelta(hours=1)).isoformat()})

    async def _t():
        db = _db()
        uid = ObjectId()
        await db.users.insert_one({"_id": uid, "email": f"trial-{uid}@test.local", "role": "user",
                                   "created_at": now.isoformat()})
        try:
            sub = await get_subscription(str(uid))
            assert sub["current_plan_id"] == "trial_trader"
            assert await get_user_tier(str(uid)) == "trader"
            state = await is_active(str(uid))
            assert state["active"] and state["plan_id"] == "trial_trader"
            # a checkout while on trial is treated as a NEW purchase that starts after the trial
            from subscription_plans import get_plan
            assert get_plan("trial_trader") is None
        finally:
            await db.users.delete_one({"_id": uid})
            await db.subscriptions.delete_many({"user_id": str(uid)})
    _run(_t())


# ---------------------------------------------------------------- e-mail templates

def test_email_template_catalog_renders_every_entry():
    import email_templates
    cat = email_templates.catalog()
    assert {c["id"] for c in cat} == set(email_templates.REGISTRY)
    for c in cat:
        r = email_templates.render(c["id"])
        assert r["subject"] and "<" in r["html"] and "STOIC" in r["html"]
    with pytest.raises(KeyError):
        email_templates.render("nope")


def test_email_template_send_test_prefixes_subject(monkeypatch):
    import email_templates, email_sender
    sent = {}

    async def _fake(recipient, subject, html, text=None, sender=None, idempotency_key=None):
        sent.update(to=recipient, subject=subject)
        return {"ok": True, "id": "x"}
    monkeypatch.setattr(email_sender, "send_email", _fake)
    monkeypatch.setattr(email_sender, "is_configured", lambda: True)
    res = _run(email_templates.send_test("login_otp", "ops@test.local"))
    assert res["ok"] and sent["to"] == "ops@test.local" and sent["subject"].startswith("[PREVIEW] ")
    monkeypatch.setattr(email_sender, "is_configured", lambda: False)
    assert _run(email_templates.send_test("login_otp", "ops@test.local"))["ok"] is False
