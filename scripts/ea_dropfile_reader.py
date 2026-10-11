#!/usr/bin/env python3
"""M122-P1 — Python port of the EA's drop-file readers (EmergentTradingBridge.mq5 ≥ 1.63).

Mirrors, line for line, what the EA does with MQL5\\Files\\STOIC-Token.txt / STOIC-Installation.txt /
STOIC-Server.txt / STOIC-Proof.txt: open as ANSI text, strip a leading BOM, skip blank and '#' lines, take the
first value, validate its shape. `--legacy` reproduces the ≤ 1.62 reader (no BOM strip, no shape check) so the
CI gate can prove the bug class is detected: the legacy reader on a 5.1-written file yields the comment line.

Runs on the Windows CI runner and on the AlmaLinux host (python 3.9): no modern syntax.

    python scripts/ea_dropfile_reader.py --files-dir <MQL5\\Files> [--expect-token X] [--expect-installation Y]
                                         [--expect-server Z] [--expect-proof H] [--legacy] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
INSTALLATION_RE = re.compile(r"^inst_[0-9a-f]{12}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SERVER_URL_DEFAULT = "https://stoicaibot.com"


def decode_ansi(raw: bytes) -> str:
    """FILE_ANSI: one byte → one character (cp1252 on a Western Windows). Undecodable bytes map to U+0080+ so
    they still count as 'non-ASCII residue' exactly like the EA sees them."""
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def strip_bom(s: str) -> str:
    """EA StripBom(): a leading U+FEFF, then up to three leading characters >= 0x80 (BOM bytes read as ANSI)."""
    if s.startswith("\ufeff"):
        s = s[1:]
    n = 0
    while n < 3 and n < len(s) and ord(s[n]) >= 0x80:
        n += 1
    return s[n:]


def first_value(text: str, *, legacy: bool = False) -> str:
    """EA ReadDropFileValue(): first non-blank, non-comment line (BOM-stripped unless legacy)."""
    for line in text.splitlines():
        if not legacy:
            line = strip_bom(line)
        line = line.strip()
        if not line or line[0] == "#":
            continue
        return line
    return ""


def read_token(text: str, *, legacy: bool = False) -> dict:
    v = first_value(text, legacy=legacy)
    if not v:
        return {"value": "", "ok": False, "reason": "empty or comments-only"}
    if legacy:
        return {"value": v, "ok": True, "reason": None}
    if not TOKEN_RE.match(v):
        return {"value": "", "ok": False, "reason": "token file malformed (%d chars)" % len(v)}
    return {"value": v, "ok": True, "reason": None}


def read_installation(text: str, *, legacy: bool = False) -> dict:
    v = first_value(text, legacy=legacy)
    if not v:
        return {"value": "", "ok": False, "reason": "empty or comments-only"}
    if legacy:
        return {"value": v, "ok": True, "reason": None}
    if not INSTALLATION_RE.match(v):
        return {"value": "", "ok": False, "reason": "installation file malformed"}
    return {"value": v, "ok": True, "reason": None}


def read_server(text: str, *, legacy: bool = False) -> dict:
    """EA ResolveServerUrl() file branch: first https:// carrying line; never an empty URL."""
    for line in text.splitlines():
        if not legacy:
            line = strip_bom(line)
        line = line.strip()
        if not legacy and (not line or line[0] == "#"):
            continue
        p = line.find("https://")
        if p < 0:
            continue
        url = line[p:]
        if len(url) < 12 or not url.startswith("https://"):
            return {"value": SERVER_URL_DEFAULT, "ok": False, "reason": "not https"}
        return {"value": url.rstrip("/"), "ok": True, "reason": None}
    return {"value": SERVER_URL_DEFAULT, "ok": False, "reason": "no https url in file"}


def read_proof(text: str, *, legacy: bool = False) -> dict:
    v = first_value(text, legacy=legacy).lower()
    if legacy:
        ok = len(v) == 64
    else:
        ok = bool(HEX64_RE.match(v))
    return {"value": v if ok else "", "ok": ok, "reason": None if ok else "proof malformed"}


READERS = {
    "STOIC-Token.txt": ("token", read_token),
    "STOIC-Installation.txt": ("installation", read_installation),
    "STOIC-Server.txt": ("server", read_server),
    "STOIC-Proof.txt": ("proof", read_proof),
}


def read_dir(files_dir: Path, *, legacy: bool = False) -> dict:
    out = {}
    for name, (key, fn) in READERS.items():
        p = files_dir / name
        if not p.exists():
            out[key] = {"value": "", "ok": False, "reason": "missing", "bom": None}
            continue
        raw = p.read_bytes()
        res = fn(decode_ansi(raw), legacy=legacy)
        res["bom"] = raw[:3] == b"\xef\xbb\xbf"
        out[key] = res
    return out


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files-dir", required=True)
    ap.add_argument("--legacy", action="store_true", help="reproduce the <= 1.62 reader (no BOM strip / shape check)")
    ap.add_argument("--expect-token")
    ap.add_argument("--expect-installation")
    ap.add_argument("--expect-server")
    ap.add_argument("--expect-proof")
    ap.add_argument("--expect-no-bom", action="store_true", help="fail when any present drop file starts with EF BB BF")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = read_dir(Path(a.files_dir), legacy=a.legacy)
    failures = []
    for key, expected in (("token", a.expect_token), ("installation", a.expect_installation),
                          ("server", a.expect_server), ("proof", a.expect_proof)):
        if expected is not None and res[key]["value"] != expected:
            failures.append("%s: got %r expected %r (%s)" % (key, res[key]["value"], expected, res[key]["reason"]))
    if a.expect_no_bom:
        for key, r in res.items():
            if r.get("bom"):
                failures.append("%s: file starts with a UTF-8 BOM" % key)
    if a.json:
        print(json.dumps({"results": res, "failures": failures, "legacy": a.legacy}, indent=2))
    else:
        for key, r in res.items():
            print("%-13s ok=%-5s bom=%-5s value=%r%s" % (key, r["ok"], r.get("bom"), r["value"],
                                                        (" (" + r["reason"] + ")") if r["reason"] else ""))
        for f in failures:
            print("FAIL " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
