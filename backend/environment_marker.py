"""Deployment-signed environment marker (audit round 7 P1).

`/api/health` advertises `environment` + `env_sig` =
HMAC-SHA256(LEDGER_ANCHOR_KEY, "<environment>|<build>") so any runner that is
about to MUTATE a target (route-auth sweep, readiness drills) can verify,
before its first login, that the target is a non-production environment it
shares a key with. Production values are refused by the runners regardless.
"""
import hashlib
import hmac
import os

MUTABLE_ENVIRONMENTS = ("staging", "ephemeral-test", "preview", "development", "test")
PRODUCTION_HOST_MARKERS = ("stoicaibot.com",)


def _key() -> bytes | None:
    k = os.environ.get("LEDGER_ANCHOR_KEY") or ""
    return k.encode() if k else None


def _msg(environment: str, build: str | None) -> bytes:
    return f"{(environment or '').strip().lower()}|{build or 'unknown'}".encode()


def sign_environment(environment: str, build: str | None) -> dict:
    key = _key()
    env = (environment or "").strip().lower()
    return {"environment": env,
            "env_sig": hmac.new(key, _msg(env, build), hashlib.sha256).hexdigest() if key else None,
            "env_sig_alg": "hmac-sha256/LEDGER_ANCHOR_KEY" if key else None}


def verify_environment(marker: dict, key: str) -> bool:
    if not key or not marker or not marker.get("env_sig"):
        return False
    expect = hmac.new(key.encode(), _msg(marker.get("environment", ""), marker.get("build_sha")), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, str(marker["env_sig"]))


def mutation_guard(base_url: str, marker: dict, env: dict, *, allow_flag: str, token_var: str) -> list:
    """Shared refusal logic for mutating runners. Returns violations."""
    problems = []
    host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    if any(m in host for m in PRODUCTION_HOST_MARKERS):
        problems.append(f"target host '{host}' is a production hostname")
    if env.get(allow_flag, "").lower() != "true":
        problems.append(f"{allow_flag}=true is required")
    tok = env.get(token_var, "")
    if len(tok) < 16:
        problems.append(f"{token_var} (unique run token, >=16 chars) is required")
    environment = (marker or {}).get("environment") or (marker or {}).get("app_env") or ""
    if environment not in MUTABLE_ENVIRONMENTS:
        problems.append(f"target advertises environment={environment!r}; mutating runs need one of {MUTABLE_ENVIRONMENTS}")
    if not verify_environment(marker, env.get("LEDGER_ANCHOR_KEY", "")):
        problems.append("environment marker signature missing or does not verify with LEDGER_ANCHOR_KEY")
    db_name = env.get("DB_NAME", "")
    if db_name and (db_name.endswith("prod") or "production" in db_name):
        problems.append(f"DB_NAME '{db_name}' looks like a production database")
    return problems
