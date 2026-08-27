"""ExecutionAuthorization — capability token for the execution choke
point (defense-in-depth). Only the Execution Authority may mint one;
`execute_authorized()` refuses any call without a valid, unexpired,
single-use token bound to the intent. A developer accidentally calling
the engine stage directly two years from now fails loudly."""
import hashlib
import hmac
import inspect
import logging
import os
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger("execution.authorization")

_PROCESS_KEY = secrets.token_bytes(32)   # per-process, unforgeable
_TTL_MS = 60_000
_used: dict[str, int] = {}               # nonce → expiry ms (single-use)
_ALLOWED_MINTERS = {"execution_authority.py"}


class UnauthorizedExecution(Exception):
    """Raised when something other than the authority tries to mint."""


@dataclass(frozen=True)
class ExecutionAuthorization:
    intent_id: str
    minted_at_ms: int
    nonce: str
    mac: str


def _mac_for(intent_id: str, ts: int, nonce: str) -> str:
    return hmac.new(_PROCESS_KEY, f"{intent_id}|{ts}|{nonce}".encode(),
                    hashlib.sha256).hexdigest()


def mint_authorization(intent_id: str) -> ExecutionAuthorization:
    """Mint a capability token. Caller MUST be the Execution Authority —
    any other module raises UnauthorizedExecution."""
    caller = os.path.basename(inspect.stack()[1].filename)
    if caller not in _ALLOWED_MINTERS:
        raise UnauthorizedExecution(
            f"ExecutionAuthorization can only be minted by the Execution "
            f"Authority (attempted from {caller})")
    ts = int(time.time() * 1000)
    nonce = secrets.token_hex(8)
    return ExecutionAuthorization(intent_id=str(intent_id or ""),
                                  minted_at_ms=ts, nonce=nonce,
                                  mac=_mac_for(str(intent_id or ""),
                                               ts, nonce))


def verify_authorization(auth, intent_id: str | None) -> str | None:
    """None when valid; otherwise the refusal reason. Single-use."""
    if not isinstance(auth, ExecutionAuthorization):
        return "missing_authorization"
    if not hmac.compare_digest(
            _mac_for(auth.intent_id, auth.minted_at_ms, auth.nonce),
            auth.mac):
        return "invalid_mac"
    now = int(time.time() * 1000)
    if now - auth.minted_at_ms > _TTL_MS:
        return "expired"
    if str(intent_id or "") != auth.intent_id:
        return "intent_mismatch"
    if auth.nonce in _used:
        return "already_used"
    if len(_used) > 4096:
        for k in [k for k, exp in _used.items() if exp < now]:
            _used.pop(k, None)
    _used[auth.nonce] = now + _TTL_MS
    return None
