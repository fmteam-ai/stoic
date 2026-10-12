"""secret_scan: the admin/ops-literal rule must not span prose (iteration_250.json false positive, 2026-10-12)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def _scan(line):
    import secret_scan
    out = []
    secret_scan.scan_text(line, "test_reports/iteration_x.json", out)
    return out


def test_prose_mentioning_token_equals_is_not_a_credential():
    line = ("Logged in as admin@trading.bot; the href does NOT contain 'token=', wizard-mode shows "
            "wizard-stoic-vps, mobile 390px has NO overflow. Error text exactly 'Your plan allows 1 account'.")
    assert _scan(line) == []


def test_quoted_single_token_credential_still_blocks():
    fake = "Zq9#vL2m" + "!pX7rT4w"      # assembled so the scanner never sees a literal pair in source
    line = 'login as admin@trading.bot with pass' + 'word: "' + fake + '"'
    assert [f["kind"] for f in _scan(line)] == ["stable-account-credential"]
