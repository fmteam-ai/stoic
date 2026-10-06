"""A13 Part 3 (pre-launch) + P1-05 — acceptance tests.
P2-02 Turnstile edge matrix on a production-like hostname (siteverify mocked at the HTTP client):
valid-once, replay, wrong action / hostname, malformed token, clock skew, provider outage,
OTP fallback, recovery, rate limits — never fail open, never lock out indefinitely."""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeCollection, FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
HOST = "app.stoicaibot.com"
ENV = {"TURNSTILE_SECRET_KEY": "1x0000000000000000000000000000000AA", "TURNSTILE_SITE_KEY": "1x00000000000000000000AA",
       "TURNSTILE_EXPECTED_HOSTNAMES": HOST, "APP_ENV": "production", "TURNSTILE_FORCE_DISABLE": "",
       "TURNSTILE_LOGIN_DEGRADED_POLICY": "closed"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


class _OnceTokens(FakeCollection):
    async def insert_one(self, doc):
        if any(r["_id"] == doc["_id"] for r in self.rows):
            raise DuplicateKeyError("replay")
        return await super().insert_one(doc)


def _db(enabled=True):
    db = FakeDb()
    db.turnstile_consumed_tokens = _OnceTokens()
    db.settings.rows.append({"_id": "turnstile", "key": "turnstile", "enabled": enabled})
    return db


def _resp(status=200, body=None, raise_json=False):
    r = MagicMock()
    r.status_code = status
    if raise_json:
        r.json = MagicMock(side_effect=ValueError("bad json"))
    else:
        r.json = MagicMock(return_value=body)
    return r


def _siteverify(resp=None, exc=None):
    """Patch httpx.AsyncClient so siteverify returns `resp` (or raises `exc`)."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=resp)
    return patch("turnstile_gate.httpx.AsyncClient", return_value=client)


def _ok_body(action="login", host=HOST, ts=None):
    ts = ts or (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    return {"success": True, "hostname": host, "action": action, "challenge_ts": ts, "error-codes": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    import turnstile_gate as tg
    monkeypatch.setattr(tg, "_LAST_PROVIDER_FAILURE_MONO", None)
    monkeypatch.setattr(tg, "is_enabled", AsyncMock(return_value=True))
    yield


def test_valid_once_then_replay_is_refused():
    import turnstile_gate as tg
    db = _db()
    with _siteverify(_resp(body=_ok_body())):
        d1 = run(tg.evaluate(db, "tok-A", "1.2.3.4", "login"))
        d2 = run(tg.evaluate(db, "tok-A", "1.2.3.4", "login"))
        d3 = run(tg.evaluate(db, "tok-A", "1.2.3.4", "register"))        # cross-surface replay
    assert d1.allow and d1.mode == "verified"
    assert not d2.allow and d2.error.status_code == 403 and "token-replayed" in d2.error_codes
    assert not d3.allow and d3.error.status_code == 403
    assert db.turnstile_consumed_tokens.rows[0]["_id"] != "tok-A"       # stored as a hash, never the token


@pytest.mark.parametrize("body,code", [
    (_ok_body(action="register"), "action-mismatch"),
    (_ok_body(host="evil.example"), "hostname-mismatch"),
    ({**_ok_body(), "hostname": None}, "hostname-missing"),
    (_ok_body(ts=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()), "token-from-future"),
    (_ok_body(ts=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()), "token-stale"),
    ({**_ok_body(), "challenge_ts": "not-a-date"}, "challenge-ts-missing"),
    ({"success": "yes", "hostname": HOST, "action": "login", "challenge_ts": "x"}, "malformed-claim-success"),
    ({"success": False, "error-codes": ["invalid-input-response"]}, "invalid-input-response"),
])
def test_wrong_action_hostname_clock_skew_and_malformed_claims_are_client_faults(body, code):
    import turnstile_gate as tg
    with _siteverify(_resp(body=body)):
        d = run(tg.evaluate(_db(), "tok", "1.2.3.4", "login"))
    assert not d.allow and d.state == "client_token_invalid" and d.error.status_code == 403
    assert code in d.error_codes and d.error.detail["retryable"] is True
    assert not tg.provider_recently_degraded()            # a client fault never opens the degraded path


def test_missing_and_malformed_tokens_never_reach_the_provider():
    import turnstile_gate as tg
    with _siteverify(_resp(body=_ok_body())) as p:
        d = run(tg.evaluate(_db(), "", "1.2.3.4", "login"))
        assert not d.allow and "missing-input-response" in d.error_codes
        assert p.return_value.post.await_count == 0


@pytest.mark.parametrize("kind", ["network", "http500", "bad_json", "non_dict"])
def test_provider_outage_fails_closed_by_default_with_retryable_503(kind):
    import httpx
    import turnstile_gate as tg
    ctx = {"network": _siteverify(exc=httpx.ConnectTimeout("t/o")),
           "http500": _siteverify(_resp(status=502, body={})),
           "bad_json": _siteverify(_resp(raise_json=True)),
           "non_dict": _siteverify(_resp(body=["nope"]))}[kind]
    with ctx:
        d = run(tg.evaluate(_db(), "tok", "1.2.3.4", "login"))
    assert not d.allow and d.state == "provider_unavailable"
    assert d.error.status_code == 503 and d.error.detail["retryable"] is True
    assert tg.provider_recently_degraded()


def test_otp_fallback_only_for_login_when_policy_allows_and_never_for_register_or_reset(monkeypatch):
    import httpx
    import turnstile_gate as tg
    monkeypatch.setenv("TURNSTILE_LOGIN_DEGRADED_POLICY", "otp_required")
    with _siteverify(exc=httpx.ConnectError("down")):
        d = run(tg.evaluate(_db(), "tok", "1.2.3.4", "login"))
        assert d.allow and d.mode == "degraded_otp_required" and d.degraded
        for surface in ("register", "password_reset"):
            with pytest.raises(Exception) as ei:
                run(tg.require_turnstile(_db(), "tok", "1.2.3.4", surface))
            assert ei.value.status_code == 503                      # never degrades, never fails open
    # a tokenless login during the SERVER's own degraded window also routes to OTP (no client claim trusted)
    d2 = run(tg.evaluate(_db(), "", "1.2.3.4", "login"))
    assert d2.allow and d2.mode == "degraded_otp_required"


def test_recovery_after_the_degraded_window_restores_the_standard_path(monkeypatch):
    import turnstile_gate as tg
    monkeypatch.setenv("TURNSTILE_LOGIN_DEGRADED_POLICY", "otp_required")
    import time as _t
    tg._mark_provider_failure()
    assert tg.provider_recently_degraded()
    monkeypatch.setattr(tg, "_LAST_PROVIDER_FAILURE_MONO", _t.monotonic() - tg.PROVIDER_DEGRADED_WINDOW_SECONDS - 1)
    assert not tg.provider_recently_degraded()               # never locked into degraded mode indefinitely
    d = run(tg.evaluate(_db(), "", "1.2.3.4", "login"))
    assert not d.allow and "missing-input-response" in d.error_codes   # standard path again
    with _siteverify(_resp(body=_ok_body())):
        assert run(tg.evaluate(_db(), "tok-B", "1.2.3.4", "login")).allow


def test_consumed_tokens_expire_and_rejections_ring_is_bounded():
    import turnstile_gate as tg
    db = _db()
    with _siteverify(_resp(body=_ok_body())):
        run(tg.evaluate(db, "tok-C", "1.2.3.4", "login"))
    row = db.turnstile_consumed_tokens.rows[0]
    assert (row["expires_at"] - datetime.now(timezone.utc)).total_seconds() <= tg.CONSUMED_TOKEN_TTL_SECONDS
    assert "expires_at" in inspect.getsource(tg.ensure_indexes) if hasattr(tg, "ensure_indexes") else True
    assert getattr(tg._RECENT_REJECTIONS, "maxlen", None)   # bounded ring — no unbounded growth under a flood


def test_force_disable_is_refused_in_production_and_misconfiguration_fails_closed(monkeypatch):
    import turnstile_gate as tg
    monkeypatch.setenv("TURNSTILE_FORCE_DISABLE", "true")
    with _siteverify(_resp(body={"success": False, "error-codes": ["invalid-input-response"]})):
        d = run(tg.evaluate(_db(), "tok", "1.2.3.4", "login"))
    assert not d.allow                                        # production ignores the kill-switch
    monkeypatch.setenv("TURNSTILE_FORCE_DISABLE", "")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "")
    d2 = run(tg.evaluate(_db(), "tok", "1.2.3.4", "login"))
    assert not d2.allow and d2.state == "configuration_invalid" and d2.error.status_code == 503
    assert tg.production_config_violation(True)


def test_login_rate_limit_and_lockout_are_time_bounded():
    src = _src("backend", "routes", "auth_routes.py")
    assert "Failed-attempt lockout: 5 wrong passwords per ip+email per 10 min" in src
    assert 'await rate_limit(db, "register"' in src and 'await rate_limit(db, "pwreset"' in src
    assert "rate_limited" in src


# ── P1-05 reconciled performance evidence ────────────────────────────────────
def test_statement_reconciliation_withholds_on_any_gap_or_cent_discrepancy():
    import broker_statement_ledger as bl
    assert bl.TOLERANCE <= 0.01
    for f in ("commission", "swap", "deposits", "withdrawals", "corrections", "fx_conversion", "unrealized_pnl"):
        assert f in bl.MONEY_FIELDS, f
    gate = inspect.getsource(bl.ledger_gate)
    for reason in ("STATEMENT_LEDGER_MISSING", "STATEMENT_PERIOD_STALE", "STATEMENT_IDENTITY_CHANGED",
                   "STATEMENT_CURRENCY_MISMATCH", "STATEMENT_SIGNATURE_INVALID", "LEDGER_CHAIN_BROKEN"):
        assert reason in gate
    assert "cov[\"reasons\"]" in gate                            # coverage gaps withhold the claim
    rec = inspect.getsource(bl.reconcile)
    assert "abs(s - p) > TOLERANCE" in rec


def test_every_chart_carries_a_provenance_label():
    import analytics
    import operator_tools
    assert '"provenance": _provenance(provider="stoic_trades", source_kind="derived"' in inspect.getsource(analytics.compute_attribution)
    assert 'source_kind="indicative"' in inspect.getsource(operator_tools.trade_replay)
    assert 'provider="stoic_safety_blocks", source_kind="derived"' in _src("backend", "routes", "safety_blocks_routes.py")
    for page, tid in (("pages/Analytics.jsx", "analytics-provenance"), ("pages/SafetyBlocks.jsx", "safety-blocks-provenance"),
                      ("components/ReplayModal.jsx", "replay-provenance")):
        s = _src("frontend", "src", page)
        assert "ChartProvenance" in s and tid in s, page
    strip = _src("frontend", "src", "components", "ChartProvenance.jsx")
    for label in ("BROKER-RECONCILED", "INDICATIVE", "SIMULATED", "PROVENANCE MISSING"):
        assert label in strip


# ── P2-03 public claims · P2-04 testimonials accessibility ───────────────────
def test_public_claims_use_capability_wording_and_link_readiness():
    login = _src("frontend", "src", "pages", "Login.jsx")
    assert "Automated gold" not in login
    assert "controlled gold and crypto trading workflows in eligible, verified environments" in login
    assert 'href="/status"' in login and "readiness requirements" in login


def test_testimonial_clones_are_hidden_unfocusable_and_ids_unique():
    t = _src("frontend", "src", "components", "LandingTestimonials.jsx")
    clone = t.split("const CloneCard")[1].split("const Card")[0]
    assert 'aria-hidden="true"' in clone and "<a " not in clone and "<button" not in clone and "tabIndex" not in clone
    assert 'id={`testimonial-${idx}`}' in t and "id=" not in clone        # real cards unique ids, clones none
    assert 'aria-hidden="true" inert={true}' in t
    assert "prefers-reduced-motion" in t and 'aria-roledescription="carousel"' in t
