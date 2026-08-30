#!/usr/bin/env python3
"""Route-matrix check (review P0-8) — authenticated direct-load of every
user-facing route, paced like a human, failing on any empty document body.

Usage:
    python scripts/route_matrix_check.py            # uses REACT_APP_BACKEND_URL
    BASE_URL=https://... python scripts/route_matrix_check.py

Requires: playwright (chromium installed) + admin credentials in env
ROUTE_MATRIX_EMAIL / ROUTE_MATRIX_PASSWORD (defaults to the preview admin).
Exit code 0 = every route rendered text; 1 = at least one blank/failed route.
"""
import asyncio
import os
import sys

ROUTES = [
    "/dashboard", "/trades", "/signals", "/commander", "/bot-health",
    "/certification", "/certification-center", "/scalp", "/execution",
    "/accounts", "/verified-performance", "/audit-log", "/loss-lab",
    "/bot", "/bot-config", "/portfolio", "/connect", "/strategies",
    "/safety-blocks", "/infrastructure", "/agents", "/research",
    "/enterprise-api", "/marketplace", "/vps", "/analytics",
    "/subscription", "/settings", "/notifications", "/help", "/support",
    "/status",
]
PACE_MS = 2500          # human-like pacing so rate limits never trip
MIN_TEXT_CHARS = 40     # anything below this is effectively blank


def _base_url() -> str:
    if os.environ.get("BASE_URL"):
        return os.environ["BASE_URL"].rstrip("/")
    envf = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "frontend", ".env")
    with open(envf) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                return line.split("=", 1)[1].strip().rstrip("/")
    raise SystemExit("BASE_URL not configured")


async def main() -> int:
    from playwright.async_api import async_playwright
    base = _base_url()
    email = os.environ.get("ROUTE_MATRIX_EMAIL", "admin@stoicaibot.com")
    password = os.environ.get("ROUTE_MATRIX_PASSWORD", "admin123")
    failures = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(f"{base}/login", wait_until="networkidle")
        await page.fill('input[type="email"]', email)
        await page.fill('input[type="password"]', password)
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(3500)
        for route in ROUTES:
            try:
                await page.goto(base + route, wait_until="domcontentloaded")
                await page.wait_for_timeout(PACE_MS)
                chars = await page.evaluate(
                    "document.body.innerText.trim().length")
                ok = chars >= MIN_TEXT_CHARS
                print(f"{'PASS' if ok else 'FAIL'} {route} ({chars} chars)")
                if not ok:
                    failures.append((route, chars))
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {route} (error: {e})")
                failures.append((route, str(e)))
            await page.wait_for_timeout(500)
        await browser.close()
    if failures:
        print(f"\n{len(failures)} route(s) rendered an empty/broken document:")
        for r, why in failures:
            print(f"  {r}: {why}")
        return 1
    print(f"\nAll {len(ROUTES)} routes rendered non-empty documents.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
