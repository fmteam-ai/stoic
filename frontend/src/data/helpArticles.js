// STOIC Help Center — knowledge base content (step-by-step guides).
// Markdown dialect: renderMarkdown in @/lib/markdown.

export const HELP_CATEGORIES = [
    { id: "start", label: "Getting Started" },
    { id: "mt5", label: "MT5 & EA Setup" },
    { id: "bot", label: "Bot & Risk" },
    { id: "billing", label: "Billing" },
    { id: "trouble", label: "Troubleshooting" },
];

export const HELP_ARTICLES = [
    // ── Getting Started ────────────────────────────────────────────────
    {
        slug: "what-is-stoic", cat: "start", title: "What is STOIC and how does it work?",
        summary: "The 60-second overview: signals, vetoes, and your own broker account.",
        body: `## What is STOIC?
STOIC is an AI trading bot for Gold (XAUUSD), indices and Bitcoin. It scans markets every 60 seconds, runs each candidate trade through a multi-agent veto cascade (trend, structure, liquidity, forecast, news, risk engine), and routes only the survivors to **your own MT5 account** via a downloadable Expert Advisor (EA).

## The important part
- Your funds never leave your broker. STOIC is software, not a broker.
- Every trade is explainable — open any trade and press DNA or REPLAY to see exactly why it fired.
- Safety rails (drawdown ladders, exposure caps, cooldowns) run on **every** mode, even Demo.

## Your first hour
1. Complete the onboarding wizard (or the 4-step checklist on the Dashboard).
2. Connect a broker account on **MT5 Accounts** — a demo account is perfect to start.
3. Install the STOIC EA in your MT5 terminal.
4. Pick a risk profile in **Bot Config** and switch the bot ON in Demo mode.
5. Watch the Dashboard: the BOT PULSE panel shows what the bot is thinking every cycle.`,
    },
    {
        slug: "onboarding-checklist", cat: "start", title: "Complete the 4-step onboarding",
        summary: "Connect broker → install EA → pick risk → first signal.",
        body: `## The 4 steps
1. **Connect a broker account** — go to MT5 Accounts, click ADD ACCOUNT, and enter your account number + broker server exactly as your MT5 terminal shows them.
2. **Install the STOIC EA** — download it from the Accounts page, compile in MetaEditor (F7), and attach it to any chart. Enable "Allow algorithmic trading".
3. **Pick a risk profile** — Bot Config → choose Low / Medium / High and toggle the bot ON.
4. **Wait for the first signal** — the bot scans every 60s. A first trade lands only when confidence and all risk filters pass — with high safety thresholds this can take hours. That is by design.

## Where to see progress
The Dashboard banner tracks all 4 steps live and self-completes as you finish them. You can re-open the full-screen wizard any time from the Help Center.`,
    },
    {
        slug: "subscription-tiers", cat: "start", title: "Choosing a subscription tier",
        summary: "Starter vs Trader vs Professional vs Elite AI — what each unlocks.",
        body: `## The 4 tiers
- **Starter** — 1 MT5 account, Demo + Shadow trading. Learn the system risk-free.
- **Trader** — 3 accounts, Supervised Live trading, Replay Studio and AI Coach.
- **Professional** — 10 accounts, Digital Twin, Research Lab, VPS management.
- **Elite AI** — 50 accounts and Autonomous Live (after your account passes certification).

## Good to know
- Plans are **prepaid** — no auto-renew surprises. We email you before a plan lapses.
- Upgrading mid-term credits your unused days into the higher tier automatically.
- Downgrades never cut time you already paid for — they start when the current pass ends.
- A subscription never bypasses safety: Autonomous Live still requires the certification gate.`,
    },
    // ── MT5 & EA Setup ─────────────────────────────────────────────────
    {
        slug: "connect-broker", cat: "mt5", title: "Connect your MT5 broker account",
        summary: "Step-by-step: registering an account and what the fields mean.",
        body: `## Before you start
Have your MT5 terminal open. You need two values exactly as MT5 shows them (top-left of the terminal): the **account number** and the **broker server name** (e.g. \`RoboForex-ECN\`).

## Steps
1. Open **MT5 Accounts** in the sidebar and click ADD ACCOUNT.
2. Enter a display name (anything you like), the account number, and the broker server.
3. Save. STOIC creates a unique **bridge token** for this account — the EA uses it to authenticate. Treat it like a password.
4. The account shows DISCONNECTED until the EA sends its first heartbeat.

## Identity verification
STOIC verifies that the EA reporting in is really attached to the account you registered (account number + broker server must match). Trades will not route to an unverified account — this protects you from wiring a bot to the wrong terminal.`,
    },
    {
        slug: "install-ea", cat: "mt5", title: "Install the STOIC EA (Expert Advisor)",
        summary: "Download, compile, attach — and the settings that matter.",
        body: `## Steps
1. On **MT5 Accounts**, click DOWNLOAD EA next to your account. You get \`EmergentTradingBridge.mq5\`.
2. In MT5: File → Open Data Folder → \`MQL5/Experts\` — copy the file there.
3. Open MetaEditor (F4), open the file, press **F7** to compile. You should see 0 errors.
4. Back in MT5, drag the EA from the Navigator onto **any chart** (one chart is enough — the EA manages all symbols).
5. In the EA inputs, paste your **bridge token** from the Accounts page.
6. Enable **Allow Algorithmic Trading** (the button in the MT5 toolbar must be green).

## How you know it works
Within ~60 seconds the account card on MT5 Accounts flips to CONNECTED and shows the EA version. The EA sends heartbeats with balance, equity and open positions.

## Keep the EA updated
The Accounts page shows the current EA version (v1.55+ required for live execution). Older EAs keep reporting data but cannot execute live trades.`,
    },
    {
        slug: "vps-quick-connect", cat: "mt5", title: "Run the bot 24/7 with a VPS",
        summary: "Why a VPS matters and how pairing works.",
        body: `## Why a VPS?
The EA only works while your MT5 terminal is running. If your PC sleeps, the bridge goes offline — the bot cannot open, manage or close trades. A VPS (a small always-on Windows server) keeps the terminal alive 24/7.

## Quick Connect (Trader tier and up)
1. Open **Infrastructure** and start the pairing flow — you get a short-lived pairing code.
2. On your VPS, run the STOIC installer (PowerShell one-liner shown in the wizard).
3. The installer downloads the signed EA build, verifies its checksum, claims your pairing code and writes the installation identity file.
4. The dashboard shows the deployment as ACTIVE once the first verified heartbeat arrives.

## Safety notes
- Every artifact is integrity-signed; the installer refuses tampered builds.
- Execution uses single-writer leases — two terminals can never double-trade one account.`,
    },
    // ── Bot & Risk ─────────────────────────────────────────────────────
    {
        slug: "risk-profiles", cat: "bot", title: "Risk profiles explained (Low / Medium / High)",
        summary: "What actually changes when you move the risk slider.",
        body: `## What a profile controls
Your risk profile sets the **risk % per trade** budget, which the sizing engine then adapts per trade using confidence, volatility, your recent accuracy and current drawdown. High confidence in calm markets sizes up; drawdown throttles everything down hard.

## Guidelines
- **Low** — capital preservation first. Smallest positions, strictest vetoes.
- **Medium** — the balanced default for most users.
- **High** — larger budget per trade. Only after weeks of verified results.

## What a profile does NOT do
It never disables the safety systems. Daily/weekly/monthly drawdown ladders, exposure caps, anti-tilt cooldowns and the veto cascade run at every risk level.

## Change it
Bot Config → Risk Level. Raising risk on a live account requires step-up MFA if you have 2FA enrolled — that is intentional.`,
    },
    {
        slug: "trading-modes", cat: "bot", title: "Demo, Supervised Live and Autonomous Live",
        summary: "The three operational modes and the certification gate.",
        body: `## The ladder
1. **Demo + Shadow** — the bot trades a demo account and/or shadow-logs what it would have done. Zero capital at risk. Available on every tier.
2. **Supervised Live** — real trades at reduced size with the full guardrail stack. Trader tier and up.
3. **Autonomous Live** — full-size autonomous execution. Elite AI tier **and** your account must pass STOIC's certification campaign (a verified demo track record). A subscription alone never unlocks this.

## Recommendation
Run Demo until you have watched the bot through at least one full week — including a news day. Then graduate deliberately. The Verified Performance page tracks the record you build.`,
    },
    {
        slug: "why-bot-silent", cat: "bot", title: "Why is my bot not trading?",
        summary: "Silence is usually the guardrails doing their job.",
        body: `## First: check the Bot Pulse
The Dashboard BOT PULSE panel shows the bot's latest verdict for every account, every cycle — including the exact reason it held (e.g. "consensus 52 < 55", "cooldown active", "market closed").

## The most common legitimate reasons
- **Market closed / EOD quiet window** — no orders near the daily close when spreads spike.
- **Cooldowns** — after a loss the same setup is blocked for a while (anti-tilt).
- **Drawdown ladder** — after a bad day the bot stands down for the window. This is a feature.
- **No edge** — ranging, conflicted markets can legitimately produce nothing for hours.
- **News freeze** — high-impact events (FOMC, CPI, NFP) freeze new entries around the print.

## When it is an actual problem
- Account shows DISCONNECTED → your EA / terminal / VPS is offline.
- Bot toggle is OFF in Bot Config (auto-heal can switch it off after severe drawdown).
- Subscription lapsed → live trading pauses (banner shows on the Dashboard).`,
    },
    {
        slug: "explainable-ai", cat: "bot", title: "Reading trade explanations (DNA & Replay)",
        summary: "Every trade can tell you exactly why it happened.",
        body: `## Decision DNA
On the Trades page, open any trade and press **DNA**. You get the full decision snapshot: consensus votes per agent, confidence calibration, Monte Carlo odds, sizing components and which guards were consulted.

## Replay Studio
Press **REPLAY** to step through the trade's flight recorder: decision → entry → fills → management events (breakeven, partials, trailing) → close, in order, with timestamps.

## Loss Lab
Losing trades get an automatic AI post-mortem: what the market did, what the bot believed, and the mistake taxonomy. Recurring mistakes automatically tighten future behavior (wider stops, higher confidence floors) — you can see and revert every adjustment.`,
    },
    // ── Billing ────────────────────────────────────────────────────────
    {
        slug: "manage-subscription", cat: "billing", title: "Subscribe, upgrade and downgrade",
        summary: "How checkout, proration and scheduled downgrades work.",
        body: `## Subscribe
1. Open **Subscription**, pick a duration (annual saves 40%) and a tier.
2. Click SUBSCRIBE — you are redirected to Stripe Checkout (we never see your card).
3. After payment you land back on STOIC; activation is instant.

## Upgrade mid-term
Your unused days convert into equal-value days of the new tier automatically — the card shows exactly how many days you will be credited before you pay.

## Downgrade
The cheaper plan is **scheduled** to start when your current pass ends. You never lose time you already paid for.

## Renewals
Plans are prepaid and never auto-renew. We email you 7 days and 1 day before a pass lapses. Your billing history (every payment and its status) is at the bottom of the Subscription page.`,
    },
    {
        slug: "refunds-disputes", cat: "billing", title: "Refunds, failed payments and disputes",
        summary: "What happens to your access and how to get help.",
        body: `## Failed or interrupted checkout
If you closed the Stripe page early, nothing was charged — just click SUBSCRIBE again. If the confirmation page says "Session expired", the same applies.

## Refunds
Open a support ticket with category **Billing** and include the approximate payment date and plan. When a refund is issued, the purchased period is removed from your access automatically.

## Disputes / chargebacks
A dispute automatically revokes the disputed period. Please talk to us first — most billing issues are resolved within one business day via the Support page.`,
    },
    // ── Troubleshooting ────────────────────────────────────────────────
    {
        slug: "ea-disconnected", cat: "trouble", title: "EA shows DISCONNECTED",
        summary: "The 6 checks that fix 95% of bridge issues.",
        body: `## Run these in order
1. **Terminal running?** MT5 must stay open (this is why we recommend a VPS).
2. **Algo trading enabled?** The toolbar button must be green; the EA smiley on the chart must not be sad.
3. **Right token?** The EA input must contain the bridge token of THIS account (Accounts page → copy).
4. **Identity match?** The account number + broker server in STOIC must match the terminal exactly. Fix any typo and save.
5. **EA version?** v1.55+ is required for live execution. Download the latest from the Accounts page and recompile (F7).
6. **Firewall / antivirus?** Allow MT5 outbound HTTPS.

## Still stuck?
Open the Accounts page → DIAGNOSTICS on the account card for a live checklist, or open a support ticket with category **Technical** — include your broker name and EA version.`,
    },
    {
        slug: "login-issues", cat: "trouble", title: "Login, 2FA and email code problems",
        summary: "Locked out? Here's the recovery path for each case.",
        body: `## Wrong password
Use FORGOT PASSWORD on the login page. Reset links are single-use and expire quickly. New passwords must be 8+ characters and not appear in known breach lists.

## Authenticator (TOTP) code rejected
Codes are time-based — check your phone's clock is set to automatic. If you lost the device, use one of the recovery codes you saved at enrollment, then re-enroll 2FA in Settings.

## Email sign-in code (OTP)
If your admin enabled email codes: the code lasts 10 minutes, 5 attempts max. Use RESEND CODE (30s cooldown). Check spam for mail from STOIC.

## Too many attempts
Lockouts are temporary (about 10 minutes) and per-account. Waiting it out is the fix — hammering extends nothing.

## Account suspended banner
Contact support via the Support page (category **Account**) to appeal.`,
    },
    {
        slug: "data-sync", cat: "trouble", title: "Trades or balance look wrong",
        summary: "Force a deep broker sync and understand ghost closes.",
        body: `## The bot self-heals
Every heartbeat reconciles STOIC's records against your broker's terminal. Positions closed outside STOIC (manually, or by another EA) are detected and back-filled automatically.

## Force a deep sync
MT5 Accounts → account card → REQUEST SYNC pulls 7 days of deal history straight from the broker and repairs any divergence. The card shows a badge until your EA confirms completion.

## Why a trade says "P&L estimated"
If a close happened while the bridge was offline, STOIC estimates the P&L until the broker's deal record arrives, then replaces it with the exact number. Estimated trades are excluded from AI training so they can never poison the models.`,
    },
];
