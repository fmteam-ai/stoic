"""Docker-secrets / vault-agent support: for every env var `X_FILE`, if `X`
is unset or empty, load the file's content into `X`. No-op outside managed-
secret deployments."""
import logging
import os

logger = logging.getLogger("secrets")


def resolve_file_secrets() -> None:
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
