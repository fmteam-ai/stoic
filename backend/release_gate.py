"""A13 P0-02 — release gate: the running process must be an authoritative,
digest-pinned, signed release before live authority opens (production only)."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_lock(path: str | None = None) -> dict:
    # audit 14 Oct (release-gate packaging): the production host adopts the SIGNED authoritative lock into
    # ./release/rc_lock.json (deploy/lib.sh adopt_release_lock) and docker-compose mounts it read-only over the image's
    # baked-in developer snapshot; RC_LOCK_PATH lets any deployment point elsewhere.
    p = path or os.environ.get("RC_LOCK_PATH") or os.path.join(ROOT, "release", "rc_lock.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def lock_matches_build(lock: dict | None = None, build_sha: str | None = None) -> dict:
    """Startup check: the lock the gate reads must describe THIS build (lock.git_commit vs BUILD_SHA)."""
    lock = load_lock() if lock is None else lock
    if build_sha is None:
        try:
            from modules.pamm.strategy_guard import GIT_COMMIT
            build_sha = GIT_COMMIT
        except Exception:  # noqa: BLE001
            build_sha = None
    lc = str(lock.get("git_commit") or "")
    bs = str(build_sha or "")
    known = bool(lc) and bool(bs) and bs != "unknown"
    n = min(len(lc), len(bs), 40)
    match = known and n >= 7 and lc[:n].lower() == bs[:n].lower()
    return {"ok": match, "lock_commit": lc or None, "build_sha": bs or None,
            "authoritative": bool(lock.get("authoritative")), "path": os.environ.get("RC_LOCK_PATH") or "baked-in",
            "reason": None if match else ("lock or BUILD_SHA unknown" if not known else
                                           "rc_lock.git_commit != BUILD_SHA — the gate is reading a lock from a different build "
                                           "(mount the adopted release/rc_lock.json or set RC_LOCK_PATH)")}


def evaluate(lock: dict | None = None, env: dict | None = None, ea_signed: bool | None = None) -> dict:
    """Pure check. Returns {ok, failures[], lock_commit, image_digest}."""
    env = env if env is not None else os.environ
    from_disk = lock is None            # an injected lock (tests / preflight) is checked on its own terms
    lock = load_lock() if lock is None else lock
    if ea_signed is None:
        # N100-6 — a signed EX5 record is proof on its own; an EA_RELEASE_SHA256 pin must never hide it
        from ea_capabilities import _signed_release_record
        ea_signed = bool(_signed_release_record())
    fails = []
    if not lock.get("authoritative"):
        fails.append("rc_lock is not authoritative (developer snapshot) — deploy a TAGGED release: `git tag v<version> && git push --tags` "
                     "(release.yml freezes the authoritative lock), then `deploy/update.sh` in registry/auto mode pulls by digest")
    images = lock.get("images") or {}
    if not (images.get("backend") and images.get("frontend")):
        fails.append("rc_lock carries no image digests — only release.yml writes them (GHCR push → --backend-digest/--frontend-digest); "
                     "a build-mode / on-host build deploy can never satisfy this gate")
    running = env.get("STOIC_IMAGE_DIGEST") or ""
    if not running:
        fails.append("STOIC_IMAGE_DIGEST not injected — running image identity unknown")
    elif images.get("backend") and running != images["backend"]:
        fails.append("running image digest differs from the locked backend digest")
    if not ea_signed:
        fails.append("no signed EX5 release record for the shipped EA")
    binding = lock_matches_build(lock) if from_disk else None
    if binding and binding["lock_commit"] and binding["build_sha"] and binding["build_sha"] != "unknown" and not binding["ok"]:
        fails.append(binding["reason"])
    return {"ok": not fails, "failures": fails, "lock_commit": lock.get("git_commit"),
            "image_digest": running or None, "locked_backend_digest": images.get("backend"),
            "lock_binding": binding}
