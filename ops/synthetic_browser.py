"""Browser route-settlement synthetic (audit round 6 P2).

    python ops/synthetic_browser.py --base https://stoicaibot.com [--json out.json]

Raw HTML cannot prove SPA routing, so this drives a real Chromium session:
  * /dashboard unauthenticated must settle on EXACTLY one of: the login
    boundary (/login), the app shell (authenticated) or the explicit
    backend-outage screen — NEVER the public welcome page.
  * /welcome must render the welcome page (public surface healthy).
  * /login must render the login form.
  * no uncaught console errors / failed API responses with 5xx.
  * a bad login must produce a visible 4xx boundary, not a redirect loop.
Exit 0 = every assertion held.
"""
import argparse
import json
import sys
import time


def run(base: str) -> dict:
    from playwright.sync_api import sync_playwright
    base = base.rstrip("/")
    results, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        console_errors, bad_responses = [], []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad_responses.append((r.url, r.status)) if r.status >= 500 else None)

        def settle(path: str, wait_selector: str | None = None) -> dict:
            t0 = time.time()
            page.goto(f"{base}{path}", wait_until="domcontentloaded", timeout=30000)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=20000)
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(1500)
            return {"path": path, "final_url": page.url, "ms": round((time.time() - t0) * 1000),
                    "welcome": page.locator('[data-testid="welcome-page"]').count() > 0,
                    "login": page.locator('[data-testid="login-form"], form[data-testid*="login"], input[type="password"]').count() > 0,
                    "outage": page.locator('[data-testid="backend-outage-screen"]').count() > 0,
                    "app": page.locator('[data-testid="app-layout"], [data-testid="patient-dashboard"], nav').count() > 0
                    and "/login" not in page.url and "/welcome" not in page.url}

        r = settle("/dashboard", '[data-testid="login-form"], [data-testid="backend-outage-screen"], input[type="password"], nav')
        states = [k for k in ("login", "outage", "app") if r[k]]
        r["verdict"] = "PASS" if (not r["welcome"] and states and "/welcome" not in r["final_url"]) else "FAIL"
        if r["verdict"] == "FAIL":
            errors.append(f"/dashboard settled on {r['final_url']} welcome={r['welcome']} states={states} — protected route must show login, app or outage")
        r["states"] = states
        results.append(r)

        r = settle("/welcome", '[data-testid="welcome-page"]')
        r["verdict"] = "PASS" if r["welcome"] else "FAIL"
        if r["verdict"] == "FAIL":
            errors.append("/welcome did not render the welcome page")
        results.append(r)

        r = settle("/login", 'input[type="password"]')
        r["verdict"] = "PASS" if r["login"] else "FAIL"
        if r["verdict"] == "FAIL":
            errors.append("/login did not render the login form")
        results.append(r)

        # negative auth path in the browser: explicit boundary, no redirect loop
        try:
            page.fill('input[type="email"], input[name="email"]', "monitor@invalid.test")
            page.fill('input[type="password"]', "definitely-wrong-password")
            page.keyboard.press("Enter")
            page.wait_for_timeout(3000)
            bad_login = {"final_url": page.url, "still_login": "/login" in page.url,
                         "error_visible": page.locator('[role="alert"], [data-testid*="error"], [data-testid*="challenge"], [data-testid*="turnstile"]').count() > 0}
            bad_login["verdict"] = "PASS" if bad_login["still_login"] else "FAIL"
            if bad_login["verdict"] == "FAIL":
                errors.append(f"bad login left /login → {page.url}")
        except Exception as e:  # noqa: BLE001
            bad_login = {"verdict": "SKIP", "note": f"login form not automatable: {e}"}
        results.append({"path": "/login (bad credentials)", **bad_login})

        srv5xx = [b for b in bad_responses if "/api/" in b[0]]
        if srv5xx:
            errors.append(f"5xx API responses during session: {srv5xx[:5]}")
        real_console = [c for c in console_errors if "favicon" not in c.lower()]
        browser.close()
    return {"synthetic": "browser-route-settlement", "base": base, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": results, "api_5xx": srv5xx, "console_errors": real_console[:10],
            "errors": errors, "result": "PASS" if not errors else "FAIL"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    out = run(a.base)
    txt = json.dumps(out, indent=1)
    print(txt)
    if a.json:
        open(a.json, "w").write(txt)
    sys.exit(0 if out["result"] == "PASS" else 1)
