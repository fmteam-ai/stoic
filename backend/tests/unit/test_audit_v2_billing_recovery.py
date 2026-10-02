"""Audit v2 billing — (1) trial retry never applies a later offer; (2) checkout
crash recovery: deterministic idempotency key, row-first ledger, session reuse,
and webhook recovery / orphan alerting for unknown paid sessions.

Uses an in-memory async Mongo stand-in (only the operators the billing code
uses) — no live database."""
import asyncio
import copy
from datetime import datetime, timezone
import re
import sys
import types

import pytest

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

_MISSING = object()


def _get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _cond(val, spec):
    if isinstance(spec, dict) and spec and all(k.startswith("$") for k in spec):
        for op, arg in spec.items():
            v = None if val is _MISSING else val
            if op == "$ne" and v == arg:
                return False
            if op == "$exists" and (val is not _MISSING) != bool(arg):
                return False
            if op == "$lt" and not (v is not None and v < arg):
                return False
            if op == "$in" and v not in arg:
                return False
            if op == "$regex" and not (isinstance(v, str) and re.search(arg, v)):
                return False
        return True
    return (None if val is _MISSING else val) == spec


def match(doc, flt):
    for k, spec in (flt or {}).items():
        if k == "$or":
            if not any(match(doc, f) for f in spec):
                return False
        elif not _cond(_get(doc, k), spec):
            return False
    return True


def _set(doc, path, value):
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


class _Res:
    def __init__(self, modified=0, inserted_id=None, upserted_id=None):
        self.modified_count = modified
        self.matched_count = modified
        self.inserted_id = inserted_id
        self.upserted_id = upserted_id


class FakeColl:
    def __init__(self, unique=()):
        self.docs = []
        self.unique = tuple(unique)

    def _check_unique(self, doc, skip=None):
        for field in self.unique:
            v = doc.get(field)
            if v is None:
                continue
            for d in self.docs:
                if d is not skip and d.get(field) == v:
                    raise DuplicateKeyError(f"dup {field}={v}")

    async def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if match(d, flt):
                return copy.deepcopy(d)
        return None

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", ObjectId())
        self._check_unique(doc)
        self.docs.append(doc)
        return _Res(inserted_id=doc["_id"])

    def _apply(self, d, upd, inserting=False):
        for path, v in (upd.get("$set") or {}).items():
            _set(d, path, copy.deepcopy(v))
        for path, v in (upd.get("$inc") or {}).items():
            _set(d, path, (_get(d, path) if _get(d, path) is not _MISSING else 0) + v)
        for path, v in (upd.get("$push") or {}).items():
            cur = _get(d, path)
            _set(d, path, (cur if cur is not _MISSING else []) + [v])
        if inserting:
            for path, v in (upd.get("$setOnInsert") or {}).items():
                _set(d, path, copy.deepcopy(v))

    async def update_one(self, flt, upd, upsert=False):
        for d in self.docs:
            if match(d, flt):
                before = copy.deepcopy(d)
                self._apply(d, upd)
                try:
                    self._check_unique(d, skip=d)
                except DuplicateKeyError:
                    d.clear()
                    d.update(before)
                    raise
                return _Res(modified=1)
        if upsert:
            d = {k: v for k, v in flt.items() if not k.startswith("$") and not isinstance(v, dict)}
            d["_id"] = ObjectId()
            self._apply(d, upd, inserting=True)
            self._check_unique(d)
            self.docs.append(d)
            return _Res(upserted_id=d["_id"])
        return _Res()

    async def find_one_and_update(self, flt, upd, upsert=False, return_document=False):
        for d in self.docs:
            if match(d, flt):
                before = copy.deepcopy(d)
                self._apply(d, upd)
                return copy.deepcopy(d) if return_document else before
        return None


class FakeDB:
    def __init__(self):
        self._c = {"payment_transactions": FakeColl(unique=("session_id", "idempotency_key")),
                   "orphan_payments": FakeColl(unique=("session_id",)),
                   "subscriptions": FakeColl(unique=("user_id",))}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, FakeColl())


def install_emergent_stub(monkeypatch):
    """The payments wrapper isn't installed in unit envs — provide inert classes."""
    try:
        import emergentintegrations.payments.stripe.checkout  # noqa: F401
        return
    except ImportError:
        pass

    class CheckoutSessionRequest:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mods = {}
    for name in ("emergentintegrations", "emergentintegrations.payments",
                 "emergentintegrations.payments.stripe", "emergentintegrations.payments.stripe.checkout"):
        mods[name] = sys.modules.get(name) or types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, mods[name])
    leaf = mods["emergentintegrations.payments.stripe.checkout"]
    for cls in ("StripeCheckout", "CheckoutSessionResponse", "CheckoutStatusResponse"):
        if not hasattr(leaf, cls):
            setattr(leaf, cls, type(cls, (), {}))
    if not hasattr(leaf, "CheckoutSessionRequest") or not callable(getattr(leaf, "CheckoutSessionRequest")):
        leaf.CheckoutSessionRequest = CheckoutSessionRequest


pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def env(monkeypatch):
    import plan_settings
    import subscription_service as ss
    import subscription_plans as sp
    import alerting
    db = FakeDB()
    monkeypatch.setattr(ss, "get_db", lambda: db)
    alerts = []

    async def _alert(_db, kind, severity, message, dedup_key=None, meta=None, synthetic=False):
        alerts.append({"kind": kind, "severity": severity, "dedup_key": dedup_key, "meta": meta})
        return "a1"
    monkeypatch.setattr(alerting, "raise_alert", _alert)

    async def _fresh(_db):
        return plan_settings._state["pricing_version"]
    monkeypatch.setattr(plan_settings, "ensure_fresh", _fresh)
    saved = (dict(plan_settings._state), sp.PRICING_VERSION)
    plan_settings._state.update({"pricing_version": 7, "trial_days": 15, "trial_tier": "trader",
                                 "trial_enabled_at": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(sp, "PRICING_VERSION", 7)
    yield types.SimpleNamespace(db=db, ss=ss, sp=sp, ps=plan_settings, alerts=alerts)
    plan_settings._state.clear()
    plan_settings._state.update(saved[0])


# ─────────────────────────────── 1. trial retry ───────────────────────────────

def _pending_user(env, dec):
    uid = ObjectId()
    created = datetime.now(timezone.utc)
    env.db.users.docs.append({"_id": uid, "email": "u@x", "role": "user",
                              "created_at": created.isoformat(), "trial_decision": dec})
    return uid


def test_trial_failure_records_signup_offer_version(env, monkeypatch):
    async def _boom(_db):
        raise RuntimeError("mongo hiccup")
    monkeypatch.setattr(env.ps, "ensure_fresh", _boom)
    dec = _run(env.ss.decide_trial_at_signup(env.db, datetime.now(timezone.utc)))
    assert dec["status"] == "pending_error" and dec["offer_version"] == 7 and dec["attempts"] == 1


def test_trial_failure_with_no_loaded_snapshot_records_unknown(env, monkeypatch):
    async def _boom(_db):
        raise RuntimeError("x")
    monkeypatch.setattr(env.ps, "ensure_fresh", _boom)
    env.ps._state["pricing_version"] = 0
    dec = _run(env.ss.decide_trial_at_signup(env.db, datetime.now(timezone.utc)))
    assert dec["status"] == "pending_error" and dec["offer_version"] is None


def _pending_dec(version):
    now = datetime.now(timezone.utc).isoformat()
    return {"status": "pending_error", "offer_version": version, "created_at": now,
            "decided_at": now, "attempts": 1}


@pytest.mark.parametrize("stored,reason", [(6, "offer_changed_since_signup"), (None, "offer_version_unknown")])
def test_trial_retry_with_changed_or_unknown_version_is_not_granted(env, stored, reason):
    uid = _pending_user(env, _pending_dec(stored))
    user = _run(env.db.users.find_one({"_id": uid}))
    new = _run(env.ss.retry_pending_trial_decision(env.db, user))
    u = _run(env.db.users.find_one({"_id": uid}))
    assert new["status"] == "pending_error" and new["review_reason"] == reason
    assert u["trial_decision"]["status"] == "pending_error" and u["trial_decision"]["admin_review_required"] is True
    assert "trial_grant" not in u
    assert u["trial_admin_review"]["required"] is True and u["trial_admin_review"]["reason"] == reason
    assert env.alerts and env.alerts[0]["kind"] == "trial_decision_review"
    # entitlement is NOT lazily evaluated against the current offer either
    assert env.ss._trial_grant(datetime.now(timezone.utc), u) is None


def test_trial_retry_with_matching_version_grants(env):
    uid = _pending_user(env, _pending_dec(7))
    user = _run(env.db.users.find_one({"_id": uid}))
    new = _run(env.ss.retry_pending_trial_decision(env.db, user))
    u = _run(env.db.users.find_one({"_id": uid}))
    assert new["status"] == "granted" and new["offer_version"] == 7
    assert u["trial_grant"]["tier"] == "trader" and u["trial_grant"]["days"] == 15
    assert "trial_admin_review" not in u and not env.alerts


# ─────────────────────────────── 2. checkout ───────────────────────────────

class _FakeStripe:
    """Mimics Stripe: with an idempotency key, a replay returns the same session."""
    def __init__(self, with_key=True):
        self.created, self.by_key, self.with_key = [], {}, with_key
        if not with_key:
            self.create_checkout_session = self._create_nokey

    async def create_checkout_session(self, req, idempotency_key=None):
        if idempotency_key in self.by_key:
            return self.by_key[idempotency_key]
        s = types.SimpleNamespace(session_id=f"cs_{len(self.created) + 1}", url=f"https://stripe.test/{len(self.created) + 1}",
                                  metadata=dict(req.metadata))
        self.created.append(s)
        if idempotency_key:
            self.by_key[idempotency_key] = s
        return s

    async def _create_nokey(self, req):
        return await _FakeStripe.create_checkout_session(self, req)


class _Req:
    base_url = "https://api.test/"

    def __init__(self, key=None):
        self.headers = {"Idempotency-Key": key} if key else {}


@pytest.fixture
def routes(env, monkeypatch):
    install_emergent_stub(monkeypatch)
    import importlib
    sr = importlib.import_module("routes.subscription_routes")

    class CSR:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    monkeypatch.setattr(sr, "CheckoutSessionRequest", CSR)
    monkeypatch.setattr(sr, "get_db", lambda: env.db)
    monkeypatch.setenv("CHECKOUT_ALLOWED_ORIGINS", "https://app.test")
    stripe = _FakeStripe()
    monkeypatch.setattr(sr, "_stripe_client", lambda host: stripe)
    return types.SimpleNamespace(sr=sr, stripe=stripe)


USER = {"id": str(ObjectId()), "email": "buyer@x", "role": "user"}
BODY = {"plan_id": "trader_monthly", "origin": "https://app.test"}


def test_deterministic_key_and_client_key_validation(env):
    plan = env.sp.get_plan("trader_monthly")
    k1 = env.sp.checkout_idempotency_key("u1", plan, 7, now=1000.0)
    assert k1 == env.sp.checkout_idempotency_key("u1", plan, 7, now=1100.0)        # same 10-min bucket
    assert k1 != env.sp.checkout_idempotency_key("u1", plan, 8, now=1000.0)        # pricing version
    assert k1 != env.sp.checkout_idempotency_key("u2", plan, 7, now=1000.0)        # user
    assert k1 != env.sp.checkout_idempotency_key("u1", plan, 7, now=1000.0 + 600)  # next bucket
    assert env.sp.checkout_idempotency_key("u1", plan, 7, client_key="abcdefgh-1") != \
        env.sp.checkout_idempotency_key("u2", plan, 7, client_key="abcdefgh-1")   # namespaced per user
    for bad in ("short", "x" * 129, "has space!!", "semi;colon00"):
        with pytest.raises(ValueError):
            env.sp.checkout_idempotency_key("u1", plan, 7, client_key=bad)


def test_bad_client_key_is_400(routes):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(routes.sr.create_checkout(BODY, _Req("bad key"), USER))
    assert ei.value.status_code == 400 and not routes.stripe.created


@pytest.mark.parametrize("client_key", [None, "client-key-0001"])
def test_retry_with_same_key_creates_one_stripe_session(env, routes, client_key):
    a = _run(routes.sr.create_checkout(BODY, _Req(client_key), USER))
    b = _run(routes.sr.create_checkout(BODY, _Req(client_key), USER))
    assert len(routes.stripe.created) == 1
    assert a["session_id"] == b["session_id"] == "cs_1" and b["reused"] is True
    assert a["idempotency_key"] == b["idempotency_key"]
    rows = env.db.payment_transactions.docs
    assert len(rows) == 1 and rows[0]["session_id"] == "cs_1" and rows[0]["payment_status"] == "initiated"
    assert rows[0]["metadata"]["idempotency_key"] == a["idempotency_key"]
    assert rows[0]["metadata"]["user_id"] == USER["id"] and rows[0]["metadata"]["plan_id"] == "trader_monthly"


def test_row_is_written_before_stripe_is_called(env, routes, monkeypatch):
    seen = {}
    orig = routes.stripe.create_checkout_session

    async def _spy(req, idempotency_key=None):
        seen["rows"] = copy.deepcopy(env.db.payment_transactions.docs)
        return await orig(req, idempotency_key=idempotency_key)
    monkeypatch.setattr(routes.stripe, "create_checkout_session", _spy)
    _run(routes.sr.create_checkout(BODY, _Req(), USER))
    assert len(seen["rows"]) == 1 and seen["rows"][0]["payment_status"] == "initiated"
    assert seen["rows"][0]["session_id"].startswith(env.ss.PENDING_SESSION_PREFIX)


def test_crash_after_stripe_create_then_retry_reuses_same_session(env, routes, monkeypatch):
    orig = env.ss.attach_checkout_session
    calls = {"n": 0}

    async def _crash_once(key, sid, url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process died between Stripe create and DB write")
        return await orig(key, sid, url)
    monkeypatch.setattr(env.ss, "attach_checkout_session", _crash_once)
    with pytest.raises(RuntimeError):
        _run(routes.sr.create_checkout(BODY, _Req(), USER))
    assert len(routes.stripe.created) == 1
    out = _run(routes.sr.create_checkout(BODY, _Req(), USER))
    assert len(routes.stripe.created) == 1                    # Stripe replayed the SAME session
    assert out["session_id"] == "cs_1" and out["checkout_url"] == "https://stripe.test/1"
    rows = env.db.payment_transactions.docs
    assert len(rows) == 1 and rows[0]["session_id"] == "cs_1" and rows[0]["stripe_create_attempts"] == 2


def test_terminal_intent_in_same_window_gets_a_fresh_key(env, routes):
    a = _run(routes.sr.create_checkout(BODY, _Req(), USER))
    env.db.payment_transactions.docs[0].update({"payment_status": "paid", "applied": True})
    b = _run(routes.sr.create_checkout(BODY, _Req(), USER))
    assert a["idempotency_key"] != b["idempotency_key"] and b["session_id"] == "cs_2"


def test_client_key_reused_for_other_plan_is_409(routes):
    from fastapi import HTTPException
    _run(routes.sr.create_checkout(BODY, _Req("client-key-0002"), USER))
    with pytest.raises(HTTPException) as ei:
        _run(routes.sr.create_checkout({**BODY, "plan_id": "professional_monthly"}, _Req("client-key-0002"), USER))
    assert ei.value.status_code == 409


# ─────────────────────────────── webhook recovery ───────────────────────────────

def _user(env):
    uid = ObjectId()
    env.db.users.docs.append({"_id": uid, "email": "w@x", "role": "user",
                              "created_at": datetime.now(timezone.utc).isoformat(),
                              "trial_decision": {"status": "not_eligible"}})
    return str(uid)


def test_paid_unknown_but_rebuildable_session_is_fulfilled_exactly_once(env):
    uid = _user(env)
    plan = env.sp.get_plan("trader_monthly")
    md = {"user_id": uid, "plan_id": "trader_monthly", "amount_minor": str(plan.amount_cents),
          "currency": "usd", "pricing_version": "7", "duration_months": "1", "idempotency_key": "ck_lost"}
    r1 = _run(env.ss.fulfil_paid_session("cs_lost", metadata=md, paid_amount_minor=plan.amount_cents, paid_currency="usd"))
    r2 = _run(env.ss.fulfil_paid_session("cs_lost", metadata=md, paid_amount_minor=plan.amount_cents, paid_currency="usd"))
    assert r1["outcome"] == "rebuilt" and r1["subscription"]["current_plan_id"] == "trader_monthly"
    assert r2["outcome"] == "not_applied"
    rows = [d for d in env.db.payment_transactions.docs if d["session_id"] == "cs_lost"]
    assert len(rows) == 1 and rows[0]["applied"] is True and rows[0]["rebuilt_from_metadata"] is True
    sub = env.db.subscriptions.docs[0]
    first_vu = sub["valid_until"]
    assert sub["last_session_id"] == "cs_lost"
    _run(env.ss.fulfil_paid_session("cs_lost", metadata=md, paid_amount_minor=plan.amount_cents, paid_currency="usd"))
    assert env.db.subscriptions.docs[0]["valid_until"] == first_vu       # never extended twice
    assert not env.db.orphan_payments.docs and not env.alerts


def test_paid_session_found_via_idempotency_key_rebinds_intent_row(env, routes):
    uid = _user(env)
    user = {"id": uid, "email": "w@x", "role": "user"}
    routes.stripe.__init__(with_key=False)
    routes.sr._stripe_client = lambda host: routes.stripe
    out = _run(routes.sr.create_checkout(BODY, _Req(), user))
    key = out["idempotency_key"]
    plan = env.sp.get_plan("trader_monthly")
    # a different (orphaned) session of the same intent was the one paid
    r = _run(env.ss.fulfil_paid_session("cs_other", metadata={"idempotency_key": key, "user_id": uid,
                                                              "plan_id": "trader_monthly"},
                                        paid_amount_minor=plan.amount_cents, paid_currency="usd"))
    assert r["outcome"] == "rebound" and r["subscription"]["current_plan_id"] == "trader_monthly"
    row = env.db.payment_transactions.docs[0]
    assert row["session_id"] == "cs_other" and row["applied"] is True and "cs_1" in row["superseded_session_ids"]
    # a second paid session for the already-fulfilled intent is a duplicate charge → orphan + alert
    r2 = _run(env.ss.fulfil_paid_session("cs_1", metadata={"idempotency_key": key}, paid_amount_minor=1))
    assert r2["outcome"] == "orphaned" and r2["reason"] == "duplicate_payment_for_intent"
    assert env.alerts[-1]["kind"] == "orphan_payment"


def test_paid_unknown_non_rebuildable_session_is_orphaned_and_alerted(env):
    r = _run(env.ss.fulfil_paid_session("cs_ghost", metadata={"foo": "bar"}, paid_amount_minor=999, paid_currency="usd"))
    assert r["outcome"] == "orphaned"
    orphans = env.db.orphan_payments.docs
    assert len(orphans) == 1 and orphans[0]["session_id"] == "cs_ghost" and orphans[0]["status"] == "open"
    assert orphans[0]["paid_amount_minor"] == 999
    assert env.alerts and env.alerts[0]["kind"] == "orphan_payment" and env.alerts[0]["severity"] == "critical"
    assert not env.db.payment_transactions.docs and not env.db.subscriptions.docs
    # unknown user id is also not rebuildable
    r = _run(env.ss.fulfil_paid_session("cs_ghost2", metadata={"user_id": str(ObjectId()), "plan_id": "trader_monthly"}))
    assert r["outcome"] == "orphaned" and r["reason"] == "user_not_found"


def test_webhook_routes_unknown_paid_session_through_recovery(env, routes, monkeypatch):
    calls = []

    async def _fulfil(sid, **kw):
        calls.append((sid, kw))
        return {"outcome": "orphaned"}
    monkeypatch.setattr(routes.sr, "fulfil_paid_session", _fulfil)

    class _Hook:
        async def handle_webhook(self, body, sig):
            return types.SimpleNamespace(event_type="checkout.session.completed", session_id="cs_x",
                                         payment_status="paid", metadata={"user_id": "forged"})

        async def get_checkout_status(self, sid):
            return types.SimpleNamespace(payment_status="paid", amount_total=100, currency="usd",
                                         metadata={"user_id": "real", "plan_id": "trader_monthly"})
    monkeypatch.setattr(routes.sr, "_stripe_client", lambda host: _Hook())

    class _R:
        base_url = "https://api.test/"
        headers = {}

        async def body(self):
            return b"{}"
    out = _run(routes.sr.stripe_webhook(_R()))
    assert out == {"ok": True}
    assert calls and calls[0][0] == "cs_x" and calls[0][1]["metadata"]["user_id"] == "real"
