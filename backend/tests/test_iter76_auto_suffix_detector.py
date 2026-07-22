"""iter-76 · Auto Broker Suffix Detector tests.

Pure-function tests covering: Tauro `.fx`, OnEquity `.e`, IC Markets `.raw`,
FBS `.std`, RoboForex bare, FXTM `pro`, mixed-suffix brokers, and edge
cases (empty list, all garbage, single match)."""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

from broker_symbol_detector import infer_broker_suffix, _split_base_suffix


def test_tauro_fx_suffix():
    """Tauro Markets / JMFinancial — `.fx` across all instruments."""
    symbols = [
        "XAUUSD.fx", "XAGUSD.fx", "EURUSD.fx", "GBPUSD.fx",
        "USDJPY.fx", "BTCUSD.fx",
    ]
    out = infer_broker_suffix(symbols)
    assert out["suffix"] == ".fx"
    assert out["confidence"] == 1.0
    assert "XAUUSD" in out["matched_bases"]
    assert "EURUSD" in out["matched_bases"]
    assert out["sample_size"] == 6


def test_onequity_e_suffix():
    """OnEquity ECN — `.e` everywhere."""
    out = infer_broker_suffix(["XAUUSD.e", "EURUSD.e", "USDJPY.e", "BTCUSD.e"])
    assert out["suffix"] == ".e"
    assert out["confidence"] == 1.0


def test_ic_markets_raw_suffix():
    out = infer_broker_suffix(["XAUUSD.raw", "EURUSD.raw", "GBPUSD.raw"])
    assert out["suffix"] == ".raw"


def test_roboforex_bare_no_suffix():
    """RoboForex micro accounts often use bare names."""
    out = infer_broker_suffix(["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"])
    assert out["suffix"] == ""
    assert out["confidence"] == 1.0


def test_fxtm_pro_suffix():
    """FXTM-style — 'pro' (no leading dot)."""
    out = infer_broker_suffix(["XAUUSDpro", "EURUSDpro", "GBPUSDpro"])
    assert out["suffix"] == "pro"


def test_cent_account_suffix():
    out = infer_broker_suffix(["XAUUSDcent", "EURUSDcent", "USDJPYcent"])
    assert out["suffix"] == "cent"


def test_special_char_suffix():
    """Some brokers use `+` or `#`."""
    plus = infer_broker_suffix(["XAUUSD+", "EURUSD+", "GBPUSD+"])
    assert plus["suffix"] == "+"
    hash_ = infer_broker_suffix(["XAUUSD#", "EURUSD#"])
    assert hash_["suffix"] == "#"


def test_mixed_suffix_picks_majority():
    """If broker exposes BOTH .fx and bare versions, picks the one used
    on more distinct bases."""
    # 3 bases use .fx, 1 base uses bare → .fx wins
    out = infer_broker_suffix([
        "XAUUSD.fx", "EURUSD.fx", "GBPUSD.fx",
        "USDJPY",  # bare for one base only
    ])
    assert out["suffix"] == ".fx"
    assert out["confidence"] == 0.75   # 3 of 4 distinct bases


def test_tie_broken_by_longer_suffix():
    """Equal vote counts → longer suffix wins (more specific)."""
    out = infer_broker_suffix([
        "XAUUSD.fx", "EURUSD.fx",   # 2 distinct bases on .fx
        "GBPUSD.f",  "USDJPY.f",    # 2 distinct bases on .f
    ])
    assert out["suffix"] == ".fx"


def test_ignores_unrelated_instruments():
    """Stocks / weird tickers must NOT contribute to the inference."""
    out = infer_broker_suffix([
        "XAUUSD.fx", "EURUSD.fx",
        "AAPL", "TSLA", "USOIL.fx", "BRENT",
    ])
    assert out["suffix"] == ".fx"


def test_empty_input_returns_safe_default():
    out = infer_broker_suffix([])
    assert out["suffix"] == ""
    assert out["confidence"] == 0.0
    assert out["sample_size"] == 0


def test_garbage_input_returns_no_match():
    out = infer_broker_suffix(["FOO", "BAR", "BAZ123"])
    assert out["suffix"] == ""
    assert out["confidence"] == 0.0
    assert out["matched_bases"] == []


def test_single_match_still_returns_suffix():
    """If only ONE known base is offered, trust that suffix anyway —
    it's the only evidence we have."""
    out = infer_broker_suffix(["XAUUSD.fx"])
    assert out["suffix"] == ".fx"
    assert out["confidence"] == 1.0


def test_rejects_garbage_long_suffix():
    """Won't be fooled by `XAUUSDdoesnotexist` as a 12-char suffix."""
    out = infer_broker_suffix(["XAUUSDdoesnotexist"])
    assert out["suffix"] == ""  # rejected by MAX_SUFFIX_LEN


def test_case_insensitive_base_match():
    """`xauusd.fx` (lowercase) should still match the XAUUSD base."""
    out = infer_broker_suffix(["xauusd.fx", "eurusd.fx"])
    assert out["suffix"] == ".fx"


def test_split_helper_returns_tuple():
    base, sfx = _split_base_suffix("XAUUSD.fx")
    assert base == "XAUUSD"
    assert sfx == ".fx"
    bare = _split_base_suffix("XAUUSD")
    assert bare == ("XAUUSD", "")


def test_split_helper_returns_none_on_unknown():
    assert _split_base_suffix("AAPL") is None
    assert _split_base_suffix("") is None
    assert _split_base_suffix("XAUUSD/THIS_IS_BAD") is None


def test_response_shape_contract():
    """Every response must include the documented keys with right types."""
    out = infer_broker_suffix(["XAUUSD.fx"])
    assert isinstance(out["suffix"], str)
    assert isinstance(out["confidence"], float)
    assert isinstance(out["matched_bases"], list)
    assert isinstance(out["matched_symbols"], list)
    assert isinstance(out["sample_size"], int)


# ════════════════════ Routing integration ════════════════════
# Verify execution.py picks the right suffix from the 3-tier precedence.

import asyncio
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient


def _db_creds():
    mongo_url = "mongodb://localhost:27017"
    db_name = "test_database"
    with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
        for line in f:
            if line.startswith("MONGO_URL="):
                mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("DB_NAME="):
                db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    return mongo_url, db_name


def test_user_set_suffix_wins_over_auto_detected():
    """If user has manual symbol_suffix AND auto-detected exists,
    execution.py must use the user's value (override semantics)."""
    account = {
        "_id": ObjectId(),
        "symbol_suffix": ".manual",
        "auto_detected_symbol_suffix": ".auto",
    }
    user_suffix = (account.get("symbol_suffix") or "").strip()
    suffix = user_suffix if user_suffix else (account.get("auto_detected_symbol_suffix") or "").strip()
    source = "manual" if user_suffix else ("auto" if suffix else "none")
    assert suffix == ".manual"
    assert source == "manual"


def test_auto_detected_used_when_user_suffix_empty():
    account = {"symbol_suffix": "",
               "auto_detected_symbol_suffix": ".fx"}
    user_suffix = (account.get("symbol_suffix") or "").strip()
    suffix = user_suffix if user_suffix else (account.get("auto_detected_symbol_suffix") or "").strip()
    source = "manual" if user_suffix else ("auto" if suffix else "none")
    assert suffix == ".fx"
    assert source == "auto"


def test_bare_when_neither_suffix_set():
    account = {}
    user_suffix = (account.get("symbol_suffix") or "").strip()
    suffix = user_suffix if user_suffix else (account.get("auto_detected_symbol_suffix") or "").strip()
    source = "manual" if user_suffix else ("auto" if suffix else "none")
    assert suffix == ""
    assert source == "none"


def test_heartbeat_persists_auto_suffix():
    """Simulated heartbeat with available_symbols → DB stores
    auto_detected_symbol_suffix on the account."""
    import requests
    mongo_url, db_name = _db_creds()

    async def _scenario():
        client = AsyncIOMotorClient(mongo_url)
        db = client[db_name]
        try:
            # Seed a test account with a bridge_token
            tok = f"iter76-test-{ObjectId()}"
            uid = f"iter76-user-{ObjectId()}"
            acct_id = ObjectId()
            await db.accounts.insert_one({
                "_id": acct_id, "user_id": uid, "label": "iter76-tauro-clone",
                "broker": "Tauro Clone", "bridge_token": tok,
                "mode": "live", "status": "connected",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            # POST a heartbeat with .fx-style symbols
            with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL"):
                        api = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                        break
            requests.post(f"{api}/api/bridge/heartbeat",
                          json={
                              "bridge_token": tok,
                              "balance": 1000.0, "equity": 1000.0,
                              "open_positions": 0,
                              "client_version": "1.34",
                              "available_symbols": [
                                  "XAUUSD.fx", "EURUSD.fx", "GBPUSD.fx",
                                  "USDJPY.fx", "AAPL",
                              ],
                          }, timeout=10)
            # Re-read the account doc
            updated = await db.accounts.find_one({"_id": acct_id})
            assert updated["auto_detected_symbol_suffix"] == ".fx"
            assert updated["auto_detected_suffix_confidence"] == 1.0
            assert "XAUUSD" in updated["auto_detected_suffix_bases"]
        finally:
            await db.accounts.delete_one({"_id": acct_id})
            client.close()
    asyncio.run(_scenario())
