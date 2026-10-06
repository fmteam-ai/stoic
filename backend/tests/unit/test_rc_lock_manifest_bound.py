"""release/rc_lock.json is hash-bearing: regenerating docs/TEST_MANIFEST.md without
`python scripts/freeze_rc_lock.py` makes deploy/update.sh refuse the tree (provenance gate)."""
import hashlib
import json
import os

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_rc_lock_binds_current_test_manifest():
    lock = json.load(open(os.path.join(ROOT, "release", "rc_lock.json")))
    actual = hashlib.sha256(open(os.path.join(ROOT, "docs", "TEST_MANIFEST.md"), "rb").read()).hexdigest()
    assert lock["test_manifest_sha256"] == actual, \
        "docs/TEST_MANIFEST.md changed — run `python scripts/freeze_rc_lock.py` (never hand-edit the lock)"
