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


def test_credential_assembled_from_string_fragments_blocks_in_every_path():
    """2026-10-13 — backend/tests/test_audit_p3_live_iter228.py carried the LIVE admin password as
    `ADMIN_PASS = "ab" + "cd" + "#ef"` to dodge the literal rule. The shape itself is now a blocking finding."""
    import secret_scan
    frag = 'ADMIN_P' + 'ASS = "Kq7" + "mZ2v" + "#Rt9" + "pL4"'
    for path in ("backend/tests/test_live.py", "backend/routes/x.py", "scripts/run.sh"):
        out = []
        secret_scan.scan_text(frag, path, out)
        assert [f["kind"] for f in out] == ["concatenated-credential-literal"], path
    # fixture shapes stay allowed: `"…" * n` padding, assertions of ABSENCE, non-credential names
    for ok in ('TOKEN = "abcdefgh" + "x" * 16', "assert ('ADMIN_PASS" + "WORD=\"admin' + '123\"') not in ci",
               'label = "ab" + "cd" + "ef"', 'path = "/api/bridge/poll?token=" + "A" * 48'):
        out = []
        secret_scan.scan_text(ok, "backend/tests/unit/t.py", out)
        assert out == [], ok
