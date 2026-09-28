"""Single source of truth for the production flag (SEC-001 hardening).

Every guardrail must agree on what "production" means. Historically some
modules matched only APP_ENV=="production" while others accepted the "prod"
shorthand, so a APP_ENV=prod deploy silently skipped the startup guardrails
and left the CI bypass tokens live. Route ALL checks through is_production().
"""
import os

_PROD_VALUES = {"production", "prod"}


def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in _PROD_VALUES


BYPASS_DISABLED_SENTINELS = frozenset({"disabled", "unset", "none", "off", "-"})


def bypass_token(name: str) -> str:
    """Test-bypass secret reader. The publish Secrets panel cannot store an
    empty value, so an explicit `disabled` (or unset/none/off/-) is treated
    exactly like absent — never as a usable bypass credential."""
    return removable_secret(os.environ, name)


def retired_secret_names(env=None) -> frozenset:
    """PRODUCTION_RETIRED_SECRETS — comma list of secret names the operator
    declares retired. Honoured ONLY in production (the publish Secrets panel
    may refuse edits to existing keys; new keys flow in from .env)."""
    env = os.environ if env is None else env
    if (env.get("APP_ENV") or "").strip().lower() not in _PROD_VALUES:
        return frozenset()
    return frozenset(n.strip().upper() for n in (env.get("PRODUCTION_RETIRED_SECRETS") or "").split(",") if n.strip())


def removable_secret(env, name: str) -> str:
    """Read a secret that production requires to be ABSENT; the sentinel
    `disabled` (unset/none/off/-) or a PRODUCTION_RETIRED_SECRETS entry reads
    as absent so it can be retired without an empty Secrets-panel value."""
    if name.upper() in retired_secret_names(env):
        return ""
    val = (env.get(name) or "").strip()
    return "" if val.lower() in BYPASS_DISABLED_SENTINELS else val
