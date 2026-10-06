"""EA capability gate (audit r18 P0-02): live capital may only be activated —
and canonical authority may only allow new exposure — on terminals whose
STOIC EA carries the destination-side controls. Versions are compared as
numeric tuples, never as strings."""
import re

# capability → first EA version that implements it
CAPABILITIES = {
    "command_fencing_v1": (1, 50),      # intent/seq dedupe + durable new-order journal (r4)
    "nl_close_fence_v1": (1, 57),       # close_idem_key dedupe + per-trade close_seq ordering (r17/r18)
    "margin_mode_v1": (1, 58),          # heartbeat reports ACCOUNT_MARGIN_MODE (main92 H1)
    "trade_mode_v1": (1, 60),           # heartbeat reports ACCOUNT_TRADE_MODE (main98 N98-6)
}
LIVE_REQUIRED = ("command_fencing_v1", "nl_close_fence_v1")
LIVE_MIN_VERSION = max(CAPABILITIES[c] for c in LIVE_REQUIRED)


def version_tuple(v) -> tuple | None:
    """'1.57' → (1, 57); '1.57.1' → (1, 57, 1); 'v1.60' → (1, 60); junk → None."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)\s*$", str(v or ""))
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def version_str(t: tuple) -> str:
    return ".".join(str(x) for x in t)


def capabilities_for(version) -> set[str]:
    t = version_tuple(version)
    if t is None:
        return set()
    return {c for c, mn in CAPABILITIES.items() if t >= mn}


def missing_live_capabilities(version) -> list[str]:
    have = capabilities_for(version)
    return [c for c in LIVE_REQUIRED if c not in have]


def live_gate(account: dict) -> dict | None:
    """None when the terminal qualifies for live exposure, else a blocker dict
    {code, reason} used identically by activation readiness and canonical authority."""
    v = account.get("ea_version")
    t = version_tuple(v)
    if t is None:
        return {"code": "EA_VERSION_UNKNOWN",
                "reason": f"EA version unknown — update to v{version_str(LIVE_MIN_VERSION)}+ (nl_close_fence_v1)"}
    missing = missing_live_capabilities(v)
    if missing:
        return {"code": "EA_CAPABILITY_BELOW_MIN",
                "reason": f"EA v{version_str(t)} lacks {', '.join(missing)} — update to "
                          f"v{version_str(LIVE_MIN_VERSION)}+ before live activation"}
    # r20 P1-01: binary proof is REQUIRED for live accounts — the expected hash is
    # pinned from the signed release record (never from editable account metadata)
    # and the terminal must report its EX5 hash on the heartbeat handshake.
    # P1-03 runbook: a locally compiled EX5 stays DEMO/PAPER-only. The
    # exemption is server-authoritative (broker_env.attested_environment):
    # a user-declared "demo" never bypasses the proof until an admin attests
    # it — otherwise a live account could self-label DEMO (security audit).
    from broker_env import attested_environment, broker_environment
    if attested_environment(account) == "LIVE":
        expected = expected_ea_sha256()
        reported = (account.get("ea_binary_sha256") or "").lower()
        declared_demo = broker_environment(account) == "DEMO"
        if not expected:
            if declared_demo:
                return _demo_unattested(account)
            return {"code": "EA_RELEASE_HASH_UNPINNED",
                    "reason": "no verified EX5 hash recorded for this release (scripts/verify_ea_release.py --record)"}
        if not reported:
            if declared_demo:
                return _demo_unattested(account)
            return {"code": "EA_BINARY_PROOF_MISSING",
                    "reason": "terminal has not reported its EX5 hash over a verified installation chain — "
                              "pair the terminal and let the signed binary proof arrive on the heartbeat"}
        if reported not in {expected.lower(), *accepted_ea_sha256s()}:
            return {"code": "EA_BINARY_HASH_MISMATCH",
                    "reason": "terminal-reported EX5 hash matches neither the current nor the previous signed release hash"}
        # r22/r25: only an INSTALLER-MEASURED hash bound to the same installation
        # counts as proof. user_trust (token + public login/server), an unmeasured
        # installer pairing, or an EA echoing a hash the installer never recorded
        # is telemetry — never live-admissible.
        method = str(account.get("ea_binary_sha256_method") or "")
        if method != "installer_attested":
            hint = {"user_trust": "EX5 hash was reported over a one-click trusted terminal",
                    "installer_mismatch": "heartbeat EX5 hash differs from the hash the installer measured on this terminal",
                    "installer_unattested": "the installer's EX5 measurement was reported with the bridge token only — "
                                            "re-run the installer so it signs the proof with its enrolled device key",
                    "installer_attestation_stale": "the signed installer attestation is older than the freshness window — "
                                                   "re-run the installer to re-attest",
                    "device_key_revoked": "the installer device key was revoked — re-pair with a fresh dashboard token",
                    }.get(method, "the installer has not recorded a measured EX5 hash for this installation")
            return {"code": "EA_BINARY_PROOF_UNATTESTED",
                    "reason": f"{hint} — re-run the STOIC installer on the terminal so the deployed EX5 is "
                              "measured and bound to the installation (STOIC-Proof.txt)"}
    return None


_EA_RELEASE_FILES = ("release/ea_release.json", "/app/release/ea_release.json")


def _demo_unattested(account: dict | None = None) -> dict:
    from broker_env import attestation_state
    if account is not None and attestation_state(account) == "invalidated":
        return {"code": "EA_DEMO_ATTESTATION_INVALIDATED",
                "reason": "the DEMO attestation was voided because the account's bound identity changed "
                          "(broker/server/number/terminal/credentials) — an admin must re-attest it "
                          "(Admin → Broker Registry → Account Environments)"}
    return {"code": "EA_DEMO_UNATTESTED",
            "reason": "account is declared DEMO but not attested — an admin must confirm the demo "
                      "environment (Admin → Broker Registry → Account Environments) before an "
                      "unverified EX5 may trade on it; live accounts always need the signed release proof"}


_EXPECTED_CACHE: dict = {}


def shipped_ea_version(mq5_path: str | None = None) -> str:
    """`#property version` of the MQ5 this server ships (falls back to the live minimum)."""
    import os
    import re
    path = mq5_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "EmergentTradingBridge.mq5")
    try:
        m = re.search(r'^#property\s+version\s+"([\d.]+)"', open(path, encoding="utf-8", errors="replace").read(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return version_str(LIVE_MIN_VERSION)


def expected_ea_sha256() -> str | None:
    """Verified EX5 hash for the CURRENT EA version (first of `accepted_ea_sha256s()`)."""
    acc = accepted_ea_sha256s()
    return acc[0] if acc else None


def accepted_ea_sha256s() -> list[str]:
    """N-R6 — EX5 hashes a LIVE terminal may run, current first. Trusted ONLY from:
      · EA_RELEASE_SHA256 / EA_RELEASE_SHA256_PREVIOUS env pins (set by the attested deploy), or
      · a release record (release/ea_release.json | docs/RELEASE_HASHES.json#ea) that is
        Ed25519-SIGNED, compiled by the sanctioned CI job, whose recorded MQ5 hash matches the
        MQ5 this server ships and whose version == the shipped #property version — plus the
        record's `previous` entry (the last signed release) so a rollout never puts terminals
        still on the previous EA into CLOSE_ONLY. Never from account metadata; an unsigned or
        drifted record contributes nothing (fail closed)."""
    import os
    out: list[str] = []
    for name in ("EA_RELEASE_SHA256", "EA_RELEASE_SHA256_PREVIOUS"):
        env = (os.environ.get(name) or "").strip().lower()
        if len(env) == 64 and env not in out:
            out.append(env)
    rec = _signed_release_record()
    if rec:
        for h in (rec.get("ex5_sha256"), (rec.get("previous") or {}).get("ex5_sha256")):
            h = str(h or "").lower()
            if len(h) == 64 and h not in out:
                out.append(h)
    return out


def _signed_release_record() -> dict | None:
    """The verified EA release entry (current + optional signed `previous`) or None."""
    import hashlib
    import json
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    mq5_path = os.path.join(here, "static", "EmergentTradingBridge.mq5")
    candidates = list(_EA_RELEASE_FILES) + [os.path.join(here, "..", "docs", "RELEASE_HASHES.json"),
                                             "/app/docs/RELEASE_HASHES.json"]
    for f in candidates:
        try:
            st = os.stat(f)
        except OSError:
            continue
        key = (f, st.st_mtime_ns, st.st_size)
        if key in _EXPECTED_CACHE:
            if _EXPECTED_CACHE[key]:
                return _EXPECTED_CACHE[key]
            continue
        result = None
        try:
            rec = json.load(open(f))
            rec = rec.get("ea") if "ea" in rec and "ex5_sha256" not in rec else rec
            h = str((rec or {}).get("ex5_sha256") or "").lower()
            sig = (rec or {}).get("signature") or {}
            # the record must be for the EA this server SHIPS (its #property version),
            # which may be newer than the live-minimum capability version (1.59 ≥ 1.57)
            ok = (len(h) == 64 and str(rec.get("version") or "") == shipped_ea_version(mq5_path)
                  and rec.get("compiled_by") == "github-actions" and sig.get("sig_hex"))
            if ok and os.path.exists(mq5_path):
                # line-ending independent (CRLF Windows runner vs LF server)
                ok = hashlib.sha256(open(mq5_path, "rb").read().replace(b"\r\n", b"\n")).hexdigest() == str(rec.get("mq5_sha256") or "")
            if ok:
                from release_signing import verify_hex, key_id_accepted
                # A14-7 — the record's key id must be the current, un-revoked release key
                ok = key_id_accepted(sig.get("key_id")) and verify_hex(_canonical_payload(rec), sig["sig_hex"])
            if ok:
                prev = rec.get("previous") or None
                # the previous release must carry its own valid signature (its MQ5 is gone)
                if prev and not (len(str(prev.get("ex5_sha256") or "")) == 64 and prev.get("compiled_by") == "github-actions"
                                 and (prev.get("signature") or {}).get("sig_hex")
                                 and key_id_accepted((prev.get("signature") or {}).get("key_id"))
                                 and verify_hex(_canonical_payload(prev), prev["signature"]["sig_hex"])):
                    prev = None
                result = {**rec, "previous": prev}
        except Exception:  # noqa: BLE001 — unreadable/unverifiable record ⇒ fail closed
            result = None
        _EXPECTED_CACHE[key] = result
        if result:
            return result
    return None


def _canonical_payload(rec: dict) -> bytes:
    import json
    body = {k: rec.get(k) for k in ("version", "mq5_sha256", "ex5_sha256", "metaeditor_version",
                                    "windows_build", "mt5_build", "source_commit")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
