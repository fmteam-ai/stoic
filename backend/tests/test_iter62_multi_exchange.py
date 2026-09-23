from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""HTTP tests for iter-62 multi-exchange CCXT support.

Verifies:
  * GET /api/crypto/exchanges returns all 5 supported exchanges with the
    metadata the UI dropdown needs
  * POST /api/crypto/accounts honours exchange_id (routes the verify call
    to the right CCXT class)
  * Passphrase required for OKX/KuCoin enforced at the route layer
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pytest
import requests
from live_target import require_live_base_url

BASE_URL = require_live_base_url()

pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    yield s


def test_exchanges_endpoint_requires_auth():
    r = requests.get(f"{BASE_URL}/api/crypto/exchanges", timeout=10)
    assert r.status_code == 401


def test_exchanges_endpoint_returns_all_five(session):
    r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=10)
    assert r.status_code == 200
    body = r.json()
    # Default is "binance" only if binance is reachable from this server.
    # Otherwise it shifts to the first reachable exchange (smart default).
    assert body["default"] in {"binance", "binanceus", "kraken", "okx", "kucoin"}
    ids = {x["id"] for x in body["exchanges"]}
    assert ids == {"binance", "binanceus", "kraken", "okx", "kucoin"}
    # Schema sanity for each
    for x in body["exchanges"]:
        assert isinstance(x["label"], str) and x["label"]
        assert isinstance(x["requires_passphrase"], bool)
        assert isinstance(x["supports_sandbox"], bool)
        assert x["default_quote"] in {"USDT", "USD"}


def test_okx_kucoin_flagged_passphrase_required(session):
    r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=10)
    by_id = {x["id"]: x for x in r.json()["exchanges"]}
    assert by_id["okx"]["requires_passphrase"] is True
    assert by_id["kucoin"]["requires_passphrase"] is True
    assert by_id["binance"]["requires_passphrase"] is False
    assert by_id["kraken"]["requires_passphrase"] is False


def test_kraken_quote_is_usd(session):
    r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=10)
    by_id = {x["id"]: x for x in r.json()["exchanges"]}
    assert by_id["kraken"]["default_quote"] == "USD"
    assert by_id["binance"]["default_quote"] == "USDT"


def test_exchanges_endpoint_returns_reachability_per_exchange(session):
    r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=15)
    assert r.status_code == 200
    body = r.json()
    for x in body["exchanges"]:
        # `reachable` must be present and a bool (probe completed)
        assert "reachable" in x, f"exchange {x['id']} missing 'reachable' field"
        assert isinstance(x["reachable"], bool), f"reachable for {x['id']} not bool"
        # When unreachable, reach_error explains why
        if x["reachable"] is False:
            assert x.get("reach_error"), f"{x['id']} unreachable but no error message"


def test_default_skips_unreachable_exchange(session):
    """If Binance Global is blocked, default must shift to a reachable one."""
    r = session.get(f"{BASE_URL}/api/crypto/exchanges", timeout=15)
    body = r.json()
    by_id = {x["id"]: x for x in body["exchanges"]}
    default_meta = by_id[body["default"]]
    # The default exchange must itself be reachable.
    assert default_meta["reachable"] is True, (
        f"Default exchange '{body['default']}' is not reachable — "
        f"smart-default logic broken"
    )


def test_create_account_with_unknown_exchange_returns_422(session):
    r = session.post(
        f"{BASE_URL}/api/crypto/accounts",
        json={
            "label": "bad-exchange",
            "api_key": "AAAAAAAAAAAAAAAA",
            "api_secret": "BBBBBBBBBBBBBBBB",
            "exchange_id": "ftx",  # ftx is RIP, not supported
            "testnet": True,
        },
        timeout=20,
    )
    assert r.status_code == 422
    assert "unsupported" in r.text.lower() or "choose one of" in r.text.lower()


def test_create_okx_account_without_passphrase_returns_422(session):
    r = session.post(
        f"{BASE_URL}/api/crypto/accounts",
        json={
            "label": "okx-no-pass",
            "api_key": "AAAAAAAAAAAAAAAA",
            "api_secret": "BBBBBBBBBBBBBBBB",
            "exchange_id": "okx",
            "testnet": True,
            # api_passphrase intentionally omitted
        },
        timeout=20,
    )
    assert r.status_code == 422
    assert "passphrase" in r.text.lower()


def test_create_kraken_account_routes_to_kraken(session):
    """Verify the verify-probe error message identifies Kraken, not Binance —
    proves the route dispatched to the correct CCXT class."""
    r = session.post(
        f"{BASE_URL}/api/crypto/accounts",
        json={
            "label": "fake-kraken",
            "api_key": "AAAAAAAAAA1234",
            "api_secret": "BBBBBBBBBB12345678",
            "exchange_id": "kraken",
            "testnet": True,
        },
        timeout=20,
    )
    assert r.status_code == 422
    body = r.json()
    # Error message must mention Kraken (proves dispatch worked) — not "Binance".
    # iter-148: detail is a stable-code dict {"code", "message"} now.
    detail = body.get("detail", "")
    msg = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    assert "Kraken" in msg, body
    assert "Binance" not in msg, body


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
