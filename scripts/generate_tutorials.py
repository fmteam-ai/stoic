"""Generate AI-narrated MP4 tutorials from REAL app screenshots.

  playwright (screenshots of live preview) + OpenAI TTS voiceover
  (Emergent universal key) + ffmpeg (stitch) → backend/static/tutorials/

Run:  cd /app && python scripts/generate_tutorials.py
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402

load_dotenv("/app/backend/.env")

APP = os.environ.get("TUTGEN_APP") or os.popen(
    "grep REACT_APP_BACKEND_URL /app/frontend/.env | cut -d= -f2"
).read().strip() or "http://localhost:3000"
OUT = Path("/app/backend/static/tutorials")
BUILD = OUT / "build"
ADMIN = ("admin@stoicaibot.com", "admin123")
VOICE, MODEL = "onyx", "tts-1-hd"

TUTORIALS = [
    {"slug": "getting-started", "track": "retail",
     "title": "Getting Started with STOIC",
     "description": "Sign up, verify your email, secure your account with "
                    "2FA and connect your MT5 account.",
     "steps": [
         {"route": "/register", "auth": False,
          "say": "Welcome to STOIC, your AI trading co-pilot. To get "
                 "started, open the register page, enter your email and a "
                 "password, accept the terms, and create your account."},
         {"route": "/login", "auth": False,
          "say": "Check your inbox for a verification email and click the "
                 "activation link. Then sign in here with your new "
                 "credentials."},
         {"route": "/settings", "auth": True,
          "say": "Before trading, secure your account. In Settings, open "
                 "the Security section and enroll two factor "
                 "authentication. Sensitive actions like live activation "
                 "always require a fresh 2FA code."},
         {"route": "/accounts", "auth": True,
          "say": "Now connect your MetaTrader 5 account. On the MT5 "
                 "Accounts page, add your account, then install the STOIC "
                 "host agent on your VPS and pair it with the one-time "
                 "bridge token. Once the heartbeat turns green, you are "
                 "connected."},
         {"route": "/", "auth": True,
          "say": "That's it. Your dashboard is live and the AI is "
                 "watching the market. Next, learn how to run the "
                 "trading bot."}]},
    {"slug": "ai-trading-bot", "track": "retail",
     "title": "Running the AI Trading Bot",
     "description": "The dashboard, AI signals, Risk Commander and the "
                    "panic button.",
     "steps": [
         {"route": "/", "auth": True,
          "say": "This is your trading dashboard. It shows account "
                 "equity, open trades, today's profit and the bot's "
                 "current state, all in real time."},
         {"route": "/signals", "auth": True,
          "say": "The AI Signals page shows every opportunity the brain "
                 "evaluated, with confidence scores and the exact gates "
                 "each signal passed or failed. Nothing trades without "
                 "passing the full pipeline."},
         {"route": "/commander", "auth": True,
          "say": "Risk Commander is your natural language control room. "
                 "Ask it to reduce risk, pause a symbol, or explain a "
                 "decision, and it acts within your safety limits."},
         {"route": "/bot", "auth": True,
          "say": "In Bot Config you choose your risk level and which "
                 "symbols to trade. The safety guardian and trading "
                 "authority always have the final word over every order."},
         {"route": "/", "auth": True,
          "say": "And remember the panic button on the top bar. One "
                 "click flattens exposure and halts the bot instantly. "
                 "You are always in control."}]},
    {"slug": "pamm-manager", "track": "manager",
     "title": "PAMM Managed Strategy for Managers",
     "description": "Create a program, assign a strategy profile and earn "
                    "LIVE status through the certification pipeline.",
     "steps": [
         {"route": "/managed", "auth": True,
          "say": "The Managed Strategy desk is where PAMM managers "
                 "operate. Create a program, connect its master account, "
                 "and monitor NAV, investors and risk from one screen."},
         {"route": "/managed", "auth": True, "scroll": "pamm-strategy-panel",
          "say": "In the Strategy Profile panel you assign one of the "
                 "four registered strategies: Sniper, Scalper, Fast "
                 "Scalp or Nitro Scalper, together with a risk profile. "
                 "The assignment pins the exact strategy version and "
                 "hash, so live money never runs on a moving target."},
         {"route": "/managed", "auth": True, "scroll": "pamm-cert-panel",
          "say": "Before real capital, the combination must earn its "
                 "certification: replay, shadow, demo and canary stages, "
                 "each with hard evidence gates and a tamper-evident "
                 "audit chain. Only a certified combo can go live."},
         {"route": "/managed", "auth": True,
          "say": "Once live, the execution guard checks every single "
                 "order against the assignment, the certification and "
                 "the strictest risk envelope. If anything drifts, "
                 "trading fails closed. That is managed money done "
                 "safely."}]},
    {"slug": "investor-flow", "track": "retail",
     "title": "Investing in a PAMM Program",
     "description": "Browse the marketplace, join a program and monitor "
                    "your investment.",
     "steps": [
         {"route": "/marketplace", "auth": True,
          "say": "The Marketplace lists published PAMM programs with "
                 "verified track records, fees and risk limits. Compare "
                 "managers before committing a single dollar."},
         {"route": "/marketplace", "auth": True,
          "say": "Found a program you like? Send a join request with "
                 "your deposit amount. The manager approves it and your "
                 "allocation starts tracking the program's NAV."},
         {"route": "/performance", "auth": True,
          "say": "Monitor everything on the Verified Performance page: "
                 "returns, drawdown and your allocation, all reconciled "
                 "against broker truth. Withdrawals follow the same "
                 "audited path. Welcome aboard."}]},
]


async def tts(text: str, path: Path) -> None:
    from emergentintegrations.llm.openai import OpenAITextToSpeech
    t = OpenAITextToSpeech(api_key=os.getenv("EMERGENT_LLM_KEY"))
    audio = await t.generate_speech(text=text, model=MODEL, voice=VOICE)
    path.write_bytes(audio)


async def capture(page, step, path: Path) -> bool:
    """Returns False if renders stayed blank (caller should renew page)."""
    for attempt in range(3):
        if attempt == 0 and page.url.startswith(APP) \
                and "/login" not in page.url:
            # SPA navigation — avoids re-fetching hundreds of vite
            # modules which trips the preview edge 429 rate limit.
            await page.evaluate(
                "(r) => { window.history.pushState({}, '', r);"
                " window.dispatchEvent(new PopStateEvent('popstate')); }",
                step["route"])
        else:
            await page.goto(APP + step["route"],
                            wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        if step.get("scroll"):
            loc = page.locator(f'[data-testid="{step["scroll"]}"]')
            if await loc.count():
                await loc.first.scroll_into_view_if_needed()
                await page.wait_for_timeout(600)
        await page.screenshot(path=str(path), full_page=False)
        from PIL import Image
        im = Image.open(path).convert("L").resize((10, 10))
        avg = sum(im.getdata()) / 100
        if avg < 240:  # dark app — near-white means blank render
            return True
        print(f"  blank capture (avg={avg:.0f}) attempt {attempt + 1} "
              f"for {step['route']} — retrying")
        await page.wait_for_timeout(2000)
    return False


def ffmpeg_segment(img: Path, mp3: Path, seg: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-i", str(mp3),
         "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-vf", "scale=1280:720,fade=t=in:st=0:d=0.4",
         "-c:a", "aac", "-b:a", "128k", "-af", "apad=pad_dur=0.6",
         "-shortest", "-r", "24", str(seg)],
        check=True, capture_output=True)


def ffmpeg_concat(segs: list, out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{s}'\n" for s in segs))
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
                    str(lst), "-c", "copy", str(out)],
                   check=True, capture_output=True)
    lst.unlink()


def duration(f: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                        "format=duration", "-of", "csv=p=0", str(f)],
                       capture_output=True, text=True)
    return round(float(r.stdout.strip() or 0), 1)


def update_manifest(entry: dict) -> None:
    mf = OUT / "manifest.json"
    data = {"tutorials": [], "voice": VOICE, "model": MODEL}
    if mf.exists():
        data = json.loads(mf.read_text())
    data["tutorials"] = [t for t in data["tutorials"]
                         if t["slug"] != entry["slug"]] + [entry]
    order = [t["slug"] for t in TUTORIALS]
    data["tutorials"].sort(key=lambda t: order.index(t["slug"]))
    mf.write_text(json.dumps(data, indent=1))


async def run_one(slug: str) -> None:
    from playwright.async_api import async_playwright
    tut = next(t for t in TUTORIALS if t["slug"] == slug)
    OUT.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    launch_args = ["--disable-dev-shm-usage", "--no-sandbox",
                   "--disable-gpu"]
    async with async_playwright() as pw:
        # Persistent profile: caches JS bundles (avoids edge rate limits)
        # and keeps the login session across per-tutorial processes.
        ctx = await pw.chromium.launch_persistent_context(
            "/tmp/tutgen_profile", args=launch_args,
            viewport={"width": 1280, "height": 720})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        anon_ctx = anon_page = None
        logged_in = False
        segs = []
        for i, step in enumerate(tut["steps"]):
            img = BUILD / f"{tut['slug']}_{i}.png"
            mp3 = BUILD / f"{tut['slug']}_{i}.mp3"
            seg = BUILD / f"{tut['slug']}_{i}.mp4"
            if step["auth"]:
                if not logged_in:
                    await page.goto(APP + "/",
                                    wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector(
                            '[data-testid="nav-dashboard"]', timeout=15000)
                    except Exception:
                        await page.goto(APP + "/login",
                                        wait_until="domcontentloaded")
                        await page.wait_for_selector(
                            'input[type="email"]', timeout=20000)
                        await page.fill('input[type="email"]', ADMIN[0])
                        await page.fill('input[type="password"]', ADMIN[1])
                        await page.click('button[type="submit"]')
                        await page.wait_for_selector(
                            '[data-testid="nav-dashboard"]', timeout=30000)
                    logged_in = True
                p = page
            else:
                if anon_page is None:
                    anon_ctx = await pw.chromium.launch_persistent_context(
                        "/tmp/tutgen_anon", args=launch_args,
                        viewport={"width": 1280, "height": 720})
                    anon_page = (anon_ctx.pages[0] if anon_ctx.pages
                                 else await anon_ctx.new_page())
                p = anon_page
            if not await capture(p, step, img):
                # renderer degraded — replace the page and retry once
                print(f"  renewing page for {step['route']}")
                new_p = await (ctx if step["auth"] else anon_ctx).new_page()
                await p.close()
                if step["auth"]:
                    page = new_p
                else:
                    anon_page = new_p
                if not await capture(new_p, step, img):
                    raise RuntimeError(
                        f"blank capture persisted for {step['route']}")
            if not mp3.exists():
                await tts(step["say"], mp3)
            ffmpeg_segment(img, mp3, seg)
            segs.append(seg)
            print(f"  {slug} step {i + 1}/{len(tut['steps'])} ok")
        await ctx.close()
        if anon_ctx:
            await anon_ctx.close()
    final = OUT / f"{slug}.mp4"
    ffmpeg_concat(segs, final)
    entry = {"slug": slug, "title": tut["title"],
             "description": tut["description"], "track": tut["track"],
             "file": f"{slug}.mp4", "duration_s": duration(final),
             "size_mb": round(final.stat().st_size / 1e6, 2),
             "steps": [s["say"] for s in tut["steps"]]}
    update_manifest(entry)
    print(f"WROTE {final} ({entry['duration_s']}s, {entry['size_mb']}MB)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True,
                    choices=[t["slug"] for t in TUTORIALS])
    asyncio.run(run_one(ap.parse_args().id))
