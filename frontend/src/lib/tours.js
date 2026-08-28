// Interactive guided tours — step definitions.
// Each step: route + data-testid selector (falls back to centered card).
export const TOURS = {
    "getting-started": {
        title: "Getting Started",
        steps: [
            { route: "/", selector: "nav-dashboard", title: "Your Dashboard", body: "Everything starts here — equity, open trades, bot state and today's P&L in real time." },
            { route: "/", selector: "nav-accounts", title: "Connect MT5", body: "Add your MetaTrader 5 account under MT5 Accounts, then pair the STOIC host agent on your VPS." },
            { route: "/", selector: "nav-settings", title: "Secure with 2FA", body: "Open Settings → Security and enroll two-factor auth. Risk-raising actions always require a fresh code." },
            { route: "/", selector: "nav-subscription", title: "Pick a Plan", body: "Choose the plan that matches how many bots and accounts you want to run." },
        ],
    },
    "ai-trading-bot": {
        title: "Running the AI Bot",
        steps: [
            { route: "/", selector: "nav-signals", title: "AI Signals", body: "Every opportunity the brain evaluated — confidence, gates passed and why trades were skipped." },
            { route: "/", selector: "nav-commander", title: "Risk Commander", body: "Talk to your risk desk in plain language: reduce risk, pause a symbol, or ask why a trade happened." },
            { route: "/", selector: "nav-bot", title: "Bot Config", body: "Set your risk appetite and symbols. The Safety Guardian and Trading Authority veto anything outside limits." },
            { route: "/", selector: "nav-bot-health", title: "Bot Health", body: "One glance tells you if the pipeline, broker link and clocks are healthy." },
        ],
    },
    "pamm-manager": {
        title: "PAMM Manager Desk",
        steps: [
            { route: "/managed", selector: "nav-product-managed", title: "Managed Strategy", body: "Your PAMM operations desk — programs, NAV, investors and risk in one place." },
            { route: "/managed", selector: "pamm-strategy-panel", title: "Assign a Strategy", body: "Pick Sniper, Scalper, Fast Scalp or Nitro plus a risk profile. The exact version and hash get pinned." },
            { route: "/managed", selector: "pamm-cert-panel", title: "Earn Certification", body: "Walk the pipeline: REPLAY → SHADOW → DEMO → CANARY. Hard evidence gates, tamper-evident audit chain, then LIVE." },
            { route: "/managed", selector: "pamm-strategy-mode", title: "Fail-Closed Governance", body: "Once migrated, a program never silently reverts — every order is checked against the assignment and envelope." },
        ],
    },
    "investor-flow": {
        title: "Investing in a PAMM",
        steps: [
            { route: "/marketplace", selector: "nav-product-marketplace", title: "Browse Programs", body: "Published PAMM programs with verified track records, fees and risk limits." },
            { route: "/marketplace", selector: null, title: "Join a Program", body: "Send a join request with your deposit. Once the manager approves, your allocation tracks the program NAV." },
            { route: "/performance", selector: "nav-performance", title: "Monitor Performance", body: "Returns, drawdown and your allocation — reconciled against broker truth, always." },
        ],
    },
};
