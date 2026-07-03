"""iter-85 · EA v1.36 token-from-file auto-load.

Pairs with the iter-84 PowerShell installer. The installer writes the
bridge token to `MQL5\\Files\\STOIC-Token.txt`; this iter ensures the EA
source we ship reads from that file when the inputs field is empty/default.

Tests are content-level (we don't compile MQL5 in CI) but they're enough
to lock in:
  · Version bumps are coherent (#property version, EA_CLIENT_VERSION, Print, /api/ea-script).
  · ResolveBridgeToken() helper is present.
  · EffectiveToken global is declared.
  · `BridgeToken` is no longer passed as an argument to outbound StringFormat
    calls (must go through EffectiveToken now).
  · /api/setup/claim-pairing advertises the latest version.
"""
from __future__ import annotations

import os
import re
import requests

EA_PATH = "/app/backend/static/EmergentTradingBridge.mq5"
EXPECTED_VERSION = "1.38"

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
if not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL


def _ea_source() -> str:
    with open(EA_PATH) as f:
        return f.read()


# ─────────────────── Version coherence ───────────────────
def test_property_version_matches():
    src = _ea_source()
    assert re.search(rf'#property\s+version\s+"{re.escape(EXPECTED_VERSION)}"', src)


def test_client_version_define_matches():
    src = _ea_source()
    assert f'#define EA_CLIENT_VERSION "{EXPECTED_VERSION}"' in src


def test_oninit_print_includes_version():
    src = _ea_source()
    # The Print concatenates EA_CLIENT_VERSION at runtime — assert the pattern.
    assert 'Print("STOIC Bridge EA v", EA_CLIENT_VERSION, " started' in src


# ─────────────────── Token-from-file logic ───────────────────
def test_resolve_helper_exists():
    src = _ea_source()
    assert "string ResolveBridgeToken()" in src
    assert "STOIC-Token.txt" in src
    assert "FileIsExist" in src
    assert "FileOpen" in src


def test_effective_token_global_declared():
    src = _ea_source()
    assert re.search(r"string\s+EffectiveToken\s*=\s*\"\"\s*;", src)


def test_oninit_assigns_effective_token():
    src = _ea_source()
    assert "EffectiveToken = ResolveBridgeToken()" in src


def test_oninit_assigns_effective_token_before_first_heartbeat():
    """Order matters: token must be resolved BEFORE SendHeartbeat() in OnInit
    otherwise the first heartbeat fires with an empty bridge_token."""
    src = _ea_source()
    init_block = src[src.index("int OnInit()"):]
    init_block = init_block[:init_block.index("return INIT_SUCCEEDED")]
    # Use the actual call site (with semicolon) — there's an earlier `SendHeartbeat()`
    # mention inside a comment.
    assert init_block.index("ResolveBridgeToken()") < init_block.index("SendHeartbeat();")


def test_resolve_helper_skips_comment_and_blank_lines():
    """Installer writes a multi-line file with `#` comments and a header.
    Helper must skip those to get the actual token."""
    src = _ea_source()
    body = src[src.index("string ResolveBridgeToken()"):
               src.index("string ResolveBridgeToken()") + 2500]
    # comment-line skip: char 0 == '#'
    assert "StringGetCharacter(line, 0) == '#'" in body
    # blank-line skip
    assert "StringLen(line) == 0" in body


# ─────────────────── BridgeToken is no longer the runtime value ───────────────────
def test_bridge_token_input_declaration_kept():
    """The user-facing inputs dialog must still expose `BridgeToken` as a
    manual override option."""
    src = _ea_source()
    assert 'input string BridgeToken' in src


def test_outbound_calls_use_effective_token_not_input():
    """All HTTP calls must build their JSON with EffectiveToken (the
    resolved value). Argument-position `BridgeToken,` usages would mean
    we forgot to switch a call site to the resolved global."""
    src = _ea_source()
    # Allow `BridgeToken` to appear in:
    #   · the input declaration line
    #   · comments and Print warnings
    #   · the resolver function body (input_trim = BridgeToken)
    # but NOT as a positional arg in StringFormat (which is `BridgeToken,`).
    # Scan line by line so we can tolerate the legitimate occurrences.
    bad_lines = []
    for i, ln in enumerate(src.splitlines(), 1):
        if "BridgeToken" not in ln:
            continue
        stripped = ln.strip()
        if stripped.startswith("//"):
            continue
        if "input string BridgeToken" in ln:
            continue
        if "input_trim = BridgeToken" in ln:
            continue
        # The resolver function's name itself contains "BridgeToken".
        if "ResolveBridgeToken" in ln:
            continue
        # Print() string literals naming the input field for the user.
        if "Print(" in ln and "BridgeToken" in ln:
            continue
        bad_lines.append((i, ln))
    assert not bad_lines, f"BridgeToken still used as runtime arg at: {bad_lines}"


def test_effective_token_used_in_heartbeat_body():
    src = _ea_source()
    # The heartbeat body builds the JSON with EffectiveToken
    assert 'EffectiveToken, balance' in src


# ─────────────────── Served file matches local file ───────────────────
def test_api_ea_script_serves_v136():
    r = requests.get(f"{BASE_URL}/api/ea-script", timeout=20)
    assert r.status_code == 200
    body = r.text
    assert f'#property version   "{EXPECTED_VERSION}"' in body
    assert f'#define EA_CLIENT_VERSION "{EXPECTED_VERSION}"' in body
    assert "ResolveBridgeToken" in body
    assert "EffectiveToken" in body


def test_claim_pairing_advertises_v136():
    """The installer reads `ea_latest_version` from the claim response and
    cache-busts its EA download with it — keep this in sync."""
    # Quick smoke: just check the constant in the route source.
    with open("/app/backend/routes/setup_routes.py") as f:
        src = f.read()
    assert f'"ea_latest_version": "{EXPECTED_VERSION}"' in src
