"""HIBP Pwned-Passwords screening via the k-anonymity range API.
Only the first 5 chars of the SHA-1 leave the server; fail-open on any
network/service error so HIBP outages never block auth flows."""
import hashlib
import logging

import httpx

logger = logging.getLogger("hibp")

RANGE_URL = "https://api.pwnedpasswords.com/range/"
HEADERS = {"User-Agent": "stoic-trading-bot/1.0 (breached-password-screen)",
           "Add-Padding": "true"}


async def is_password_breached(password: str) -> bool:
    """True only when the password is POSITIVELY known-breached."""
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(f"{RANGE_URL}{prefix}", headers=HEADERS)
        if resp.status_code != 200:
            logger.warning("HIBP range returned %s — failing open", resp.status_code)
            return False
        for line in resp.text.splitlines():
            part, _, count = line.partition(":")
            if part.strip().upper() == suffix:
                try:
                    return int(count.strip()) > 0
                except ValueError:
                    return True
        return False
    except Exception as e:
        logger.warning("HIBP unreachable (%s) — failing open", type(e).__name__)
        return False


BREACHED_DETAIL = {
    "code": "breached_password",
    "message": "This password appears in known data breaches. "
               "Please choose a different one.",
}
