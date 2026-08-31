// Fix-It Shortcuts — map every activation problem / promotion blocker /
// readiness code to the exact place in the app where the fix lives.

const TEXT_RULES = [
    { re: /command fencing|ea version unknown|update to the latest|intent journaling/i,
      label: "UPDATE EA", to: a => (a ? `/accounts?focus=${a}` : "/accounts") },
    { re: /never sent a heartbeat|heartbeat is stale|autotrading|attach the .*ea|terminal is running/i,
      label: "OPEN QUICK INSTALL", to: a => (a ? `/accounts?focus=${a}` : "/accounts") },
    { re: /certification|certified|provisional|degraded/i,
      label: "OPEN CERTIFICATION", to: () => "/certification" },
    { re: /not connected|broker account/i,
      label: "OPEN ACCOUNTS", to: a => (a ? `/accounts?focus=${a}` : "/accounts") },
    { re: /equity is unknown/i,
      label: "CHECK ACCOUNT", to: a => (a ? `/accounts?focus=${a}` : "/accounts") },
    { re: /soak/i, label: "OPEN BOT HEALTH", to: () => "/bot-health" },
    { re: /session window|outside .*session/i,
      label: "EDIT SESSION WINDOW", to: () => "/scalp" },
    { re: /panic|tripped|circuit breaker/i, label: "OPEN BOT CONFIG", to: () => "/bot" },
    { re: /step-up|totp|2fa|mfa/i, label: "OPEN SECURITY SETTINGS", to: () => "/settings" },
    { re: /reconcil|unknown execution|broker-accepted|unresolved|without a broker-confirmed/i,
      label: "OPEN TRADES", to: () => "/trades" },
    { re: /worker|lease|crashloop|stalled|outbox/i,
      label: "OPEN INFRASTRUCTURE", to: () => "/infrastructure" },
    { re: /heartbeat/i,
      label: "OPEN QUICK INSTALL", to: a => (a ? `/accounts?focus=${a}` : "/accounts") },
];

export function matchFixShortcut(text, accountId = null) {
    const s = String(text || "");
    for (const r of TEXT_RULES)
        if (r.re.test(s)) return { label: r.label, to: r.to(accountId) };
    return null;
}

// Structured 409 payloads: {code, message, problems|blockers|reasons: [...]}
export function extractBlockerDetail(err, accountId = null) {
    const detail = err?.response?.data?.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
    if (typeof detail.message !== "string") return null;
    const items = ["problems", "blockers", "reasons"]
        .flatMap(k => (Array.isArray(detail[k]) ? detail[k] : []))
        .map(x => (typeof x === "string" ? x : (x?.message || x?.msg || x?.detail)))
        .filter(Boolean)
        .map(text => ({ text, fix: matchFixShortcut(text, accountId) }));
    if (!items.length) return null;
    return { code: detail.code, message: detail.message, items };
}

// Readiness strip codes → direct deep link to the page where the fix lives.
const CODE_ROUTES = {
    PANIC_TRIPPED: { label: "OPEN BOT CONFIG", to: "/bot" },
    POSITION_TRUTH_STALE: { label: "OPEN ACCOUNTS", to: "/accounts" },
    EXECUTION_BLOCKED: { label: "OPEN ACCOUNTS", to: "/accounts" },
    RECONCILIATION_PENDING: { label: "OPEN TRADES", to: "/trades" },
    AUTHORITY_REDUCED: { label: "OPEN ACCOUNTS", to: "/accounts" },
    NO_ENABLED_ACCOUNTS: { label: "OPEN ACCOUNTS", to: "/accounts" },
    NO_BOTS_ENABLED: { label: "OPEN BOT CONFIG", to: "/bot" },
};

export function readinessFixRoute(code, accounts = []) {
    const base = CODE_ROUTES[code];
    if (!base) return null;
    const acc = accounts?.[0]?.account_id;
    return acc && base.to === "/accounts"
        ? { ...base, to: `/accounts?focus=${acc}` }
        : base;
}
