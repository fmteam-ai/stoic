"""Docker-secrets / vault-agent support: for every env var `X_FILE`, if `X`
is unset or empty, load the file's content into `X`. No-op outside managed-
secret deployments.

v1.60.5 — blank values are UNSET. `docker compose env_file` and python-dotenv
both turn an untouched template line `KEY=` into KEY="" — every
`int(os.environ.get("KEY", "60"))` then dies at import with
`invalid literal for int()` (the install-from-archive backend never became
healthy). Dropping the blanks restores the defaults the code expects."""
import logging
import os

logger = logging.getLogger("secrets")


def drop_blank_env() -> int:
    blanks = [k for k, v in os.environ.items() if v == ""]
    for k in blanks:
        del os.environ[k]
    return len(blanks)


def resolve_file_secrets() -> None:
    drop_blank_env()
    for key in list(os.environ):
        if not key.endswith("_FILE"):
            continue
        target = key[:-5]
        path = os.environ[key]
        if os.environ.get(target) or not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                os.environ[target] = f.read().strip()
            logger.info("loaded %s from secret file", target)
        except OSError as e:
            raise RuntimeError(f"secret file for {target} unreadable: {path}") from e
