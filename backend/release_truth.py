"""Runtime release truth (audit P1-5): read the cosign-verified attestation
recorded by deploy/lib.sh and reconcile it with what is actually running."""
import json
import os

ATT_PATHS = ("release/attestation.current.json", "../release/attestation.current.json",
             "/stoic/release/attestation.current.json")


def _load():
    for p in ATT_PATHS:
        for base in ("", os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            f = os.path.join(base, p) if base else p
            if os.path.exists(f):
                try:
                    return json.load(open(f)), f
                except Exception:  # noqa: BLE001
                    return None, f
    return None, None


def release_attestation_check(production: bool) -> dict:
    try:
        from modules.pamm.strategy_guard import GIT_COMMIT
    except Exception:  # noqa: BLE001
        GIT_COMMIT = None
    running_sha = GIT_COMMIT if GIT_COMMIT and GIT_COMMIT != "unknown" else None
    running_digest = os.environ.get("STOIC_IMAGE_DIGEST") or None
    att, path = _load()
    out = {"enforced": production, "attestation_present": att is not None, "path": path,
           "running_build_sha": running_sha, "running_image_digest": running_digest}
    if not att:
        out.update(ok=not production, note="no verified release attestation on this host "
                   "(deploy/lib.sh verify_attestation writes release/attestation.current.json)")
        return out
    images = att.get("images") or {}
    be = images.get("backend") or ""
    sha_match = bool(running_sha) and att.get("commit") == running_sha
    digest_match = bool(running_digest) and (be.endswith(running_digest) or running_digest in be)
    out.update(tag=att.get("tag"), commit=att.get("commit"), images=images,
               tests=att.get("tests"), gates=att.get("gates"),
               promotion_decision=att.get("promotion_decision"),
               signature_verified_by="deploy/lib.sh cosign verify-blob (keyless, workflow identity pinned)",
               provenance=att.get("provenance"),
               sha_matches_running=sha_match, digest_matches_running=digest_match)
    ok = att.get("promotion_decision") == "APPROVED" and sha_match and (digest_match or not production)
    out["ok"] = ok if production else True
    if not ok:
        out["note"] = ("attested commit/digest differ from the running process — "
                       "this is not the deployed release truth")
    return out
