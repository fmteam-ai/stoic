"""HTTP tests for iter-62 multi-exchange CCXT support.

Verifies:
  * GET /api/crypto/exchanges returns all 5 supported exchanges with the
    metadata the UI dropdown needs
  * POST /api/crypto/accounts honours exchange_id (routes the verify call
    to the right CCXT class)
  * Passphrase required for OKX/KuCoin enforced at the route layer
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


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
    assert body["default"] == "binance"
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
    # Error message must mention Kraken (proves dispatch worked) — not "Binance"
    assert "Kraken" in body.get("detail", ""), body
    assert "Binance" not in body.get("detail", ""), body
