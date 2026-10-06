"""A13 P0-02 — release gate: the running process must be an authoritative,
digest-pinned, signed release before live authority opens (production only)."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_lock(path: str | None = None) -> dict:
    p = path or os.path.join(ROOT, "release", "rc_lock.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def evaluate(lock: dict | None = None, env: dict | None = None, ea_signed: bool | None = None) -> dict:
    """Pure check. Returns {ok, failures[], lock_commit, image_digest}."""
    env = env if env is not None else os.environ
    lock = load_lock() if lock is None else lock
    if ea_signed is None:
        # N100-6 — a signed EX5 record is proof on its own; an EA_RELEASE_SHA256 pin must never hide it
        from ea_capabilities import _signed_release_record
        ea_signed = bool(_signed_release_record())
    fails = []
    if not lock.get("authoritative"):
        fails.append("rc_lock is not authoritative (developer snapshot)")
    images = lock.get("images") or {}
    if not (images.get("backend") and images.get("frontend")):
        fails.append("rc_lock carries no image digests")
    running = env.get("STOIC_IMAGE_DIGEST") or ""
    if not running:
        fails.append("STOIC_IMAGE_DIGEST not injected — running image identity unknown")
    elif images.get("backend") and running != images["backend"]:
        fails.append("running image digest differs from the locked backend digest")
    if not ea_signed:
        fails.append("no signed EX5 release record for the shipped EA")
    return {"ok": not fails, "failures": fails, "lock_commit": lock.get("git_commit"),
            "image_digest": running or None, "locked_backend_digest": images.get("backend")}
