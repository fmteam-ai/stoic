"""Single source of truth for the CURRENT EA version in tests.

Historical iteration tests pinned literal versions ("1.40", "1.42", ...)
which broke on every EA bump. All version tests now assert COHERENCE
against the live `#define EA_CLIENT_VERSION` instead of a stale pin.
"""
import re

EA_PATH = "/app/backend/static/EmergentTradingBridge.mq5"


def current_ea_version() -> str:
    with open(EA_PATH) as f:
        src = f.read()
    m = re.search(r'#define\s+EA_CLIENT_VERSION\s+"([\d.]+)"', src)
    assert m, "EA_CLIENT_VERSION define missing from EA source"
    return m.group(1)
