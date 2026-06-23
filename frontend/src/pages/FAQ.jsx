import { useState, useMemo } from "react";
import { Link } from "react-router-dom";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    HelpCircle, Search, ChevronDown, Rocket, Brain, ShieldCheck,
    Plug, CreditCard, Lock, MessageCircle, Wrench,
} from "lucide-react";

// ─── FAQ data ────────────────────────────────────────────────────────────────
// Sorted into clear, scannable categories. Each entry is self-contained — no
// dependency on dynamic state. Keep questions short, answers tight (≤ 5 lines).
const CATEGORIES = [
    { id: "start",   label: "Getting Started",  icon: Rocket },
    { id: "signals", label: "AI Signals",       icon: Brain },
    { id: "risk",    label: "Risk & Trading",   icon: ShieldCheck },
    { id: "mt5",     label: "MT5 Bridge",       icon: Plug },
    { id: "billing", label: "Billing & Plans",  icon: CreditCard },
    { id: "security",label: "Security & 2FA",   icon: Lock },
    { id: "alerts",  label: "Notifications",    icon: MessageCircle },
    { id: "trouble", label: "Troubleshooting",  icon: Wrench },
];

const FAQS = [
    // Getting Started ─────────────────────────────────────────────────────────
    { cat: "start", q: "What is STOIC?",
      a: "STOIC is a Dual-AI trading bot that scans live markets for Gold (XAUUSD) and Bitcoin (BTCUSD), runs every signal through 10 institutional-grade vetoes, and only executes the few that pass. Trades route to your MT5 account via a downloadable Expert Advisor (EA) bridge." },
    { cat: "start", q: "How do I start trading?",
      a: "1) Add an MT5 account in the MT5 Accounts page (paper account works for sandboxing). 2) Download the EA and attach it to a chart with your bridge token. 3) Configure risk in Bot Config and toggle Bot Active to ON. STOIC will start generating + executing signals automatically." },
    { cat: "start", q: "Do I need a paid plan to use STOIC?",
      a: "Paper accounts (sandbox) work on any plan — even trial. Live execution against a real MT5 account requires an active paid subscription. See the Billing page for your current plan status." },
    { cat: "start", q: "What brokers does STOIC support?",
      a: "Any broker with a MetaTrader 5 platform. The Accounts page has presets for the most common ones (RoboForex, IC Markets, Exness, Pepperstone, FXTM, OctaFX, XM, FBS, HotForex, Tickmill) — but you can type any broker manually." },
    { cat: "start", q: "How many MT5 accounts can I connect?",
      a: "Up to 5 different brokers × 3 accounts per broker = 15 live accounts. Paper accounts don't count toward these limits. Need more? Contact support to extend." },

    // AI Signals ─────────────────────────────────────────────────────────────
    { cat: "signals", q: "How are signals generated?",
      a: "Two engines run in parallel — Claude Sonnet 4.5 (semantic reasoning over market context, news, macro data) and a locally-trained Logistic Regression (probability-of-win classifier). Their outputs feed a 10-layer veto cascade that kills any signal that doesn't survive every gate." },
    { cat: "signals", q: "What are the 10 vetoes?",
      a: "In order: 1) Sentiment, 2) Macro Event Blackout, 3) Regime, 4) Entropy, 5) Meta-Labeler, 6) Multi-Timeframe Trend, 7) Learned Meta-Classifier, 8) A+ Confluence, 9) Min R:R, 10) DXY Gate (XAU only). A single block = HOLD." },
    { cat: "signals", q: "Why are most signals HOLD?",
      a: "By design. STOIC is *low frequency, high conviction* — most candidate setups don't deserve real money. Each HOLD card has a 'WHY HOLD' banner explaining the exact reason (veto fired, confidence too low, or Claude declined)." },
    { cat: "signals", q: "How do I read the Veto Cascade strip?",
      a: "Each pill shows one gate: ✓ = pass, ⚪ = skip (n/a), ✕ = block. Reading left-to-right matches STOIC's internal order. A signal needs every pill green or grey to fire." },
    { cat: "signals", q: "What does the Signal Strength score mean?",
      a: "For BUY/SELL signals, STOIC scores 6 components (Claude conviction, R:R, learned-meta p_win, A+ confluence depth, news sentiment, DXY alignment) and averages them. ≥80 = STRONG, 60-79 = SOLID, <60 = MARGINAL. Use it to size your trades — full Kelly on STRONG, half-Kelly on MARGINAL." },
    { cat: "signals", q: "Can I manually generate a signal?",
      a: "Yes — click 'GENERATE SIGNALS' on the AI Signals page. The bot will analyse all configured symbols and produce fresh signals. Useful for testing or before manually executing." },
    { cat: "signals", q: "How do I execute a signal manually?",
      a: "Each BUY/SELL card has an EXECUTE button. Click it, pick which MT5 account (paper or live), and confirm. The trade flows through the same pipeline as autonomous ones but tagged as `origin=manual_signal`." },

    // Risk & Trading ─────────────────────────────────────────────────────────
    { cat: "risk", q: "How does position sizing work?",
      a: "Kelly Criterion sized lot calculation, capped at 0.25 of full Kelly (conservative). The position scales with: account balance × Kelly fraction × confidence factor × regime multiplier ÷ stop-loss distance." },
    { cat: "risk", q: "What is anti-tilt?",
      a: "A capital-preservation circuit breaker. After N consecutive losing trades (default 3), STOIC pauses all new entries for X hours (default 4). Existing open trades aren't affected — only NEW signals are frozen." },
    { cat: "risk", q: "How does the daily/weekly drawdown circuit work?",
      a: "If your realised P&L for the day drops to -3% of equity (configurable), the bot auto-stops. Same for rolling 7-day at -7%. Resumes at next UTC rollover. Both are hard kill-switches the AI can't override." },
    { cat: "risk", q: "Why is the bot skipping Asia session for gold?",
      a: "XAUUSD chops sideways during the Asia session (00:00-07:00 UTC) with razor-thin moves and wide spreads. By default STOIC skips new gold entries in that window. Toggle in Bot Config → Capital Preservation." },
    { cat: "risk", q: "What is the slippage veto?",
      a: "Server-side check that runs after the EA fills your order. If the actual fill price is more than N pips from the intended entry (default 5 for XAU, 50 for BTC), the trade is auto-closed and a 'VETOED — slippage too wide' notice is logged." },

    // MT5 Bridge ─────────────────────────────────────────────────────────────
    { cat: "mt5", q: "How does the MT5 EA bridge work?",
      a: "1) You add an MT5 account → STOIC issues a unique bridge token. 2) You install the EA in MT5 with that token. 3) The EA polls /api/bridge/work every 5s for pending orders. 4) When it picks one up, it calls OrderSend() and reports the result back. STOIC is platform-agnostic — it doesn't directly trade your account." },
    { cat: "mt5", q: "My account shows as DISCONNECTED. How do I fix it?",
      a: "The EA hasn't sent a heartbeat in 5+ minutes. Check: (1) MT5 is open and AutoTrading is ON, (2) the EA is attached to a chart and showing 🙂 (not ☹️), (3) WebRequest URL is whitelisted in MT5 → Tools → Options → Expert Advisors, (4) the bridge token in the EA inputs matches the one on the Accounts page." },
    { cat: "mt5", q: "Where do I find my bridge token?",
      a: "Accounts page → click the eye icon next to your account label. Treat it like a password — anyone with that token can place trades on your MT5 account." },
    { cat: "mt5", q: "What is a paper account?",
      a: "An internal sandbox that simulates trade execution without touching any real broker. Use it to test the full signal-to-execution pipeline, evaluate STOIC's edge, and try config changes risk-free. P&L is calculated as if filled at the live price." },
    { cat: "mt5", q: "Can I use the same MT5 account on two devices?",
      a: "Yes, but only ONE MT5 terminal should be running the EA per bridge token — otherwise you'll see duplicate fills. If you need two terminals, create a second MT5 account in STOIC (counts as 1 of your 3 per-broker slots)." },

    // Billing ────────────────────────────────────────────────────────────────
    { cat: "billing", q: "How can I see my current plan?",
      a: "The Billing page shows your active plan, expiration date, and full transaction history. The plan card uses color-coded badges: green ● ACTIVE, amber ● GRACE PERIOD, gold ● PERMANENT, red ○ INACTIVE." },
    { cat: "billing", q: "What plans are available?",
      a: "Monthly, Quarterly, and Annual. See the Subscription page for current pricing and feature breakdown." },
    { cat: "billing", q: "What happens when my subscription expires?",
      a: "You get a 7-day grace period during which live trading still works. After grace ends, live execution is blocked — paper trading remains available. Renew anytime from the Billing or Subscription page." },
    { cat: "billing", q: "Why is my transaction stuck on PENDING?",
      a: "PENDING means a checkout session was created but never completed (you closed the Stripe tab or didn't finish payment). They don't count toward your subscription — just complete a fresh checkout to activate the plan." },
    { cat: "billing", q: "How do I cancel my subscription?",
      a: "Go to Subscription → click 'Manage Plan' → 'Cancel'. Your access continues until the current period's expiration date (no refund for unused time, but no further charges either). Contact support for special cases." },

    // Security ───────────────────────────────────────────────────────────────
    { cat: "security", q: "How do I enable Two-Factor Authentication?",
      a: "Settings → Section 03 → 'ENABLE 2FA'. Scan the QR with Google Authenticator / Authy / 1Password, type the 6-digit code to confirm, then save the 8 recovery codes shown (they appear ONLY ONCE)." },
    { cat: "security", q: "What if I lose my authenticator app?",
      a: "Use any one of the 8 recovery codes you saved at enrolment — paste it as the '2FA code' on login or when disabling 2FA. Each code is single-use. If you've lost both the app AND your codes, contact support to verify identity and reset." },
    { cat: "security", q: "How do I change my password?",
      a: "Settings → Section 02 → enter current password + new password (≥6 chars) + confirm. You stay signed in on this device after the change; other devices are forced to re-login." },
    { cat: "security", q: "Where is my data stored?",
      a: "Account credentials, MT5 tokens, and trade data are in our MongoDB cluster with encryption-at-rest. MT5 investor/master passwords (if you supply them) are encrypted via a server-side vault using a per-environment key." },
    { cat: "security", q: "Is my Stripe payment info stored on STOIC?",
      a: "No. STOIC never sees or stores your card details — Stripe handles the entire checkout flow on their own infrastructure. STOIC only keeps the session ID + plan + amount + status for your transaction history." },

    // Notifications ──────────────────────────────────────────────────────────
    { cat: "alerts", q: "How do I set up Telegram alerts?",
      a: "Notifications page → create a Telegram bot via @BotFather → paste your bot token + chat ID. STOIC will then send 'Trade Opened', 'Trade Closed', 'SL Imminent', and 'Circuit Breaker Tripped' alerts directly to that chat." },
    { cat: "alerts", q: "Which events get sent to Telegram?",
      a: "Trade opened (with entry/SL/TP), trade closed (with P&L), SL approaching within velocity ETA, high-confidence signal (≥75%), and any circuit-breaker trip (daily/weekly drawdown, anti-tilt freeze)." },
    { cat: "alerts", q: "I got a Telegram alert but don't see the trade in STOIC. Why?",
      a: "This used to happen due to test fixtures leaking fake alerts — fixed in iter-15 with a 3-layer guard. If it happens again, the trade probably had an implausible price (>50% from live quote) and was correctly filtered. Check /trades for any 'failed' or 'cancelled' entries near the alert time." },

    // Troubleshooting ────────────────────────────────────────────────────────
    { cat: "trouble", q: "Why is the bot generating signals but never opening trades?",
      a: "Check: 1) Bot is Active (Bot Config), 2) Auto-Execute is ON, 3) Confidence threshold isn't too high (default 75%), 4) MT5 account is connected (recent heartbeat), 5) You're not at max_concurrent_trades, 6) No capital-preservation guard is tripped (Dashboard shows tripped status)." },
    { cat: "trouble", q: "How do I clear old signals?",
      a: "AI Signals page → CLEAR button (top-right). You can clear: HOLD signals only, signals older than 1d/7d, or ALL. Each option asks for confirmation before deletion." },
    { cat: "trouble", q: "How do I clear old trades?",
      a: "Trades page → CLEAR button. You can clear closed, cancelled, failed trades, or filter by age (>7d, >30d). Open and pending trades are PROTECTED and can never be deleted through this — close them properly first." },
    { cat: "trouble", q: "The dashboard shows BOT STOPPED. How do I start it?",
      a: "Bot Config → toggle 'Bot Active' to ON (top of page). The bot starts polling on the next cycle (within 60 seconds). You'll see the LIVE indicator turn green in the top-right." },
    { cat: "trouble", q: "Where do I see why a specific signal was blocked?",
      a: "AI Signals page → find the signal card → look for the 'WHY HOLD' banner directly below the Veto Cascade strip. It tells you the exact gate that blocked it in plain English. For VETO cases, the AI Reasoning paragraph below the card has the full explanation." },
];

// ─── Page ────────────────────────────────────────────────────────────────────
export default function FAQ() {
    const [query, setQuery] = useState("");
    const [category, setCategory] = useState("all");
    const [openIds, setOpenIds] = useState(new Set());

    const toggle = (id) => {
        setOpenIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        return FAQS.filter(f => {
            if (category !== "all" && f.cat !== category) return false;
            if (!q) return true;
            return f.q.toLowerCase().includes(q) || f.a.toLowerCase().includes(q);
        });
    }, [query, category]);

    const grouped = useMemo(() => {
        const g = {};
        filtered.forEach(f => {
            if (!g[f.cat]) g[f.cat] = [];
            g[f.cat].push(f);
        });
        return g;
    }, [filtered]);

    const counts = useMemo(() => {
        const c = { all: FAQS.length };
        FAQS.forEach(f => { c[f.cat] = (c[f.cat] || 0) + 1; });
        return c;
    }, []);

    return (
        <AppLayout>
            <PageHeader
                title="FAQ"
                subtitle="Quick answers to the things people ask most about STOIC."
                testid="faq-header"
            />

            <div className="p-4 md:p-8 space-y-4 max-w-4xl">
                {/* Search */}
                <div className="relative">
                    <Search className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
                    <input value={query} onChange={e => setQuery(e.target.value)}
                        data-testid="faq-search"
                        placeholder="Search FAQs… (e.g. '2FA', 'anti-tilt', 'bridge token')"
                        className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none pl-9 pr-3 py-2.5 text-sm font-mono transition-colors" />
                </div>

                {/* Category chips */}
                <div className="flex flex-wrap gap-2" data-testid="faq-categories">
                    <CatChip id="all" label="ALL" count={counts.all} active={category === "all"} onClick={() => setCategory("all")} />
                    {CATEGORIES.map(c => (
                        <CatChip key={c.id} id={c.id} label={c.label.toUpperCase()}
                            count={counts[c.id] || 0}
                            active={category === c.id}
                            icon={c.icon}
                            onClick={() => setCategory(c.id)} />
                    ))}
                </div>

                {/* Results */}
                {filtered.length === 0 ? (
                    <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="faq-empty">
                        <HelpCircle className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                        <div className="font-display font-bold text-lg mb-1">No FAQs match your search</div>
                        <div className="text-sm text-[#A1A1AA]">Try a different keyword or clear the filter.</div>
                    </div>
                ) : (
                    <div className="space-y-6" data-testid="faq-results">
                        {CATEGORIES.filter(c => grouped[c.id]?.length).map(cat => (
                            <FAQSection key={cat.id} cat={cat} items={grouped[cat.id]} openIds={openIds} toggle={toggle} />
                        ))}
                    </div>
                )}

                {/* Contact footer */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 flex items-start gap-3 mt-6">
                    <MessageCircle className="w-4 h-4 text-[#00FF41] shrink-0 mt-0.5" />
                    <div className="flex-1">
                        <div className="font-display font-bold text-sm mb-1">Didn&apos;t find your answer?</div>
                        <div className="text-xs text-[#A1A1AA] leading-relaxed">
                            Use the <Link to="/commander" className="text-[#00FF41] hover:underline">Risk Commander</Link> chat for trading-strategy questions,
                            or check the <Link to="/notifications" className="text-[#00FF41] hover:underline">Notifications</Link> page to reach out via Telegram support.
                        </div>
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}

function CatChip({ id, label, count, active, icon: Icon, onClick }) {
    const activeCls = active
        ? "border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]"
        : "border-[#1F1F1F] text-[#52525B] hover:text-[#A1A1AA] hover:border-[#333333]";
    return (
        <button type="button" onClick={onClick}
            data-testid={`faq-cat-${id}`}
            disabled={count === 0 && id !== "all"}
            className={`font-mono text-[10px] tracking-widest px-2.5 py-1.5 border transition-colors flex items-center gap-1.5 disabled:opacity-30 ${activeCls}`}>
            {Icon && <Icon className="w-3 h-3" />}
            {label} <span className="font-display font-bold">{count}</span>
        </button>
    );
}

function FAQSection({ cat, items, openIds, toggle }) {
    const Icon = cat.icon;
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid={`faq-section-${cat.id}`}>
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Icon className="w-4 h-4 text-[#00FF41]" />
                <div className="font-display font-bold text-base tracking-tight">{cat.label}</div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest ml-auto">{items.length} ENTR{items.length === 1 ? "Y" : "IES"}</div>
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                {items.map((f, i) => {
                    const id = `${f.cat}-${i}`;
                    const open = openIds.has(id);
                    return (
                        <div key={id} data-testid={`faq-item-${id}`}>
                            <button type="button" onClick={() => toggle(id)}
                                data-testid={`faq-toggle-${id}`}
                                className="w-full px-5 py-3 text-left flex items-start gap-3 hover:bg-[#050505] transition-colors">
                                <ChevronDown className={`w-4 h-4 text-[#52525B] shrink-0 mt-0.5 transition-transform ${open ? "rotate-180 text-[#00FF41]" : ""}`} />
                                <span className="font-display font-medium text-sm flex-1">{f.q}</span>
                            </button>
                            {open && (
                                <div className="px-5 pb-4 pl-12 text-sm text-[#A1A1AA] leading-relaxed">
                                    {f.a}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </section>
    );
}
