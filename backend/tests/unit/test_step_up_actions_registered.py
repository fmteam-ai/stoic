"""v1.60.32 — every action name passed to require_step_up() must be registered in STEP_UP_ACTIONS.

Settings → Notifications "Send test" asked for step-up "alert_test" (and host-migration for "host_migration") but
the token issuer refused them with "Unknown step-up action", so an admin / MFA user could never send a test alert.
"""
import os
import re

import pytest

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_CALL = re.compile(r'require_step_up\([^)]*?,\s*"([a-z_]+)"\s*\)')
_CONST = re.compile(r'^STEP_UP_ACTION\s*=\s*"([a-z_]+)"', re.M)


def _source_files():
    for base in (BACKEND, os.path.join(BACKEND, "routes")):
        for name in sorted(os.listdir(base)):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def test_every_require_step_up_action_is_registered():
    from step_up import STEP_UP_ACTIONS
    used: dict[str, str] = {}
    for path in _source_files():
        src = open(path, encoding="utf-8").read()
        for m in _CALL.finditer(src):
            used[m.group(1)] = os.path.relpath(path, BACKEND)
        for m in _CONST.finditer(src):
            used[m.group(1)] = os.path.relpath(path, BACKEND)
    assert "alert_test" in used and "host_migration" in used          # the regex really sees the call sites
    missing = {a: f for a, f in used.items() if a not in STEP_UP_ACTIONS}
    assert not missing, f"require_step_up actions not registered in STEP_UP_ACTIONS: {missing}"


def test_alert_test_token_can_be_issued():
    from step_up import STEP_UP_ACTIONS
    assert {"alert_test", "host_migration"} <= STEP_UP_ACTIONS
    src = open(os.path.join(BACKEND, "routes", "auth_routes.py"), encoding="utf-8").read()
    assert "Unknown step-up action" in src and "STEP_UP_ACTIONS" in src
