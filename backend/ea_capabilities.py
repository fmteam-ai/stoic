"""EA capability gate (audit r18 P0-02): live capital may only be activated —
and canonical authority may only allow new exposure — on terminals whose
STOIC EA carries the destination-side controls. Versions are compared as
numeric tuples, never as strings."""
import re

# capability → first EA version that implements it
CAPABILITIES = {
    "command_fencing_v1": (1, 50),      # intent/seq dedupe + durable new-order journal (r4)
    "nl_close_fence_v1": (1, 57),       # close_idem_key dedupe + per-trade close_seq ordering (r17/r18)
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
    # P1-03 runbook: a locally compiled EX5 stays DEMO/PAPER-only — broker demo
    # accounts (practice money) are exempt from the binary proof, never from
    # the capability floor above.
    from broker_env import broker_environment
    if broker_environment(account) == "LIVE":
        expected = expected_ea_sha256()
        reported = (account.get("ea_binary_sha256") or "").lower()
        if not expected:
            return {"code": "EA_RELEASE_HASH_UNPINNED",
                    "reason": "no verified EX5 hash recorded for this release (scripts/verify_ea_release.py --record)"}
        if not reported:
            return {"code": "EA_BINARY_PROOF_MISSING",
                    "reason": "terminal has not reported its EX5 hash over a verified installation chain — "
                              "pair the terminal and let the signed binary proof arrive on the heartbeat"}
        if reported != expected.lower():
            return {"code": "EA_BINARY_HASH_MISMATCH",
                    "reason": "terminal-reported EX5 hash does not match the verified release hash"}
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


def expected_ea_sha256() -> str | None:
    """Verified EX5 hash for the CURRENT EA version, from the signed release record
    (EA_RELEASE_SHA256 env pin for containers) — not account metadata."""
    import json
    import os
    env = (os.environ.get("EA_RELEASE_SHA256") or "").strip().lower()
    if len(env) == 64:
        return env
    for f in _EA_RELEASE_FILES:
        try:
            rec = json.load(open(f))
        except (OSError, ValueError):
            continue
        h = str(rec.get("ex5_sha256") or rec.get("sha256") or "").lower()
        if len(h) == 64 and str(rec.get("version") or "") == version_str(LIVE_MIN_VERSION):
            return h
    return None
