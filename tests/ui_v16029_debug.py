# debug: infra API responses + accounts fix-button text + 409 source
import asyncio
from dotenv import dotenv_values
from playwright.async_api import async_playwright

env = dotenv_values("/app/backend/.env")
BASE = dotenv_values("/app/frontend/.env")["REACT_APP_BACKEND_URL"].rstrip("/")


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1920, "height": 1080})
        page.on("response", lambda r: print("RESP", r.status, r.url.replace(BASE, "")) if ("/api/infra" in r.url or r.status >= 400) else None)
        await page.goto(f"{BASE}/login")
        await page.wait_for_timeout(3000)
        await page.fill("input[type=email]", env["TEST_ADMIN_EMAIL"])
        await page.fill("input[type=password]", env["TEST_ADMIN_PASSWORD"])
        await page.wait_for_timeout(1000)
        print("disabled?", await page.is_disabled("button[type=submit]"))
        print((await page.inner_text("form"))[:400])
        await page.click("button[type=submit]", timeout=60000)
        await page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
        print("--- accounts")
        await page.goto(f"{BASE}/accounts")
        await page.wait_for_selector("[data-testid^=account-row-]", timeout=30000); await page.wait_for_timeout(6000); print("URL", page.url)
        for t in await page.eval_on_selector_all("[data-testid^=status-line-]", "els=>els.map(e=>e.dataset.testid+': '+e.innerText.slice(0,120))"):
            print(t)
        print("--- infra")
        await page.goto(f"{BASE}/infrastructure")
        await page.wait_for_timeout(8000)
        print("URL", page.url)
        print(await page.eval_on_selector_all("[data-testid]", "els=>els.map(e=>e.dataset.testid).filter(t=>/cert|backup|vps|infra|mt5/i.test(t))"))
        print((await page.inner_text("main"))[:800])
        await page.screenshot(path="/app/tests/.screens/infra.jpeg", quality=40, type="jpeg")
        await b.close()

asyncio.run(main())
