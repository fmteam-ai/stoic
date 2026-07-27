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
