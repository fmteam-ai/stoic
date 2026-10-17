# v1.60.29 UI smoke: scoreboard funnel, accounts fix button + 30s cert poll, infrastructure cert-clock + backups
import asyncio
from dotenv import dotenv_values
from playwright.async_api import async_playwright

env = dotenv_values("/app/backend/.env")
BASE = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/")
EMAIL, PW = env["TEST_ADMIN_EMAIL"], env["TEST_ADMIN_PASSWORD"]
SS = "/app/tests/.screens"


async def main():
    import os
    os.makedirs(SS, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1920, "height": 1080})
        errors, cert_gets = [], []
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"))
        page.on("console", lambda m: errors.append(f"CONSOLE {m.text}") if m.type == "error" else None)
        page.on("response", lambda r: print("API", r.status, r.url.replace(BASE, "")[:120]) if "/api/" in r.url and r.status >= 400 else None)
        page.on("request", lambda r: cert_gets.append(r.url) if r.method == "GET" and "/api/accounts/certification" in r.url else None)

        # login
        for attempt in range(2):
            try:
                await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
                await page.wait_for_selector("button[type=submit]:not([disabled])", timeout=30000)
                await page.fill("input[type=email]", EMAIL)
                await page.fill("input[type=password]", PW)
                await page.click("button[type=submit]")
                await page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
                print("LOGIN OK ->", page.url)
                break
            except Exception as e:
                print("login attempt failed", attempt, str(e)[:150])
                await page.wait_for_timeout(2000)

        # scoreboard
        errors.clear()
        await page.goto(f"{BASE}/scoreboard", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("[data-testid=decision-funnel]", timeout=20000)
            txt = await page.inner_text("[data-testid=decision-funnel]")
            print("FUNNEL OK:", txt[:300].replace("\n", " | "))
            d = await page.locator("[data-testid^=funnel-deferred-], [data-testid^=funnel-held-]").count()
            print("deferred/held rows:", d)
        except Exception as e:
            print("FUNNEL FAIL", str(e)[:200])
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{SS}/scoreboard.jpeg", quality=40, type="jpeg")
        print("scoreboard errors:", errors[:5])

        # accounts
        errors.clear(); cert_gets.clear()
        await page.goto(f"{BASE}/accounts", wait_until="domcontentloaded")
        await page.wait_for_selector("[data-testid^=account-row-]", timeout=40000)
        await page.wait_for_timeout(5000)
        body = await page.inner_text("body")
        print("Accounts has 'Turn the bot on':", "Turn the bot on" in body)
        btns = page.get_by_text("Turn the bot on")
        print("turn-bot-on count:", await btns.count())
        for t in await page.eval_on_selector_all("[data-testid^=status-line-]", "els=>els.map(e=>e.dataset.testid+': '+e.innerText.slice(0,140).replace(/\\n/g,' | '))"):
            print(t)
        await page.screenshot(path=f"{SS}/accounts.jpeg", quality=40, type="jpeg")
        print("cert GETs at t~5s:", len(cert_gets))
        await page.wait_for_timeout(32000)
        print("cert GETs at t~37s:", len(cert_gets))
        print("accounts errors:", errors[:5])

        # infrastructure
        errors.clear()
        await page.goto(f"{BASE}/infrastructure", wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("[data-testid=cert-clock_synchronization]", timeout=20000)
            print("cert-clock tile:", (await page.inner_text("[data-testid=cert-clock_synchronization]"))[:200].replace("\n", " | "))
            print("cert-clock-detail count:", await page.locator("[data-testid=cert-clock-detail]").count())
            if await page.locator("[data-testid=cert-clock-detail]").count():
                print("detail:", await page.locator("[data-testid=cert-clock-detail]").first.inner_text())
        except Exception as e:
            print("CERT CLOCK FAIL", str(e)[:200])
        await page.wait_for_timeout(3000)
        body = await page.inner_text("body")
        print("SERVER · MONGO BACKUPS:", "SERVER · MONGO BACKUPS" in body.upper())
        print("VPS FLEET · PER-DEPLOYMENT BACKUPS:", "VPS FLEET · PER-DEPLOYMENT BACKUPS" in body.upper())
        await page.screenshot(path=f"{SS}/infra.jpeg", quality=40, type="jpeg")
        print("infra errors:", errors[:5])
        await b.close()

asyncio.run(main())
