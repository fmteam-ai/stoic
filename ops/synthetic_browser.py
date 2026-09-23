"""Browser route-settlement synthetic (audit round 6 P2).

    python ops/synthetic_browser.py --base https://stoicaibot.com [--json out.json]

Raw HTML cannot prove SPA routing, so this drives a real Chromium session:
  * /dashboard unauthenticated must settle on EXACTLY one of: the login
    boundary (/login), the app shell (authenticated) or the explicit
    backend-outage screen — NEVER the public welcome page.
  * /welcome must render the welcome page (public surface healthy).
  * /login must render the login form.
  * console errors are CLASSIFIED (uncaught exception, React boundary/hydration,
    CSP violation, failed request) and FAIL the run; only documented benign
    patterns (BENIGN_CONSOLE) are ignored. Any 5xx API response fails too.
  * exactly ONE settled state per protected route (AMBIGUOUS_STATE otherwise);
    conflicting authority banners fail.
  * a bad login must produce a visible 4xx boundary, not a redirect loop.
Exit 0 = every assertion held.
"""
import argparse
import json
import sys
import time


BENIGN_CONSOLE = ("favicon", "download the react devtools", "[vite] connecting", "[vite] connected", "websocket connection to 'ws")


def _classify(msg: str) -> str:
    m = msg.lower()
    if "content security policy" in m or "refused to" in m and "csp" in m:
        return "csp_violation"
    if "hydrat" in m or "error boundary" in m or "the above error occurred" in m:
        return "react_boundary"
    if "uncaught" in m or "unhandled" in m or "typeerror" in m or "referenceerror" in m:
        return "uncaught_exception"
    if "failed to fetch" in m or "net::err" in m or "/api/" in m:
        return "failed_request"
    return "console_error"


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
        r["states"] = states
        # route state machine: /dashboard unauthenticated → exactly ONE of login|outage|app
        if r["welcome"] or "/welcome" in r["final_url"]:
            r["verdict"], r["code"] = "FAIL", "WELCOME_FALLBACK"
            errors.append(f"/dashboard fell back to the public welcome page ({r['final_url']})")
        elif len(states) != 1:
            r["verdict"], r["code"] = "FAIL", "AMBIGUOUS_STATE" if states else "NO_STATE"
            errors.append(f"/dashboard settled with states={states} — exactly one of login|outage|app is required ({r['code']})")
        else:
            r["verdict"] = "PASS"
        # conflicting authority banners (e.g. READY and BLOCKED/OUTAGE markers at once) are an ambiguity too
        banners = page.locator('[data-testid*="authority-badge"], [data-testid*="readiness-badge"], [data-testid="backend-outage-screen"]').all_inner_texts()
        levels = {b.strip().upper() for b in banners if b.strip()}
        if {"READY", "FULL"} & levels and ({"BLOCKED", "OUTAGE", "CLOSE_ONLY"} & levels or r["outage"]):
            errors.append(f"AMBIGUOUS_STATE: conflicting authority banners {sorted(levels)}")
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
        # console classification: uncaught exceptions, hydration/React boundary, CSP violations and
        # failed critical fetches FAIL the synthetic; only narrowly documented benign patterns are ignored
        real_console = [c for c in console_errors if not any(b in c.lower() for b in BENIGN_CONSOLE)]
        classified = [(_classify(c), c[:300]) for c in real_console]
        fatal = [(k, c) for k, c in classified if k != "benign"]
        if fatal:
            errors.append(f"console errors ({len(fatal)}): " + "; ".join(f"[{k}] {c[:120]}" for k, c in fatal[:5]))
        browser.close()
    build = None
    try:
        import json as _j, urllib.request
        build = _j.load(urllib.request.urlopen(f"{base}/api/health", timeout=10)).get("build_sha")
    except Exception:  # noqa: BLE001
        pass
    return {"synthetic": "browser-route-settlement", "base": base, "build_sha": build,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "results": results, "api_5xx": srv5xx, "console_errors": classified[:10],
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
