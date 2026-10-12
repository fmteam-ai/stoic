"""Runtime release truth (audit P1-5): read the cosign-verified attestation
recorded by deploy/lib.sh and reconcile it with what is actually running."""
from __future__ import annotations
import json
import os

# A18 Part 3 — the HOST's verified truth comes first (ro mount ./deploy/state:/app/state, written by deploy/lib.sh
# publish_release_truth): a registry image cannot carry the lock that holds its own digest, so the copies baked into
# the image are only the fallback (on-host builds).
STATE_DIR = os.environ.get("STOIC_STATE_DIR") or "/app/state"
ATT_PATHS = (os.path.join(STATE_DIR, "release", "attestation.current.json"),
             "release/attestation.current.json", "../release/attestation.current.json",
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


def release_signed_verdict(ea: dict | None, att: dict | None) -> dict:
    """P1-02 (main131) — the Go-Live "Release signed" row. Pure. GREEN only when the EA record is VERIFIED and
    ENFORCED (never `ok`, which is True whenever enforcement is off), the release attestation is valid
    (promotion APPROVED, signature verified by the deploy) and the deployed build is the attested commit."""
    ea, att = ea or {}, att or {}
    verified, enforced = bool(ea.get("verified")), bool(ea.get("enforced"))
    att_valid = bool(att.get("attestation_present")) and att.get("promotion_decision") == "APPROVED"
    build_match = bool(att.get("sha_matches_running"))
    ok = verified and enforced and att_valid and build_match
    missing = [k for k, v in (("EA record verified", verified), ("EA proof enforced", enforced),
                              ("release attestation valid", att_valid), ("deployed build == release commit", build_match)) if not v]
    detail = (f"{att.get('tag') or 'release'} @ {str(att.get('commit') or '')[:12]} · EA verified + enforced" if ok
              else "missing: " + ", ".join(missing))
    return {"ok": ok, "detail": detail, "checks": {"ea_verified": verified, "ea_enforced": enforced,
                                                  "attestation_valid": att_valid, "build_matches_release": build_match}}


LOCK_PATHS = (os.path.join(STATE_DIR, "release", "rc_lock.json"),
              "release/rc_lock.json", "../release/rc_lock.json", "/stoic/release/rc_lock.json")


def deploy_source_check() -> dict:
    """A18 Part 3 — what the running images were provisioned from (deploy/state/deploy_source.json): registry (signed
    CI digests), build (on-host), build-fallback (registry pull/verification failed → on-host build of the same commit).
    Informational; `warn` on a fallback so the operator sees that the live digest gate cannot be green."""
    d = _deploy_source()
    if not d:
        return {"ok": True, "severity": "ok", "available": False, "source": None,
                "detail": "no deploy_source.json yet (written by deploy/update.sh provision_images)"}
    src = d.get("source")
    containers = d.get("containers") or {}
    mixed = bool(d.get("mixed")) or any(c.get("status") == "MISMATCH" for c in containers.values())
    if mixed:   # M121-5 — a mixed stack is a failed deploy, never "ok"
        bad = sorted(n for n, c in containers.items() if c.get("status") == "MISMATCH")
        return {"ok": False, "severity": "block", "available": True, "source": src, "reason": d.get("reason"), "commit": d.get("commit"),
                "mixed": True, "containers": containers, "at": d.get("at"),
                "detail": "MIXED STACK — container(s) not running the provisioned image: " + (", ".join(bad) or "unknown"),
                "fix": "deploy/update.sh <tag> --reprovision --yes (then docker compose ps; a service missing from docker-compose.registry.yml keeps its old image)"}
    sev = "warn" if src == "build-fallback" else "ok"
    detail = {"registry": "running the signed CI images pulled by attested digest",
              "build": f"built on this host ({d.get('reason') or 'DEPLOY_MODE=build'})",
              "build-fallback": f"REGISTRY FALLBACK — {d.get('reason')}; on-host build of the same commit is running "
                                "(re-run deploy/update.sh <tag> once GHCR is reachable for live digest authority)"}.get(src, str(src))
    return {"ok": True, "severity": sev, "available": True, "source": src, "reason": d.get("reason"), "commit": d.get("commit"),
            "backend_image": d.get("backend_image"), "deploy_mode": d.get("deploy_mode"), "at": d.get("at"), "detail": detail,
            "mixed": False, "containers_checked": len(containers),
            "fix": "deploy/update.sh <tag> --yes" if src == "build-fallback" else None}


def rc_lock_check(production: bool) -> dict:
    """Round 9 P1-06: the deployed rc_lock must be the CI-authoritative lock for the
    RUNNING build (commit + backend digest). Developer snapshots never satisfy production."""
    try:
        from modules.pamm.strategy_guard import GIT_COMMIT
    except Exception:  # noqa: BLE001
        GIT_COMMIT = None
    running_sha = GIT_COMMIT if GIT_COMMIT and GIT_COMMIT != "unknown" else None
    running_digest = os.environ.get("STOIC_IMAGE_DIGEST") or None
    lock = path = None
    for p in LOCK_PATHS:
        for base in ("", os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            f = os.path.join(base, p) if base else p
            if os.path.exists(f):
                path = f
                try:
                    lock = json.load(open(f))
                except Exception:  # noqa: BLE001
                    lock = None
                break
        if path:
            break
    out = {"enforced": production, "present": lock is not None, "path": path, "running_build_sha": running_sha}
    if not lock:
        out.update(ok=not production, note="no rc_lock.json on this host")
        return out
    be = (lock.get("images") or {}).get("backend") or ""
    fe = (lock.get("images") or {}).get("frontend") or ""
    out.update(commit=lock.get("git_commit"), authoritative=bool(lock.get("authoritative")),
               deployment_target=lock.get("deployment_target"),
               sha_matches_running=bool(running_sha) and lock.get("git_commit") == running_sha,
               digest_matches_running=bool(running_digest) and running_digest in be)
    # M121-5 — EVERY running app container (deploy/state/deploy_source.json "containers", written by the host after
    # verify_stack_images) must run the locked digest; one stale/unpinned worker makes the stack non-authoritative.
    containers = (_deploy_source() or {}).get("containers") or {}
    stale = sorted(n for n, c in containers.items()
                   if not (c.get("repo_digest") and c["repo_digest"].split("@")[-1] in (be if c.get("kind") == "backend" else fe)))
    out.update(containers_checked=len(containers), containers_stale=stale,
               all_containers_match=bool(containers) and not stale if containers else None)
    ok = (out["authoritative"] and out["sha_matches_running"] and (out["digest_matches_running"] or not production)
          and not (production and stale))
    out["ok"] = ok if production else True
    if not ok:
        out["note"] = ("mixed stack — container(s) not running the locked digest: " + ", ".join(stale)) if stale else \
            "rc_lock is not the CI-bound lock for the running build (commit/digest/authoritative mismatch)"
    return out


def _deploy_source() -> dict | None:
    try:
        d = json.load(open(os.path.join(STATE_DIR, "deploy_source.json")))
    except Exception:  # noqa: BLE001
        return None
    return d if isinstance(d, dict) else None


def release_id(*, policy_version: str | None = None) -> dict:
    """P2-04 (main131) — ONE release identity for /api/health and the UI footer: tag + commit (from the verified
    release attestation when present, else the running build), EA version, rules-policy version and the model
    manifest version/digest. Every field is read from an artifact — nothing is hand-typed."""
    att, _ = _load()
    att = att or {}
    try:
        from modules.pamm.strategy_guard import GIT_COMMIT
    except Exception:  # noqa: BLE001
        GIT_COMMIT = None
    running = GIT_COMMIT if GIT_COMMIT and GIT_COMMIT != "unknown" else None
    try:
        from ea_capabilities import shipped_ea_version
        ea_version = shipped_ea_version()
    except Exception:  # noqa: BLE001
        ea_version = None
    mm = {"version": None, "code_commit": None, "sha256": None}
    try:
        from model_manifest import MANIFEST
        doc = json.loads(MANIFEST.read_text())
        mm = {"version": (doc.get("body") or {}).get("version"), "code_commit": str((doc.get("body") or {}).get("code_commit") or "")[:12] or None,
              "sha256": str(doc.get("body_sha256") or "")[:12] or None}
    except Exception:  # noqa: BLE001
        pass
    commit = att.get("commit") or running
    out = {"tag": att.get("tag") or os.environ.get("STOIC_RELEASE_TAG") or None, "commit": commit, "short_commit": commit[:12] if commit else None,
           "attested": bool(att) and att.get("promotion_decision") == "APPROVED" and att.get("commit") == running,
           "ea_version": ea_version, "policy_version": policy_version, "model_manifest": mm}
    out["id"] = " · ".join(x for x in (out["tag"], out["short_commit"], f"EA {ea_version}" if ea_version else None,
                                        f"policy {policy_version}" if policy_version else None,
                                        f"models v{mm['version']}@{mm['sha256']}" if mm.get("version") else None) if x)
    return out


def public_release_identity() -> dict:
    """P2-03 — what the public status page shows: the signed release id (rc_lock commit when the lock is the
    CI-authoritative one for the running build) and the short running commit. Never a hand-typed version."""
    chk = rc_lock_check(production=False)
    running = chk.get("running_build_sha")
    signed = bool(chk.get("present")) and bool(chk.get("authoritative")) and bool(chk.get("sha_matches_running"))
    return {"short_commit": running[:12] if running else None,
            "release_id": (chk.get("commit") or "")[:12] if signed else None,
            "signed": signed, "signer_key_id": None,
            "note": None if signed else ("developer snapshot — not a signed release" if chk.get("present") else "no rc_lock on this host")}
