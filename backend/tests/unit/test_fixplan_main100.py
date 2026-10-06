"""main100 review — regression guards for N100-4/5/6/8/9/12 + A14-7 key-id check + deploy script fixes."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_n100_5_heartbeat_reported_hash_is_never_demo_evidence():
    from unittest.mock import patch
    import broker_env as be
    with patch("ea_capabilities.accepted_ea_sha256s", lambda: ["a" * 64]):
        assert be.ea_binary_accepted({"ea_binary_sha256": "a" * 64, "ea_binary_sha256_method": "installer_attested"})
        assert not be.ea_binary_accepted({"ea_binary_sha256_reported": "a" * 64})
        assert not be.ea_binary_accepted({"ea_binary_sha256": "a" * 64, "ea_binary_sha256_method": "unattested"})
        assert not be.ea_binary_accepted({"ea_binary_sha256": "a" * 64})


def test_n100_4_main_bot_counters_exclude_scalp_engine():
    src = _read("backend", "bot_runner.py")
    assert src.count('**trade_counter_filter("auto")') == 2
    from account_reservations import trade_counter_filter
    assert trade_counter_filter("auto") == {"origin": "auto", "engine": {"$ne": "scalp"}}


def test_n100_8_ledger_gate_with_no_covered_account_is_missing_not_pass():
    import asyncio
    from fake_mongo import FakeDb
    import broker_statement_ledger as bsl
    db = FakeDb()
    reasons = asyncio.run(bsl.ledger_gate(db, "nobody"))
    assert "STATEMENT_LEDGER_MISSING" in reasons


def test_n100_6_release_gate_counts_signed_record_even_with_env_pin():
    import release_gate as rg
    from unittest.mock import patch
    lock = {"authoritative": True, "images": {"backend": "sha256:x", "frontend": "sha256:y"}, "git_commit": "c" * 40}
    with patch("ea_capabilities._signed_release_record", lambda: {"ex5_sha256": "a" * 64}):
        res = rg.evaluate(lock, {"STOIC_IMAGE_DIGEST": "sha256:x", "EA_RELEASE_SHA256": "a" * 64})
    assert res["ok"] is True, res


def test_a14_7_key_id_must_be_current_and_unrevoked():
    from unittest.mock import patch
    import release_signing as rs
    with patch.dict(os.environ, {"RELEASE_SIGNER_KEY_ID": "k1", "RELEASE_REVOKED_KEY_IDS": "k0"}):
        assert rs.key_id_accepted("k1") and not rs.key_id_accepted("k0") and not rs.key_id_accepted("k2") and not rs.key_id_accepted(None)
    src = _read("backend", "ea_capabilities.py")
    assert 'key_id_accepted(sig.get("key_id"))' in src


def test_n100_9_crypto_partial_flatten_oco_reject_and_fee_haircut():
    src = _read("backend", "crypto_bridge", "crypto_execution.py")
    assert "flatten_cancelled_partial" in src and 'not in ("ALL_DONE", "REJECT", "REJECTED")' in src
    assert "UNKNOWN_FEE_HAIRCUT" in src and "(partial fill held)" in src
    import ccxt
    from crypto_bridge.crypto_execution import never_left_exchange as is_rejection
    assert is_rejection(ccxt.InsufficientFunds("x")) is True
    assert is_rejection(ccxt.ExchangeNotAvailable("down")) is False
    assert is_rejection(ccxt.RequestTimeout("t")) is False


def test_n100_1_2_3_7_deploy_scripts():
    upd = _read("deploy", "update.sh")
    assert "restore_tracked_release_files" in upd and upd.index("restore_tracked_release_files") < upd.index('git checkout --detach "${REF}"')   # N101-1 moved the N100-1 restore into lib.sh
    assert "ensure_release_secrets || gate_refused" in upd and "sync_env_examples.py --check" not in upd
    bk = _read("deploy", "backup.sh")
    assert "BACKUP_[A-Z_]*=*" in bk and "must live OUTSIDE ./secrets" in bk
    assert "trap _restore_cleanup EXIT" in bk and bk.count("SECRETS_ENC") >= 3   # N101-3 — single EXIT trap on globals
    lib = _read("deploy", "lib.sh")
    assert 'grep -E "^${envkey}=." .env' not in lib
    rel = _read(".github", "workflows", "release.yml")
    assert "replicaSet=rs0" in rel and "upload-release-assets: false" in rel and "committed EX5" in rel
    assert "backend/static/EmergentTradingBridge.ex5" in _read(".github", "workflows", "ea-release.yml")
