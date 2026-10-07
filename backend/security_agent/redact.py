"""Security & Health Agent — secret masking for evidence, alerts and process logs. SA1.

`mask()` is applied to every finding's evidence and every alert text; `RedactingFilter`
is installed on the root logger of every process (API + workers) and records a hit
counter per pattern that check S1 turns into findings."""
import logging
import re
import threading

PATTERNS = [
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    # audit #9 — same bounds as integrations_settings.VALIDATORS (6–12 digit bot id, 30+ char secret)
    ("telegram_bot_token", re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")),
    ("api_key", re.compile(r"\b(?:sk|rk|pk|re|sk_live|sk_test|whsec|xox[abp])[_-][A-Za-z0-9_-]{16,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("password_kv", re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|token|api[_-]?key|bridge_token)\b(\s*[=:]\s*)(['\"]?)([^\s'\",;&]{6,})")),
    ("bridge_token", re.compile(r"\b[A-Za-z0-9]{2,8}-[0-9a-f]{24}\b|\b[0-9a-f]{32,64}\b")),
    # fix plan S4 — keys travelling in URLs: ?apiKey=…, &api_key=…, &token=…, /bot<token>/, /incoming/<secret>
    ("url_secret_param", re.compile(r"(?i)([?&](?:api_?key|apikey|token|access_token|secret|key|file_id)=)([^&\s'\"]{6,})")),
    ("url_secret_path", re.compile(r"(?i)(/(?:bot|incoming|webhook|activate|reset-password|verify)/)([A-Za-z0-9:_.~-]{12,})")),
]

_hits: dict = {}
_lock = threading.Lock()


def mask(text) -> str:
    s = str(text) if text is not None else ""
    for name, rx in PATTERNS:
        if name == "password_kv":
            s, n = rx.subn(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]", s)
        elif name in ("url_secret_param", "url_secret_path"):
            s, n = rx.subn(lambda m: f"{m.group(1)}[REDACTED]", s)
        else:
            s, n = rx.subn("[REDACTED]", s)
        if n:
            with _lock:
                _hits[name] = _hits.get(name, 0) + n
    return s


def mask_obj(obj):
    """Recursively mask strings inside dicts / lists (finding evidence)."""
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v) for v in obj]
    if isinstance(obj, str):
        return mask(obj)
    return obj


def hits(reset: bool = False) -> dict:
    with _lock:
        out = dict(_hits)
        if reset:
            _hits.clear()
    return out


class RedactingFilter(logging.Filter):
    """Masks secrets in log records before any handler formats them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            masked = mask(msg)
            if masked != msg:
                record.msg, record.args = masked, ()
                record.security_redacted = True
            # S5 — tracebacks are formatted by the handler AFTER this filter, so an HTTP error
            # carrying e.g. the Telegram bot URL would print unmasked: pre-format and mask them.
            if record.exc_info and record.exc_info[1] is not None:
                import traceback
                text = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = mask(text)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = mask(record.exc_text)
            if record.stack_info:
                record.stack_info = mask(record.stack_info)
        except Exception:  # noqa: BLE001 — a logging filter must never raise
            pass
        return True


_installed = False
_FILTER = RedactingFilter()


class _RedactingHandlerPatch:
    """S5 — root-logger filters never see records emitted by NAMED loggers, and handlers
    created later (basicConfig, uvicorn) have no filter. Patching Handler.handle masks
    every record on every handler in this process, whenever it was created."""
    _orig = None

    @classmethod
    def install(cls):
        if cls._orig is not None:
            return
        cls._orig = logging.Handler.handle

        def handle(handler, record):
            _FILTER.filter(record)
            return cls._orig(handler, record)
        logging.Handler.handle = handle


def install_log_filter() -> None:
    """Idempotent; safe to call before AND after logging.basicConfig (call it after)."""
    global _installed
    _RedactingHandlerPatch.install()
    root = logging.getLogger()
    for h in root.handlers:
        if _FILTER not in h.filters:
            h.addFilter(_FILTER)
    if _FILTER not in root.filters:
        root.addFilter(_FILTER)
    if _installed:
        return
    # fix plan S4 — HTTP clients log full request URLs at INFO (Telegram bot
    # tokens and news/macro provider credentials); keep them at WARNING.
    for noisy in ("httpx", "httpcore", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _installed = True
