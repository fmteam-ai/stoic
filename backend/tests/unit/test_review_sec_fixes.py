"""Review-sec fixes: apply-suggestion validation + live gate, Telegram webhook
secret-token header + /run refusal, NL confirm step-up + strategy validation,
WebAuthn origin derivation. Pure mocks — no Mongo, no network."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

pytestmark = pytest.mark.unit

UID = str(ObjectId())


def _run(coro):
    return asyncio.run(coro)


class _Req:
    def __init__(self, headers=None, body=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body

    async def json(self):
        return self._body


class _HeaderDict(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def _req(headers=None, body=None):
    r = _Req(headers, body)
    r.headers = _HeaderDict(r.headers)
    return r


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return list(self._docs)


def _step_up_denied():
    return AsyncMock(side_effect=HTTPException(
        status_code=403, detail={"code": "step_up_required"}))


# ----------------------------------------------------------- item 2
def test_suggestion_patch_validation_rejects_bad_values():
    from routes.safety_blocks_routes import _validate_suggestion_patch as v
    for bad in ({"risk_level": "yolo"}, {"risk_level": 3},
                {"max_concurrent_trades": 0}, {"max_concurrent_trades": 11},
                {"max_concurrent_trades": "5"}, {"max_concurrent_trades": True},
                {"max_lot_size": 0}, {"max_lot_size": -1}, {"max_lot_size": 1e9},
                {"max_lot_size": float("nan")}, {"active": "true"}):
        with pytest.raises(HTTPException) as e:
            v(bad)
        assert e.value.status_code == 400, bad
    assert v({"risk_level": "LOW", "max_concurrent_trades": 2,
              "max_lot_size": 0.3, "active": False, "evil": 1}) == {
        "risk_level": "low", "max_concurrent_trades": 2,
        "max_lot_size": 0.3, "active": False}


def _sb_db(cfg, live_accounts=1):
    db = MagicMock()
    db.bot_configs.find_one = AsyncMock(return_value=cfg)
    db.bot_configs.update_one = AsyncMock()
    db.accounts.count_documents = AsyncMock(return_value=live_accounts)
    return db


def test_apply_suggestion_without_scope_targets_default_config():
    import routes.safety_blocks_routes as sb
    cfg = {"_id": ObjectId(), "user_id": UID, "account_id": None,
           "risk_level": "high", "active": True}
    db = _sb_db(cfg)
    with patch.object(sb, "get_db", return_value=db):
        out = _run(sb.apply_suggestion({"patch": {"risk_level": "medium"}},
                                       _req(), user={"id": UID}))
    assert out["applied"] is True
    q = db.bot_configs.find_one.await_args_list[0].args[0]
    assert q["user_id"] == UID and "$or" in q            # default scope, not "first doc"
    assert db.bot_configs.update_one.await_args.args[0] == {"_id": cfg["_id"]}


def test_apply_suggestion_activation_on_live_requires_step_up():
    import routes.safety_blocks_routes as sb
    cfg = {"_id": ObjectId(), "user_id": UID, "risk_level": "low", "active": False}
    db = _sb_db(cfg, live_accounts=1)
    with patch.object(sb, "get_db", return_value=db), \
         patch("step_up.require_step_up", _step_up_denied()):
        with pytest.raises(HTTPException) as e:
            _run(sb.apply_suggestion({"patch": {"active": True}}, _req(),
                                     user={"id": UID}))
    assert e.value.status_code == 403
    db.bot_configs.update_one.assert_not_awaited()


def test_apply_suggestion_risk_raise_on_live_requires_step_up():
    import routes.safety_blocks_routes as sb
    cfg = {"_id": ObjectId(), "user_id": UID, "risk_level": "low",
           "max_lot_size": 0.5, "active": True}
    db = _sb_db(cfg, live_accounts=1)
    for patch_body in ({"risk_level": "extreme"}, {"max_lot_size": 5.0}):
        with patch.object(sb, "get_db", return_value=db), \
             patch("step_up.require_step_up", _step_up_denied()):
            with pytest.raises(HTTPException):
                _run(sb.apply_suggestion({"patch": patch_body}, _req(),
                                         user={"id": UID}))
    db.bot_configs.update_one.assert_not_awaited()


def test_apply_suggestion_risk_reduction_needs_no_step_up():
    import routes.safety_blocks_routes as sb
    cfg = {"_id": ObjectId(), "user_id": UID, "risk_level": "high",
           "max_lot_size": 0.5, "active": True}
    db = _sb_db(cfg, live_accounts=1)
    su = _step_up_denied()
    with patch.object(sb, "get_db", return_value=db), patch("step_up.require_step_up", su):
        _run(sb.apply_suggestion({"patch": {"max_lot_size": 0.35, "active": False}},
                                 _req(), user={"id": UID}))
    su.assert_not_awaited()
    db.bot_configs.update_one.assert_awaited()


# ----------------------------------------------------------- item 3
def test_telegram_header_secret_check():
    from routes.telegram_routes import _header_secret_ok, TELEGRAM_SECRET_HEADER
    doc = {"webhook_header_secret": "abc_DEF-123"}
    assert _header_secret_ok(doc, _req({TELEGRAM_SECRET_HEADER: "abc_DEF-123"}))
    assert not _header_secret_ok(doc, _req({TELEGRAM_SECRET_HEADER: "abc_DEF-124"}))
    assert not _header_secret_ok(doc, _req({}))
    # legacy registration (no header secret stored) keeps working on the
    # path secret so /panic and /close don't silently break; /run is gated
    # separately by _run_refusal.
    assert _header_secret_ok({}, _req({}))


def test_telegram_webhook_ignores_update_with_bad_header():
    import routes.telegram_routes as tg
    user_doc = {"user_id": UID, "webhook_secret": "path",
                "webhook_header_secret": "hdr", "telegram_bot_token": "enc",
                "telegram_chat_id": "42"}
    body = {"message": {"chat": {"id": 42}, "text": "/run"}}
    dispatch = AsyncMock()
    with patch.object(tg, "_load_user_by_secret", AsyncMock(return_value=user_doc)), \
         patch.object(tg, "_dispatch_command", dispatch), \
         patch.object(tg, "vault_decrypt", return_value="tok"):
        _run(tg.telegram_webhook("path", _req({}, body)))
        _run(tg.telegram_webhook("path", _req({tg.TELEGRAM_SECRET_HEADER: "nope"}, body)))
        dispatch.assert_not_awaited()
        _run(tg.telegram_webhook("path", _req({tg.TELEGRAM_SECRET_HEADER: "hdr"}, body)))
    dispatch.assert_awaited_once()


def _tg_db(status="active", cfgs=(), live_accounts=0, acct_mode="live"):
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": ObjectId(UID), "status": status})
    db.bot_configs.find = MagicMock(return_value=_Cursor(cfgs))
    db.bot_configs.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
    db.accounts.count_documents = AsyncMock(return_value=live_accounts)
    db.accounts.find_one = AsyncMock(return_value={"mode": acct_mode})
    return db


def test_telegram_run_refused_for_live_and_suspended():
    import routes.telegram_routes as tg
    acct = str(ObjectId())
    cases = [
        _tg_db(cfgs=[{"account_id": acct}], acct_mode="live"),
        _tg_db(cfgs=[{"account_id": None}], live_accounts=1),
        _tg_db(status="suspended", cfgs=[{"account_id": acct}], acct_mode="paper"),
    ]
    for db in cases:
        reply = AsyncMock()
        with patch.object(tg, "get_db", return_value=db), patch.object(tg, "_send_reply", reply):
            _run(tg._cmd_run("tok", "42", UID))
        db.bot_configs.update_many.assert_not_awaited()
        reply.assert_awaited_once()


def test_telegram_run_allowed_for_paper_only():
    import routes.telegram_routes as tg
    db = _tg_db(cfgs=[{"account_id": str(ObjectId())}], acct_mode="paper")
    with patch.object(tg, "get_db", return_value=db), patch.object(tg, "_send_reply", AsyncMock()):
        _run(tg._cmd_run("tok", "42", UID))
    db.bot_configs.update_many.assert_awaited_once()


# ----------------------------------------------------------- item 4
def test_compiled_strategy_validation():
    from routes.nl_routes import _validate_compiled_strategy as v
    out = v({"risk_level": "High", "symbols": ["xauusd", "BTC/USD"],
             "max_concurrent_trades": 99})
    assert out["risk_level"] == "high"
    assert out["symbols"] == ["XAUUSD", "BTCUSD"]
    assert out["max_concurrent_trades"] == 10
    assert out["auto_execute"] is False                     # absent → False
    assert v({"max_concurrent_trades": -4})["max_concurrent_trades"] == 1
    assert v({"auto_execute": "yes"})["auto_execute"] is False
    assert v({"auto_execute": True})["auto_execute"] is True
    for bad in ({"risk_level": "yolo"}, {"symbols": "XAUUSD"}, {"symbols": [1]},
                {"symbols": ["$(rm -rf)"]}, {"symbols": []},
                {"max_concurrent_trades": "many"}):
        with pytest.raises(HTTPException) as e:
            v(bad)
        assert e.value.status_code == 400, bad


def _nl_db(cfgs, live_accounts=1):
    db = MagicMock()
    db.bot_configs.find = MagicMock(side_effect=lambda *a, **k: _Cursor(cfgs))
    db.accounts.count_documents = AsyncMock(return_value=live_accounts)
    db.accounts.find_one = AsyncMock(return_value={"mode": "live"})
    return db


def test_nl_risk_increasing_detection():
    from routes.nl_routes import _nl_risk_increasing_live as f
    inactive = [{"_id": ObjectId(), "account_id": None, "active": False, "risk_level": "low"}]
    active_high = [{"_id": ObjectId(), "account_id": None, "active": True, "risk_level": "high"}]
    assert _run(f(_nl_db(inactive), UID, [{"type": "ENABLE_BOTS", "target": "all"}]))
    assert not _run(f(_nl_db(active_high), UID, [{"type": "ENABLE_BOTS", "target": "all"}]))
    assert _run(f(_nl_db(active_high), UID, [{"type": "SET_RISK_LEVEL", "target": "all",
                                             "params": {"risk_level": "extreme"}}]))
    assert not _run(f(_nl_db(active_high), UID, [{"type": "SET_RISK_LEVEL", "target": "all",
                                                 "params": {"risk_level": "low"}}]))
    # paper-only context → no step-up
    assert not _run(f(_nl_db(inactive, live_accounts=0), UID,
                      [{"type": "ENABLE_BOTS", "target": "all"}]))
    # arming a trigger that would enable bots later
    assert _run(f(_nl_db([]), UID, [{"type": "SET_CONDITIONAL_TRIGGER", "target": "all",
                                     "params": {"then": [{"type": "ENABLE_BOTS"}]}}]))
    assert not _run(f(_nl_db(inactive), UID, [{"type": "DISABLE_BOTS", "target": "all"},
                                              {"type": "PANIC_LOCK"}]))


def test_nl_confirm_requires_step_up_before_claim():
    import routes.nl_routes as nl
    import nl_execution as nx
    pid = str(ObjectId())
    db = _nl_db([{"_id": ObjectId(), "account_id": None, "active": False,
                  "risk_level": "low"}])
    db.nl_proposals.find_one = AsyncMock(return_value={
        "_id": ObjectId(pid), "user_id": UID, "status": "pending",
        "actions": [{"type": "ENABLE_BOTS", "target": "all"}]})
    claim = AsyncMock()
    with patch.object(nl, "get_db", return_value=db), \
         patch("step_up.require_step_up", _step_up_denied()), \
         patch.object(nx, "claim", claim):
        with pytest.raises(HTTPException) as e:
            _run(nl.nl_command_confirm({"proposal_id": pid}, _req(), user={"id": UID}))
    assert e.value.status_code == 403
    claim.assert_not_awaited()                     # proposal stays pending for the retry


def test_nl_confirm_risk_reducing_skips_step_up():
    import routes.nl_routes as nl
    import nl_execution as nx
    pid = str(ObjectId())
    db = _nl_db([{"_id": ObjectId(), "account_id": None, "active": True,
                  "risk_level": "high"}])
    db.nl_proposals.find_one = AsyncMock(return_value={
        "_id": ObjectId(pid), "user_id": UID, "status": "pending",
        "actions": [{"type": "DISABLE_BOTS", "target": "all"}]})
    su = _step_up_denied()
    with patch.object(nl, "get_db", return_value=db), \
         patch("step_up.require_step_up", su), \
         patch("nl_preview.is_expired", return_value=False), \
         patch.object(nx, "claim", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as e:
            _run(nl.nl_command_confirm({"proposal_id": pid}, _req(), user={"id": UID}))
    assert e.value.status_code == 409               # reached the claim
    su.assert_not_awaited()


# ----------------------------------------------------------- item 1
def test_webauthn_origin_ignores_body_and_uses_config(monkeypatch):
    from routes.webauthn_routes import _origin
    monkeypatch.setenv("WEBAUTHN_ORIGIN", "https://app.example.com/")
    r = _req({"origin": "https://evil.test"})
    assert _origin(r, {"origin": "https://evil.test"}) == "https://app.example.com"
    monkeypatch.delenv("WEBAUTHN_ORIGIN")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    assert _origin(_req({"origin": "https://evil.test"}), {"origin": "https://app.example.com"}) == ""
    assert _origin(_req({"origin": "https://app.example.com"}), None) == "https://app.example.com"


def test_passkey_enrolment_requires_step_up_or_password():
    import routes.webauthn_routes as wr
    from auth import hash_password
    user = {"id": UID, "role": "admin"}
    # TOTP enrolled → step-up required
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"two_factor_enabled": True})
    with patch("step_up.require_step_up", _step_up_denied()):
        with pytest.raises(HTTPException) as e:
            _run(wr._require_enrolment_proof(db, user, _req(), {}))
    assert e.value.status_code == 403
    # no factor yet → current password required and verified
    db.users.find_one = AsyncMock(return_value={"password_hash": hash_password("right-pw")})
    db.webauthn_credentials.count_documents = AsyncMock(return_value=0)
    with patch.object(wr, "check_failure_limit", AsyncMock()), \
         patch.object(wr, "record_failure", AsyncMock()) as rf, \
         patch.object(wr, "clear_failures", AsyncMock()) as cf:
        with pytest.raises(HTTPException) as e:
            _run(wr._require_enrolment_proof(db, user, _req(), {"current_password": "wrong"}))
        assert e.value.status_code == 401 and rf.await_count == 1
        _run(wr._require_enrolment_proof(db, user, _req(), {"current_password": "right-pw"}))
        assert cf.await_count == 1
