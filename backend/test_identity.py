"""Disposable test-identity detection (audit item 44).

No test identity may ever authenticate against a funded production
deployment. Patterns are configurable via TEST_IDENTITY_BLOCK_PATTERNS
(comma-separated regexes); the defaults cover every disposable identity
shape the test suites generate.
"""
import os
import re

_DEFAULT_PATTERNS = (
    r"@example\.(com|org|net)$",
    r"@test\.[a-z]+$",
    r"@example-[a-z0-9]+\.com$",
    r"^test_",
    r"^nonadmin_",
    r"^qa[._-]",
    r"\+test@",
)


def _patterns():
    raw = os.environ.get("TEST_IDENTITY_BLOCK_PATTERNS", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return list(_DEFAULT_PATTERNS)


def is_disposable_test_identity(email: str) -> bool:
    e = (email or "").strip().lower()
    return any(re.search(p, e) for p in _patterns())
