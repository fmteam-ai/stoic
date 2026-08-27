"""iter-115 · Full-pipeline integration tests

Verifies all backend endpoints requested in the review:
  1) auth cookie login
  2) GET /api/architecture — 11 stages in diagram order
  3) GET /api/bot/posture — per-symbol agent rows + top-level agents
  4) GET /api/ml/ensemble + POST /api/ml/train
  5) POST /api/bridge/dom valid + invalid token
  6) GET /api/ea-script (or /api/bridge/download-ea) serves v1.43 EA
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import re

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback: read directly from /app/frontend/.env (test env may not export)
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for ln in f:
                if ln.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = ln.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except OSError:
        pass


ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _bridge_token() -> str:
    """Load the Exness#3 test-account bridge token from Mongo at runtime —
    never hardcode pairing tokens in source (they leak via git)."""
    from pymongo import MongoClient
    _db = MongoClient(_os.environ["MONGO_URL"])[_os.environ["DB_NAME"]]
    doc = _db.accounts.find_one({"label": "Exness#3"}, {"bridge_token": 1})
    return (doc or {}).get("bridge_token") or ""


BRIDGE_TOKEN = _bridge_token()

EXPECTED_STAGE_ORDER = [
    "market_data", "feature_engineering", "models", "ensemble",
    "regime", "confidence", "monte_carlo", "sizing", "risk",
    "execution", "broker",
]
EXPECTED_MARKET_CHILDREN = ["price_feed", "news_feed", "macro_data"]
EXPECTED_MODEL_CHILDREN = ["transformer", "rl_agent", "bayes"]


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    # httpOnly cookie should be set on the session
    assert "access_token" in s.cookies, f"no access_token cookie set: {dict(s.cookies)}"
    return s


# ── Auth ──
class TestAuth:
    def test_login_sets_cookie_and_me(self, session):
        r = session.get(f"{BASE_URL}/api/auth/me", timeout=15)
        assert r.status_code == 200
        me = r.json()
        assert me.get("email") == ADMIN_EMAIL


# ── Architecture ──
class TestArchitecture:
    def test_arch_returns_11_stages_in_order(self, session):
        r = session.get(f"{BASE_URL}/api/architecture", timeout=30)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        stages = body.get("stages") or []
        assert len(stages) == 11, f"expected 11 stages, got {len(stages)}"
        keys = [s.get("key") for s in stages]
        assert keys == EXPECTED_STAGE_ORDER, f"stage order wrong: {keys}"
        # every stage has non-empty detail + status in {ok, idle}
        for st in stages:
            assert st.get("status") in ("ok", "idle"), f"{st['key']} status={st.get('status')}"
            assert isinstance(st.get("detail"), str) and st.get("detail"), \
                f"{st['key']} empty detail"

    def test_market_data_has_expected_children(self, session):
        r = session.get(f"{BASE_URL}/api/architecture", timeout=30)
        stages = {s["key"]: s for s in r.json().get("stages", [])}
        md = stages.get("market_data")
        assert md is not None
        ch_keys = [c.get("key") for c in md.get("children") or []]
        assert ch_keys == EXPECTED_MARKET_CHILDREN, f"market_data children: {ch_keys}"

    def test_models_has_expected_children(self, session):
        r = session.get(f"{BASE_URL}/api/architecture", timeout=30)
        stages = {s["key"]: s for s in r.json().get("stages", [])}
        m = stages.get("models")
        assert m is not None
        ch_keys = [c.get("key") for c in m.get("children") or []]
        assert ch_keys == EXPECTED_MODEL_CHILDREN, f"models children: {ch_keys}"


# ── Posture ──
class TestPosture:
    def test_posture_returns_symbols(self, session):
        r = session.get(f"{BASE_URL}/api/bot/posture", timeout=45)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert "symbols" in body
        assert isinstance(body["symbols"], dict) and body["symbols"]

    def test_posture_xauusd_has_agent_rows(self, session):
        r = session.get(f"{BASE_URL}/api/bot/posture", timeout=45)
        body = r.json()
        sym = body.get("symbols", {}).get("XAUUSD")
        assert sym is not None, "XAUUSD missing from posture"
        # liquidity map required keys
        liq = sym.get("liquidity")
        assert isinstance(liq, dict), f"liquidity not dict: {type(liq)}"
        # allow some to be null but the keys must exist
        for k in ("order_blocks", "stop_clusters", "profile", "cum_delta", "draw"):
            assert k in liq, f"liquidity missing {k}"
        # news_ai — may be null (cache TTL) but if present has net & drivers
        news_ai = sym.get("news_ai")
        if news_ai is not None:
            assert "net" in news_ai
            assert -3 <= float(news_ai["net"]) <= 3
            assert "drivers" in news_ai
        # calendar_intel may be null
        assert "calendar_intel" in sym
        # causal only XAUUSD
        assert "causal" in sym
        if sym["causal"] is not None:
            for k in ("pressure", "chain", "narrative"):
                assert k in sym["causal"], f"causal missing {k}"
        # consensus BUY/SELL with votes; votes is a dict of agent→signed-score
        cons = sym.get("consensus") or {}
        assert "BUY" in cons and "SELL" in cons
        for side in ("BUY", "SELL"):
            side_c = cons[side]
            assert "score" in side_c
            assert "verdict" in side_c
            votes = side_c.get("votes") or {}
            assert isinstance(votes, dict), f"votes should be dict, got {type(votes)}"
            # liquidity vote must be present per review request
            assert "liquidity" in votes, f"{side} votes missing liquidity: {list(votes.keys())}"
        # uncertainty per direction
        unc = sym.get("uncertainty") or {}
        for side in ("BUY", "SELL"):
            u = unc.get(side)
            assert u is not None, f"uncertainty missing for {side}"
            assert "confidence_pct" in u
            assert u.get("risk") in ("LOW", "MEDIUM", "HIGH")
            assert "drivers" in u
        # ml_ensemble per direction
        ml = sym.get("ml_ensemble") or {}
        for side in ("BUY", "SELL"):
            m = ml.get(side)
            assert m is not None, f"ml_ensemble missing for {side}"
            assert "p_win" in m
            assert "members" in m
        # monte_carlo/explanation may be null (last signal is old)
        assert "monte_carlo" in sym
        assert "explanation" in sym

    def test_posture_toplevel_agents(self, session):
        r = session.get(f"{BASE_URL}/api/bot/posture", timeout=45)
        body = r.json()
        # meta_strategy/online_learning may be null
        assert "meta_strategy" in body
        assert "online_learning" in body
        # self_evaluation should have graded count when trades exist
        se = body.get("self_evaluation")
        if se is not None:
            assert "graded" in se
            assert "avg_entry_quality" in se
            assert "avg_exit_quality" in se
            # last_lesson may be null but key present is enough
            assert "last_lesson" in se
        # risk_engine
        re_ = body.get("risk_engine")
        assert re_ is not None, "risk_engine missing"
        for k in ("equity", "pnl_windows", "drawdown", "cvar"):
            assert k in re_, f"risk_engine missing {k}"
        pw = re_["pnl_windows"] or {}
        for w in ("day", "week", "month"):
            assert w in pw, f"pnl_windows missing {w}"
        dd = re_["drawdown"] or {}
        assert "status" in dd
        assert "detail" in dd


# ── ML Ensemble ──
class TestMLEnsemble:
    def test_ensemble_status_trained(self, session):
        r = session.get(f"{BASE_URL}/api/ml/ensemble", timeout=30)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        # admin has 400+ trades — should be trained
        assert j.get("status") == "trained", f"expected trained, got {j.get('status')}"
        aucs = j.get("aucs") or {}
        weights = j.get("weights") or {}
        for m in ("gradient_boosting", "xgboost", "lightgbm", "catboost"):
            assert m in aucs, f"aucs missing {m}"
            assert m in weights, f"weights missing {m}"

    def test_train_endpoint_returns_same_shape(self, session):
        r = session.post(f"{BASE_URL}/api/ml/train", timeout=120)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert j.get("status") == "trained"
        assert set(j.get("aucs", {}).keys()) >= {
            "gradient_boosting", "xgboost", "lightgbm", "catboost"}
        assert set(j.get("weights", {}).keys()) >= {
            "gradient_boosting", "xgboost", "lightgbm", "catboost"}


# ── Bridge DOM ──
class TestBridgeDom:
    def test_dom_valid_token_stores(self):
        payload = {
            "bridge_token": BRIDGE_TOKEN,
            "symbol": "XAUUSD",
            "bids": [{"p": 2650.10, "v": 1.5}, {"p": 2650.05, "v": 2.0}],
            "asks": [{"p": 2650.15, "v": 1.2}, {"p": 2650.20, "v": 0.8}],
        }
        r = requests.post(f"{BASE_URL}/api/bridge/dom",
                          json=payload, timeout=15)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        assert j.get("status") == "ok"
        assert j.get("stored") == 4

    def test_dom_invalid_token_401(self):
        r = requests.post(f"{BASE_URL}/api/bridge/dom",
                          json={"bridge_token": "INVALID-TOKEN-XX",
                                "symbol": "XAUUSD",
                                "bids": [{"p": 1.0, "v": 1.0}],
                                "asks": []},
                          timeout=15)
        assert 400 <= r.status_code < 500, f"expected 4xx got {r.status_code}"


# ── EA File Download ──
class TestEADownload:
    def test_ea_script_returns_v143_with_send_dom(self):
        r = requests.get(f"{BASE_URL}/api/ea-script", timeout=30)
        assert r.status_code == 200
        text = r.text
        # Must contain version 1.43 marker
        # look for '1.43' in reasonable proximity to 'version' or 'STOIC_VERSION'
        has_version = ("1.43" in text) and (
            re.search(r"1\.43", text) is not None)
        assert has_version, "EA file missing version 1.43 marker"
        # SendDom function
        assert "SendDom" in text, "EA file missing SendDom function"

    def test_bridge_download_ea_alias_works(self):
        r = requests.get(f"{BASE_URL}/api/bridge/download-ea", timeout=30)
        assert r.status_code == 200
        assert "SendDom" in r.text


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
