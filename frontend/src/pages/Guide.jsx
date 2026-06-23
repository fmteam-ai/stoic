import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    BookOpen, Sparkles, Brain, Target, CheckCircle2, ArrowRight, ShieldCheck,
    Layers, Rocket, Settings as SettingsIcon, Zap, Clock, TrendingUp, AlertTriangle,
    Plane, Power, Repeat, Eye, Cpu, Search, Send, Bookmark, DollarSign,
} from "lucide-react";

// ─── Table-of-contents ──────────────────────────────────────────────────────
const SECTIONS = [
    { id: "intro",        title: "1. What is STOIC?",                  icon: BookOpen },
    { id: "edge",         title: "2. Why STOIC is different",          icon: Sparkles },
    { id: "engine",       title: "3. The Dual-AI engine",              icon: Brain },
    { id: "agents",       title: "4. The Multi-Agent Architecture",    icon: Cpu },
    { id: "cascade",      title: "5. The 10-Layer Veto Cascade",       icon: Layers },
    { id: "risk",         title: "6. Risk & Capital Preservation",     icon: ShieldCheck },
    { id: "setup",        title: "7. Setup — sign-up to autopilot",    icon: Rocket },
    { id: "autopilot",    title: "8. Autopilot mode explained",        icon: Plane },
    { id: "daily",        title: "9. Daily 5-min routine",             icon: Clock },
    { id: "advanced",     title: "10. Advanced — tuning & auditing",   icon: SettingsIcon },
    { id: "presets",      title: "11. Strategy Presets",                icon: Bookmark },
    { id: "going-live",   title: "12. Going live (paper → real)",      icon: TrendingUp },
    { id: "affiliate",    title: "13. Earn 20% recurring (affiliate)", icon: DollarSign },
    { id: "faq",          title: "14. Quick links",                    icon: Zap },
];

export default function Guide() {
    const [active, setActive] = useState("intro");

    // Highlight TOC entry as the user scrolls
    useEffect(() => {
        const observer = new IntersectionObserver(
            entries => {
                entries.forEach(e => { if (e.isIntersecting) setActive(e.target.id); });
            },
            { rootMargin: "-30% 0px -60% 0px", threshold: 0 },
        );
        SECTIONS.forEach(s => {
            const el = document.getElementById(s.id);
            if (el) observer.observe(el);
        });
        return () => observer.disconnect();
    }, []);

    return (
        <AppLayout>
            <PageHeader
                title="The STOIC Guide"
                subtitle="Everything you need — what it is, why it works, and how to use every feature."
                testid="guide-header"
            />

            <div className="flex flex-col lg:flex-row gap-6 p-4 md:p-8">
                {/* TOC — sticky on desktop */}
                <aside className="lg:w-64 shrink-0">
                    <nav className="lg:sticky lg:top-4 border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="guide-toc">
                        <div className="px-4 py-3 border-b border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                            ON THIS PAGE
                        </div>
                        <ul className="py-2">
                            {SECTIONS.map(s => (
                                <li key={s.id}>
                                    <a href={`#${s.id}`}
                                        data-testid={`toc-${s.id}`}
                                        className={`flex items-center gap-2 px-4 py-1.5 text-xs transition-colors border-l-2 ${
                                            active === s.id
                                                ? "border-[#00FF41] text-[#00FF41] bg-[#00FF41]/5"
                                                : "border-transparent text-[#A1A1AA] hover:text-white hover:border-[#1F1F1F]"
                                        }`}>
                                        <s.icon className="w-3 h-3 shrink-0" />
                                        <span>{s.title}</span>
                                    </a>
                                </li>
                            ))}
                        </ul>
                    </nav>
                </aside>

                {/* Body */}
                <article className="flex-1 max-w-3xl space-y-12">
                    <Intro />
                    <Edge />
                    <Engine />
                    <Agents />
                    <Cascade />
                    <RiskSection />
                    <SetupSteps />
                    <Autopilot />
                    <DailyFlow />
                    <Advanced />
                    <PresetsSection />
                    <GoingLive />
                    <AffiliateSection />
                    <QuickLinks />
                </article>
            </div>
        </AppLayout>
    );
}

// ─── Sections ───────────────────────────────────────────────────────────────
function H2({ id, icon: Icon, children }) {
    return (
        <h2 id={id} className="scroll-mt-8 font-display font-bold text-2xl tracking-tight flex items-center gap-3 border-b border-[#1F1F1F] pb-3 mb-4">
            <Icon className="w-5 h-5 text-[#00FF41]" />
            {children}
        </h2>
    );
}

function P({ children }) { return <p className="text-sm text-[#A1A1AA] leading-relaxed">{children}</p>; }

function Callout({ kind = "info", children }) {
    const palette = {
        info: { bd: "border-[#1F1F1F]", bg: "bg-[#0A0A0A]", fg: "text-[#A1A1AA]" },
        good: { bd: "border-[#00FF41]/30", bg: "bg-[#00FF41]/10", fg: "text-[#00FF41]" },
        warn: { bd: "border-[#FFB000]/30", bg: "bg-[#FFB000]/10", fg: "text-[#FFB000]" },
    }[kind];
    return (
        <div className={`px-4 py-3 border ${palette.bd} ${palette.bg} text-sm ${palette.fg} leading-relaxed`}>
            {children}
        </div>
    );
}

function CTAButton({ to, children, testid }) {
    return (
        <Link to={to} data-testid={testid}
            className="inline-flex items-center gap-2 bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium px-4 py-2 text-xs tracking-widest transition-colors">
            {children} <ArrowRight className="w-3.5 h-3.5" />
        </Link>
    );
}

function Step({ n, title, children, testid }) {
    return (
        <div className="flex gap-4" data-testid={testid}>
            <div className="shrink-0 w-8 h-8 rounded-full bg-[#00FF41]/10 border border-[#00FF41]/40 flex items-center justify-center font-display font-bold text-sm text-[#00FF41]">{n}</div>
            <div className="flex-1 pt-0.5">
                <div className="font-display font-bold text-base mb-1">{title}</div>
                <div className="text-sm text-[#A1A1AA] leading-relaxed space-y-2">{children}</div>
            </div>
        </div>
    );
}

function Intro() {
    return (
        <section>
            <H2 id="intro" icon={BookOpen}>1. What is STOIC?</H2>
            <P>
                STOIC is a fully-automated trading bot for <strong className="text-white">Gold (XAUUSD)</strong> and{" "}
                <strong className="text-white">Bitcoin (BTCUSD)</strong>. Once configured, it runs on{" "}
                <strong className="text-[#00FF41]">complete autopilot</strong> — analysing live market conditions every
                60 seconds, generating signals, filtering them, and executing the survivors directly on your MT5 broker
                account via a downloadable Expert Advisor (EA).
            </P>
            <P>
                Unlike typical retail bots that fire on a single indicator, STOIC runs every candidate trade
                through a <strong className="text-white">10-layer veto cascade</strong> driven by a dual-AI engine
                (Claude Sonnet 4.5 + a locally-trained Logistic Regression). The result is{" "}
                <em>low frequency, high conviction</em> — STOIC trades less than 10% of the candidate signals it sees.
            </P>
            <Callout kind="good">
                <strong>TL;DR — How to put STOIC on autopilot in 10 minutes:</strong> Section 6 walks you through the
                6-step setup (2FA → MT5 account → EA bridge → config → first signal → flip the switch). Section 7
                explains exactly what the bot does automatically while you sleep.
            </Callout>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-4">
                <Stat label="MARKETS" value="Gold + BTC" />
                <Stat label="VETO LAYERS" value="10" />
                <Stat label="AI ENGINES" value="Dual" />
                <Stat label="EXECUTION" value="MT5 Bridge" />
            </div>
        </section>
    );
}

function Stat({ label, value }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className="font-display font-bold text-base text-white">{value}</div>
        </div>
    );
}

function Edge() {
    const points = [
        { good: true,  l: "Dual-AI cross-check",
          r: "Claude provides semantic reasoning over news, macro, and price action; a separate Logistic Regression returns probability-of-win. Disagreement = HOLD." },
        { good: true,  l: "Institutional-grade macro layer",
          r: "Kalman filter, COT positioning, TIPS real yields, DXY inverse-correlation. Most retail bots have none of these." },
        { good: true,  l: "Session-aware learned model",
          r: "Trains separate models for Asia / London / NY — gold behaves differently per session." },
        { good: true,  l: "Capital-preservation guards",
          r: "Daily + weekly drawdown circuit, anti-tilt freeze, trade-of-day cap, Asia-session skip — all hard-coded protections the AI can't override." },
        { good: true,  l: "Explainable decisions",
          r: "Every signal shows which gates passed/blocked, plus Claude's plain-English reasoning. No black box." },
        { good: false, l: "What typical retail bots do",
          r: "Single indicator (RSI < 30, MA cross), no macro context, no veto stack, no auditability. They lose to gold's macro tape because they're trading patterns without context." },
    ];
    return (
        <section>
            <H2 id="edge" icon={Sparkles}>2. Why STOIC is different</H2>
            <P>The edge isn&apos;t in what STOIC chooses to trade — it&apos;s in what it{" "}
                <em className="text-[#FFD700]">refuses</em> to trade. Here&apos;s the side-by-side:</P>
            <div className="space-y-2 mt-4">
                {points.map(p => (
                    <div key={p.l} className={`border ${p.good ? "border-[#00FF41]/30 bg-[#00FF41]/5" : "border-[#FF3B30]/30 bg-[#FF3B30]/5"} px-4 py-3 flex items-start gap-3`}>
                        {p.good
                            ? <CheckCircle2 className="w-4 h-4 text-[#00FF41] shrink-0 mt-0.5" />
                            : <AlertTriangle className="w-4 h-4 text-[#FF3B30] shrink-0 mt-0.5" />}
                        <div>
                            <div className={`font-display font-bold text-sm ${p.good ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{p.l}</div>
                            <div className="text-xs text-[#A1A1AA] leading-relaxed mt-1">{p.r}</div>
                        </div>
                    </div>
                ))}
            </div>
        </section>
    );
}

function Engine() {
    return (
        <section>
            <H2 id="engine" icon={Brain}>3. The Dual-AI engine</H2>
            <P>Two independent decision-makers run in parallel on every candle:</P>
            <div className="grid md:grid-cols-2 gap-3 mt-4">
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
                    <div className="font-display font-bold text-base mb-2 flex items-center gap-2">
                        <span className="text-[#00FF41]">🤖</span> Engine A — Claude Sonnet 4.5
                    </div>
                    <div className="text-xs text-[#A1A1AA] leading-relaxed">
                        Reads a structured payload (price action, indicators, news sentiment, COT positioning, TIPS regime, DXY regime, Kalman state) and returns: action (BUY/SELL/HOLD), confidence 0–100, key drivers, full plain-English reasoning, and entry/SL/TP levels.
                    </div>
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
                    <div className="font-display font-bold text-base mb-2 flex items-center gap-2">
                        <span className="text-[#FFD700]">🔢</span> Engine B — Local Logistic Regression
                    </div>
                    <div className="text-xs text-[#A1A1AA] leading-relaxed">
                        Trained on YOUR own closed-trade history (no third-party data). Inputs: Claude confidence, action direction, Kalman velocity, COT/TIPS/MTF alignment, macro proximity. Outputs probability-of-win. Trains 3 session-specific models (Asia / London / NY).
                    </div>
                </div>
            </div>
            <Callout kind="good">
                <strong>Why both?</strong> Engine A understands narrative and context. Engine B learns from your actual results.
                When they disagree, STOIC defaults to HOLD — preventing both LLM hallucinations and ML over-fitting from costing you money.
            </Callout>
        </section>
    );
}

function Agents() {
    const roster = [
        {
            icon: Search, color: "#00FF41",
            name: "Research Agent",
            blurb: "The eyes & ears.",
            does: "Pulls every unstructured + macro signal needed to reason about the next 60s of the market.",
            data: [
                "News sentiment (NewsAPI)",
                "FRED macro: Fed Funds, 10Y yield, breakeven inflation, unemployment, VIX",
                "COT positioning (CFTC weekly)",
                "TIPS 10-Year real yield",
                "DXY snapshot (Yahoo + Stooq)",
                "Economic-calendar imminent events (NFP / CPI / FOMC)",
            ],
        },
        {
            icon: Brain, color: "#FFD700",
            name: "Strategy Agent",
            blurb: "The brain.",
            does: "Fuses Research's macro picture with live technicals into a candidate signal (BUY / SELL / HOLD + confidence + entry / SL / TP).",
            data: [
                "Claude Sonnet 4.5 — narrative reasoning",
                "Session-specific Logistic Regression — local hit-rate memory",
                "12 technical indicators (Kalman, ATR, RSI, MACD, Bollinger, MTF…)",
                "Kelly-criterion sizing + regime-adaptive risk modifier",
                "Built-in 10-layer veto cascade",
            ],
        },
        {
            icon: ShieldCheck, color: "#FF3B30",
            name: "Risk Agent",
            blurb: "The compliance officer.",
            does: "Operates on the WHOLE PORTFOLIO — vetoes candidate signals that look fine in isolation but stack hidden risk across positions.",
            data: [
                "Cross-asset correlation veto (XAU ↔ BTC under DXY stress)",
                "Daily & weekly drawdown circuits",
                "Anti-tilt freeze after N consecutive losses",
                "Pre-news position protector (flatten before NFP/CPI)",
                "Post-SL cooldown (revenge-trade blocker)",
            ],
        },
        {
            icon: Send, color: "#A855F7",
            name: "Execution Agent",
            blurb: "The hands.",
            does: "Takes approved signals and routes them to the right broker account, then watches the fill quality.",
            data: [
                "Picks default MT5 account (or first ●CONNECTED)",
                "Posts entry/SL/TP to the bridge — EA picks up within 5s",
                "Server-side slippage veto on fills",
                "Breakeven shift · partial close at TP1 · trailing SL until close",
            ],
        },
    ];
    return (
        <section>
            <H2 id="agents" icon={Cpu}>4. The Multi-Agent Architecture</H2>
            <P>
                Inside STOIC, every tick is processed by <strong className="text-white">four named agents</strong>
                that hand off to each other in a strict pipeline. Each agent is independent,
                logged, and observable on the live <Link to="/agents" className="text-[#00FF41] hover:underline">Agents page</Link>.
            </P>
            <P>
                This split is what lets STOIC scale: each agent has one job, one set of inputs, one set of outputs.
                When something misfires, you can see exactly which agent fired (or vetoed) — no black box.
            </P>

            {/* The handoff diagram */}
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] my-5">
                <div className="px-4 py-2 border-b border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                    PIPELINE — RUNS EVERY 60s
                </div>
                <div className="px-4 py-3 flex items-center justify-between gap-2 flex-wrap">
                    {roster.map((a, i) => (
                        <div key={a.name} className="flex items-center gap-2">
                            <div className="flex items-center gap-1.5 px-2.5 py-1.5 border border-[#1F1F1F]">
                                <a.icon className="w-3.5 h-3.5" style={{ color: a.color }} />
                                <span className="font-mono text-[11px] tracking-wide">{a.name.replace(" Agent", "")}</span>
                            </div>
                            {i < roster.length - 1 && <ArrowRight className="w-3 h-3 text-[#52525B]" />}
                        </div>
                    ))}
                </div>
            </div>

            <div className="space-y-4 mt-4" data-testid="agent-roster-detail">
                {roster.map((a, i) => (
                    <div key={a.name} className="border border-[#1F1F1F] bg-[#0A0A0A]"
                        data-testid={`guide-agent-${a.name.toLowerCase().replace(" agent", "")}`}>
                        <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-3">
                            <div className="w-7 h-7 rounded-full border flex items-center justify-center shrink-0"
                                style={{ borderColor: a.color, color: a.color }}>
                                <a.icon className="w-3.5 h-3.5" />
                            </div>
                            <div>
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                    AGENT {i + 1} · {a.blurb.toUpperCase()}
                                </div>
                                <div className="font-display font-bold text-base tracking-tight">{a.name}</div>
                            </div>
                        </div>
                        <div className="p-5 space-y-3">
                            <p className="text-sm text-[#A1A1AA] leading-relaxed">{a.does}</p>
                            <div>
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">DATA · TOOLS</div>
                                <ul className="space-y-1">
                                    {a.data.map(d => (
                                        <li key={d} className="flex items-start gap-2 text-xs text-[#A1A1AA]">
                                            <CheckCircle2 className="w-3 h-3 mt-0.5 shrink-0" style={{ color: a.color }} />
                                            <span>{d}</span>
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        </div>
                    </div>
                ))}
            </div>

            <Callout kind="info">
                <strong>Why agents instead of one giant function?</strong> Single-script bots fail silently:
                a bad news fetch crashes the whole tick. STOIC&apos;s agents are isolated — if Research can&apos;t reach
                FRED, Strategy still runs with the data it has, and the activity log shows exactly which input was missing.
                On the live <Link to="/agents" className="text-[#00FF41] hover:underline">Agents page</Link>{" "}
                you can watch every handoff in real-time, with per-step timings and the current FRED macro snapshot.
            </Callout>
        </section>
    );
}


function Cascade() {
    const gates = [
        ["1. Sentiment Veto", "News tape disagrees with action"],
        ["2. Macro Event Blackout", "NFP / CPI / FOMC within ±15-30 min"],
        ["3. Regime Veto", "Extreme volatility requires conf ≥ 85%"],
        ["4. Entropy Veto", "Shannon entropy > 0.85 = chop"],
        ["5. Meta-Labeler", "Claude vs LR disagreement"],
        ["6. MTF Trend Gate", "M15 must match H1 direction"],
        ["7. Learned Meta-Classifier", "Session-specific p_win < threshold"],
        ["8. A+ Confluence", "Need ≥ 3 of: regime, COT, TIPS, Kalman, MTF"],
        ["9. Min R:R Gate", "Weighted-TP / SL < 1.5"],
        ["10. DXY Gate (XAU only)", "Fighting the dollar trend"],
    ];
    return (
        <section>
            <H2 id="cascade" icon={Layers}>5. The 10-Layer Veto Cascade</H2>
            <P>After both engines vote, the signal has to clear <strong className="text-white">every</strong> gate.
                A single block makes the signal HOLD:</P>
            <ol className="space-y-1.5 mt-4">
                {gates.map(([title, sub]) => (
                    <li key={title} className="flex items-start gap-3 border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2">
                        <Target className="w-3.5 h-3.5 text-[#00FF41] shrink-0 mt-1" />
                        <div className="flex-1">
                            <div className="font-display font-bold text-xs">{title}</div>
                            <div className="text-[11px] text-[#52525B]">{sub}</div>
                        </div>
                    </li>
                ))}
            </ol>
            <Callout>
                On the <Link to="/signals" className="text-[#00FF41] hover:underline">Signals page</Link> you&apos;ll see this cascade
                rendered as a color-coded pill strip on every signal card. 🟢 PASS · ⚪ SKIP · 🔴 BLOCK.
            </Callout>
        </section>
    );
}

function RiskSection() {
    const guards = [
        ["🛑 Daily Drawdown Circuit",        "Auto-stops bot at –3% of equity (configurable)"],
        ["🛑 Weekly Drawdown Circuit",       "Auto-stops at –7% rolling 7-day P&L"],
        ["❄ Anti-Tilt Freeze",               "Pauses new entries after N consecutive losses"],
        ["📅 Trade-of-Day Cap",              "Max N entries per symbol per UTC day"],
        ["🌙 Asia-Session XAU Skip",         "No new gold trades 00:00–07:00 UTC"],
        ["⏱ Post-SL Cooldown",               "Blocks re-entry on the same symbol for N min after a stop-out"],
        ["📣 Pre-News Position Protector",   "Auto-flattens open trades 5 min before NFP / CPI / FOMC"],
        ["💧 Liquidity-Window Booster",      "XAU: lowers conf-floor 3 pts in London-NY overlap; raises 4 pts in off-hours"],
        ["📐 ATR-Adaptive SL/TP",            "SL = 1.5 × ATR · TP = 5 × ATR — scales with current volatility regime"],
        ["💨 Server-Side Slippage Veto",     "Auto-closes trades filled >N pips off intent"],
        ["📊 Spread Filter",                  "Skip entries when broker spread > cap"],
    ];
    return (
        <section>
            <H2 id="risk" icon={ShieldCheck}>5. Risk & Capital Preservation</H2>
            <P>Position size is calculated via <strong className="text-white">Kelly Criterion</strong> (capped at
                0.25 of full Kelly) — scaled by your confidence + regime modifier ÷ stop-loss distance.</P>
            <P className="mt-3">Beyond sizing, these guards stop the bot when things go wrong:</P>
            <div className="space-y-1.5 mt-3">
                {guards.map(([t, sub]) => (
                    <div key={t} className="border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2 text-sm flex items-center justify-between gap-2">
                        <span className="font-display font-medium">{t}</span>
                        <span className="text-[11px] text-[#52525B] font-mono">{sub}</span>
                    </div>
                ))}
            </div>
            <Callout kind="warn">
                All guards are tunable in <Link to="/bot" className="underline text-[#FFB000]">Bot Config</Link> — but defaults are
                already calibrated for the average gold/BTC trader. Start with defaults; don&apos;t weaken them until you have data.
            </Callout>
        </section>
    );
}

function SetupSteps() {
    return (
        <section>
            <H2 id="setup" icon={Rocket}>7. Setup — sign-up to autopilot</H2>
            <P>Six steps. Roughly 10 minutes if you already have an MT5 broker account. After step 6, STOIC trades
                without you needing to touch it again.</P>
            <div className="space-y-5 mt-4">
                <Step n="1" title="Activate 2FA (recommended)" testid="step-2fa">
                    <p>Go to <Link to="/settings" className="text-[#00FF41] hover:underline">Settings</Link> → Section 03 → click <strong>ENABLE 2FA</strong>.
                        Scan the QR with Google Authenticator / Authy, enter the 6-digit code, then save the 8 recovery codes shown ONCE.</p>
                </Step>
                <Step n="2" title="Add an MT5 account" testid="step-account">
                    <p>Go to <Link to="/accounts" className="text-[#00FF41] hover:underline">MT5 Accounts</Link> → click <strong>ADD ACCOUNT</strong>.
                        Pick <strong>paper</strong> mode for sandboxing or <strong>live</strong> to trade real money. Choose a broker preset (RoboForex, Exness, IC Markets, …) and STOIC fills the typical server name.</p>
                </Step>
                <Step n="3" title="Install the EA bridge (live accounts only)" testid="step-ea">
                    <p>Click <strong>Download EA</strong> on your live account row → drop <code className="text-[#00FF41]">EmergentTradingBridge.mq5</code> into your MT5 <code>MQL5/Experts</code> folder. Attach it to any chart, paste the bridge token in the EA inputs, and make sure AutoTrading is ON + the STOIC URL is whitelisted in MT5 → Tools → Options → Expert Advisors → WebRequest.</p>
                    <Callout kind="warn">
                        <strong>For 24/7 autopilot:</strong> your MT5 terminal must stay open. Use a VPS (Forex VPS providers
                        run ~$5–15/mo) so the EA can poll for orders even when your laptop is off.
                    </Callout>
                </Step>
                <Step n="4" title="Configure your bot" testid="step-config">
                    <p><Link to="/bot" className="text-[#00FF41] hover:underline">Bot Config</Link> → pick a risk level (medium is fine to start), confirm the symbols (XAUUSD + BTCUSD), and leave the capital guards on defaults. Don&apos;t weaken anti-tilt or drawdown limits until you have data.</p>
                </Step>
                <Step n="5" title="Generate your first signal" testid="step-signal">
                    <p>Go to <Link to="/signals" className="text-[#00FF41] hover:underline">AI Signals</Link> → click <strong>GENERATE SIGNALS</strong>. Read the Veto Cascade strip + the WHY HOLD banner on each card to understand the bot&apos;s thinking. Most early signals will be HOLD — that&apos;s correct behaviour.</p>
                </Step>
                <Step n="6" title="🚀 Flip the autopilot switch" testid="step-go-live">
                    <p>This is the final step that puts STOIC on autopilot. In{" "}
                        <Link to="/bot" className="text-[#00FF41] hover:underline">Bot Config</Link>:</p>
                    <ul className="list-disc list-inside space-y-1.5 ml-2 text-[#A1A1AA]">
                        <li>Click <strong className="text-[#00FF41]">START BOT</strong> (top-right of Bot Config) —
                            the LIVE indicator on the sidebar turns green.</li>
                        <li>Make sure <strong className="text-[#00FF41]">Auto-Execute = ENABLED</strong> (Execution Behaviour card).
                            Without this, STOIC only generates signals — it won&apos;t place them.</li>
                        <li>Confirm at least one symbol is listed under <strong>Symbols</strong> (XAUUSD and/or BTCUSD).</li>
                        <li>On <Link to="/accounts" className="text-[#00FF41] hover:underline">MT5 Accounts</Link>, mark the
                            account you want orders routed to as <strong>Default</strong>. STOIC executes every signal on
                            this account.</li>
                    </ul>
                    <Callout kind="good">
                        That&apos;s it. STOIC will now scan every 60 seconds, generate signals, run them through the
                        10-layer cascade, and forward any survivors to your EA — fully autonomously.
                    </Callout>
                </Step>
            </div>
            <div className="mt-5 flex gap-3 flex-wrap">
                <CTAButton to="/accounts" testid="guide-start-cta">START WITH STEP 2</CTAButton>
                <CTAButton to="/bot" testid="guide-config-cta">JUMP TO BOT CONFIG</CTAButton>
            </div>
        </section>
    );
}

function Autopilot() {
    const lifecycle = [
        { icon: Repeat,  t: "Tick (every 60s)",     d: "STOIC pulls the latest XAU/BTC price, news, COT, TIPS and DXY data. The clock on Dashboard counts down to the next tick." },
        { icon: Brain,   t: "Dual-AI vote",         d: "Claude Sonnet 4.5 and the session-specific Logistic Regression each output an opinion. Disagreement → HOLD." },
        { icon: Layers,  t: "10-layer veto cascade",d: "Sentiment, macro blackout, regime, entropy, meta-labeler, MTF, learned-meta, A+ confluence, R:R, DXY gate." },
        { icon: Target,  t: "Position sizing",      d: "Kelly-fraction × confidence × regime mod ÷ SL distance. Bounded by your max-risk-per-trade." },
        { icon: Power,   t: "Order to EA",          d: "STOIC posts the entry/SL/TP to your bridge. The EA picks it up within 5s, calls MT5 OrderSend(), reports the fill back." },
        { icon: Eye,     t: "Server-side monitor",  d: "Slippage veto, breakeven shift, partial close at TP1, trailing SL — all managed server-side until close." },
        { icon: ShieldCheck, t: "Guards always-on", d: "Anti-tilt freeze, daily/weekly drawdown circuits, Asia-session XAU skip — they pause the bot automatically if triggered." },
    ];
    const checklist = [
        ["Bot status = LIVE", "Sidebar bottom-left + Dashboard header both show the green LIVE indicator."],
        ["Auto-Execute = ENABLED", "Bot Config → Execution Behaviour card."],
        ["At least one MT5 account connected", "Accounts page → ● CONNECTED dot within last 5 min."],
        ["A default account is set", "Accounts page → the row with the gold ★ badge."],
        ["Symbols list contains XAUUSD and/or BTCUSD", "Bot Config → Symbols & Risk card."],
        ["Capital-preservation toggles ON", "Daily DD, Weekly DD, Anti-Tilt, Slippage Veto. Defaults are fine."],
        ["Telegram alerts wired (optional but recommended)", "Notifications page → so you know when trades open/close while you're away."],
        ["MT5 terminal + EA running 24/7", "Local PC stays awake OR a VPS hosts the terminal."],
    ];
    return (
        <section>
            <H2 id="autopilot" icon={Plane}>8. Autopilot mode explained</H2>
            <P>
                Once you complete Step 6, STOIC operates on a closed loop with no human in the middle. Here&apos;s
                exactly what happens on every cycle:
            </P>

            <div className="border border-[#00FF41]/30 bg-[#00FF41]/5 mt-4">
                <div className="px-4 py-2 border-b border-[#00FF41]/20 font-mono text-[10px] text-[#00FF41] tracking-widest">
                    AUTOPILOT LIFECYCLE — RUNS CONTINUOUSLY
                </div>
                <ol className="divide-y divide-[#00FF41]/15">
                    {lifecycle.map((s, i) => (
                        <li key={s.t} className="flex items-start gap-3 px-4 py-3" data-testid={`autopilot-step-${i}`}>
                            <div className="shrink-0 w-6 h-6 rounded-full bg-[#00FF41]/10 border border-[#00FF41]/40 flex items-center justify-center font-mono text-[10px] text-[#00FF41]">
                                {i + 1}
                            </div>
                            <s.icon className="w-4 h-4 text-[#00FF41] shrink-0 mt-0.5" />
                            <div className="flex-1">
                                <div className="font-display font-bold text-sm">{s.t}</div>
                                <div className="text-xs text-[#A1A1AA] leading-relaxed mt-0.5">{s.d}</div>
                            </div>
                        </li>
                    ))}
                </ol>
            </div>

            <div className="mt-6">
                <div className="font-display font-bold text-base mb-3 flex items-center gap-2">
                    <CheckCircle2 className="w-4 h-4 text-[#00FF41]" />
                    Pre-flight checklist — confirm before walking away
                </div>
                <ul className="space-y-1.5" data-testid="autopilot-checklist">
                    {checklist.map(([t, d], i) => (
                        <li key={t} className="flex items-start gap-3 border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2"
                            data-testid={`autopilot-check-${i}`}>
                            <div className="w-4 h-4 border border-[#00FF41]/50 mt-0.5 shrink-0 flex items-center justify-center">
                                <CheckCircle2 className="w-3 h-3 text-[#00FF41]" />
                            </div>
                            <div className="flex-1">
                                <div className="font-display font-bold text-xs">{t}</div>
                                <div className="text-[11px] text-[#52525B] mt-0.5">{d}</div>
                            </div>
                        </li>
                    ))}
                </ul>
            </div>

            <Callout kind="warn">
                <strong>How to pause autopilot:</strong> Bot Config → click <strong>STOP BOT</strong>. The bot stops
                generating and executing immediately. Existing open trades stay open and are still managed (breakeven,
                trailing SL, slippage veto) until they hit TP/SL or you close them manually.
            </Callout>
            <Callout kind="info">
                <strong>How to verify autopilot is firing:</strong> the Dashboard&apos;s <em>Next Tick In</em> counter
                resets every 60s, the <em>Veto Count</em> card increments each cycle (most ticks produce HOLDs — that&apos;s
                normal), and any executed trade lands in <Link to="/trades" className="text-[#00FF41] hover:underline">Trades</Link>{" "}
                with a Telegram alert if you&apos;ve set that up.
            </Callout>
        </section>
    );
}

function DailyFlow() {
    const checks = [
        ["Open Dashboard", "Glance at the BOT STATUS row — LIVE indicator, open trade count, today's P&L, Veto Count card."],
        ["Check connection",   "Accounts page → all MT5 accounts should show ● CONNECTED. If any is stale, follow the FAQ disconnection fix."],
        ["Review HOLDs",       "AI Signals page → click ALL HOLDS to see what was skipped + why. Look for repeated BLOCKED vetoes — they tell you what's killing your edge."],
        ["Audit STRONG / MARGINAL", "If any actionable signal is MARGINAL, manually decide skip vs half-size. STRONG signals you can trust to autonomous execution."],
        ["Skim Trades log",    "Trades page → confirm any opened/closed trades match your expectations. Telegram alerts mirror this."],
        ["Read Risk Commander","Optional: chat with the Commander for ad-hoc questions like \"why did the bot pass on gold today?\""],
    ];
    return (
        <section>
            <H2 id="daily" icon={Clock}>9. Daily 5-min routine</H2>
            <P>Autopilot doesn&apos;t mean &quot;set and forget forever&quot;. About <strong className="text-white">5 minutes a day</strong> keeps you in the loop:</P>
            <ol className="space-y-1.5 mt-3 list-decimal list-inside marker:text-[#52525B] marker:font-mono">
                {checks.map(([t, sub]) => (
                    <li key={t} className="border-l-2 border-[#1F1F1F] hover:border-[#00FF41]/40 pl-3 py-1 transition-colors">
                        <span className="font-display font-bold text-sm">{t}</span>
                        <span className="text-xs text-[#A1A1AA] block mt-0.5">{sub}</span>
                    </li>
                ))}
            </ol>
            <Callout kind="good">
                STOIC is designed so you <em>don&apos;t</em> watch charts all day. If you find yourself doing that, raise the
                confidence threshold or tighten the trade-of-day cap — the bot is over-trading for your style.
            </Callout>
        </section>
    );
}

function Advanced() {
    return (
        <section>
            <H2 id="advanced" icon={SettingsIcon}>10. Advanced — tuning & auditing</H2>
            <div className="space-y-4 mt-3">
                <div>
                    <div className="font-display font-bold text-base mb-1.5">🔍 Auditing a specific signal</div>
                    <P>Open the signal card → scan the <strong className="text-white">Veto Cascade strip</strong> (find the red ✕ pill).
                        Then read the <strong className="text-white">WHY HOLD</strong> banner for the plain-English reason, and the full reasoning paragraph below for the macro context Claude used.</P>
                </div>
                <div>
                    <div className="font-display font-bold text-base mb-1.5">🎯 Reading the Signal Strength score</div>
                    <P>For BUY/SELL, the strength banner combines 6 components (Claude conviction, R:R, learned-meta p_win, A+ confluence, news sentiment, DXY alignment) into a 0–100 score.
                        <strong className="text-[#00FF41]"> ≥80 STRONG</strong> trust full Kelly · <strong className="text-[#A1A1AA]">60–79 SOLID</strong> normal size · <strong className="text-[#FFB000]">&lt;60 MARGINAL</strong> half-size or skip.</P>
                </div>
                <div>
                    <div className="font-display font-bold text-base mb-1.5">⚙️ Tuning the confidence floor</div>
                    <P>Default is 75%. Set <strong>Auto-Tune ON</strong> and the bot will adjust based on your hit rate. Manual: lower to 70% for more trades (lower quality); raise to 85% for fewer / higher-conviction (recommended after 2 weeks of paper trading).</P>
                </div>
                <div>
                    <div className="font-display font-bold text-base mb-1.5">📊 Reviewing performance</div>
                    <P>The <Link to="/analytics" className="text-[#00FF41] hover:underline">Analytics</Link> page breaks down win rate, expectancy, and P&L attribution per symbol, session, and signal-strength bucket. After 30+ closed trades it&apos;s your guide for tuning.</P>
                </div>
                <div>
                    <div className="font-display font-bold text-base mb-1.5">✋ Manual override</div>
                    <P>The yellow <strong>MANUAL TRADE</strong> button on Signals lets you place a position that bypasses every veto — useful for discretionary trades after news. Use sparingly; you&apos;re trading without the safety net.</P>
                </div>
            </div>
        </section>
    );
}

function GoingLive() {
    return (
        <section>
            <H2 id="going-live" icon={TrendingUp}>12. Going live (paper → real)</H2>
            <ol className="list-decimal list-inside space-y-2 marker:text-[#52525B] marker:font-mono">
                <li className="text-sm text-[#A1A1AA]"><strong className="text-white">Paper for 2+ weeks.</strong> Let the learned-meta classifier collect at least 30 closed trades per session before going live. The model needs data to filter your losers.</li>
                <li className="text-sm text-[#A1A1AA]"><strong className="text-white">Subscribe.</strong> Live execution requires an active paid plan — see the <Link to="/subscription" className="text-[#00FF41] hover:underline">Subscription</Link> page.</li>
                <li className="text-sm text-[#A1A1AA]"><strong className="text-white">Start small.</strong> Connect a microcent or cent account first (most brokers offer these). $50–$100 is plenty to validate that signal-to-execution works end-to-end.</li>
                <li className="text-sm text-[#A1A1AA]"><strong className="text-white">Watch the first 5 live trades.</strong> Confirm slippage is acceptable, fills happen within 1–2s, and Telegram alerts mirror what you see in STOIC. If anything is off, pause and investigate before scaling.</li>
                <li className="text-sm text-[#A1A1AA]"><strong className="text-white">Scale only after Analytics shows positive expectancy.</strong> 30 trades minimum. Don&apos;t scale on luck.</li>
            </ol>
            <Callout kind="warn">
                <strong>Realistic expectation:</strong> STOIC aims for ~55–65% win rate at 1.5–2.5 R:R — that translates to
                ~6–12% monthly when sized correctly. Anything claiming &quot;200% / month consistently&quot; is selling fantasy.
            </Callout>
        </section>
    );
}

function PresetsSection() {
    const presets = [
        { name: "Sniper",          tag: "Patient. Precise. A-grade only." },
        { name: "Scalper",         tag: "Fast in, fast out, many small wins." },
        { name: "Trend Rider",     tag: "Catch the wave, hold the line." },
        { name: "Breakout Hunter", tag: "Wait for volatility expansion." },
        { name: "Mean Reversion",  tag: "Buy weakness, sell strength." },
        { name: "Aggressive",      tag: "Macro-first. Force trades on clear bias." },
        { name: "Balanced",        tag: "STOIC's house defaults." },
    ];
    return (
        <section>
            <H2 id="presets" icon={Bookmark}>11. Strategy Presets</H2>
            <P>
                Presets are <strong className="text-white">one-click personality profiles</strong> for the bot. They overlay
                behaviour knobs (confidence floor, trade cap, trailing stop, cooldowns, news protector) onto your config — but
                leave <strong className="text-white">risk level, symbols, and drawdown limits untouched</strong>. Find them at
                the top of <Link to="/bot" className="text-[#00FF41] hover:underline">Bot Config</Link>.
            </P>

            <div className="font-display font-bold text-base mt-5 mb-2">The 7 built-in presets</div>
            <div className="grid sm:grid-cols-2 gap-2 mt-2">
                {presets.map(p => (
                    <div key={p.name} className="border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2"
                        data-testid={`guide-preset-${p.name.toLowerCase().replace(/\s+/g, "-")}`}>
                        <div className="font-display font-bold text-sm tracking-tight text-[#FFD700]">{p.name}</div>
                        <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">{p.tag}</div>
                    </div>
                ))}
            </div>

            <div className="font-display font-bold text-base mt-6 mb-2">Save your own preset</div>
            <P>
                Customise any built-in preset, tweak the trailing or cooldowns to your taste, then hit{" "}
                <strong className="text-[#FFD700]">SAVE CURRENT AS PRESET</strong> at the bottom of the presets grid. Name it,
                describe it, and it joins <strong className="text-white">Your Presets</strong> — apply again with one click.
                Limit: <strong>10 custom presets per account</strong>. Hover any custom card to reveal the trash icon to delete.
            </P>

            <Callout kind="info">
                Presets do <strong>not</strong> persist automatically — after applying, click{" "}
                <strong className="text-white">SAVE CONFIGURATION</strong> at the bottom of Bot Config to commit. This lets
                you preview a preset before committing.
            </Callout>

            <div className="mt-5">
                <CTAButton to="/bot" testid="guide-cta-presets">OPEN PRESETS</CTAButton>
            </div>
        </section>
    );
}

function AffiliateSection() {
    return (
        <section>
            <H2 id="affiliate" icon={DollarSign}>13. Earn 20% recurring (affiliate program)</H2>
            <P>
                Refer one trader — get paid every month they stay subscribed. STOIC pays{" "}
                <strong className="text-[#FFD700]">20% recurring commission</strong> on the base subscription fee for the
                <strong className="text-white"> full lifetime</strong> of each referred account. No caps, no clawbacks.
            </P>

            <div className="grid sm:grid-cols-2 gap-2 mt-4">
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">COMMISSION</div>
                    <div className="font-display font-bold text-lg text-[#FFD700]">20% recurring</div>
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">COOKIE WINDOW</div>
                    <div className="font-display font-bold text-lg">60 days</div>
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">MIN PAYOUT</div>
                    <div className="font-display font-bold text-lg">$50</div>
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">PAYOUT</div>
                    <div className="font-display font-bold text-lg">Stripe / PayPal</div>
                </div>
            </div>

            <div className="font-display font-bold text-base mt-6 mb-2">How to join</div>
            <div className="space-y-3">
                <Step n="1" title="Apply" testid="guide-aff-step-1">
                    Open <Link to="/affiliate" className="text-[#00FF41] hover:underline">Affiliate</Link> in the sidebar.
                    Fill the form (display name, payout method, traffic source). Applications are reviewed manually within
                    1–2 business days.
                </Step>
                <Step n="2" title="Share your code" testid="guide-aff-step-2">
                    Once approved, you get a unique 6-character code and a trackable link
                    (<code className="font-mono text-[10px] text-[#00FF41]">https://stoic.app/api/r/YOURCODE</code>). Drop it in
                    blog reviews, YouTube descriptions, Discord pinned messages — anywhere your audience lives.
                </Step>
                <Step n="3" title="Track + get paid" testid="guide-aff-step-3">
                    Live click and conversion counters update in your dashboard. Once your unpaid balance hits <strong>$50</strong>,
                    click <strong className="text-[#00FF41]">REQUEST PAYOUT</strong>. Payouts are processed around the 15th of
                    each month.
                </Step>
            </div>

            <div className="font-display font-bold text-base mt-6 mb-2">The public landing page</div>
            <P>
                Share <Link to="/affiliates" className="text-[#00FF41] hover:underline">/affiliates</Link> directly with cold
                traffic — it&apos;s a public marketing page that explains the program, runs an interactive earnings calculator,
                and routes visitors to <strong className="text-white">create their account</strong> before applying.
                Perfect for your bio link, your X/Twitter pinned post, or affiliate-recruitment outreach.
            </P>

            <Callout kind="warn">
                <strong>The agreement matters.</strong> Self-referrals, brand-bidding on &quot;STOIC&quot; keywords, and
                guaranteed-profit claims will get you banned and forfeit your unpaid balance. Section 5 of the agreement
                covers the FTC / FCA / SEC essentials — read it before publishing anything.
            </Callout>

            <div className="mt-5 flex flex-wrap gap-2">
                <CTAButton to="/affiliate" testid="guide-cta-affiliate-dashboard">AFFILIATE DASHBOARD</CTAButton>
                <Link to="/affiliates" data-testid="guide-cta-affiliate-public"
                    className="inline-flex items-center gap-2 border border-[#FFD700]/40 hover:bg-[#FFD700]/10 text-[#FFD700] font-medium px-4 py-2 text-xs tracking-widest transition-colors">
                    VIEW PUBLIC LANDING PAGE <ArrowRight className="w-3.5 h-3.5" />
                </Link>
            </div>
        </section>
    );
}

function QuickLinks() {
    const links = [
        { to: "/", label: "Dashboard" },
        { to: "/signals", label: "AI Signals" },
        { to: "/bot", label: "Bot Config" },
        { to: "/accounts", label: "MT5 Accounts" },
        { to: "/trades", label: "Trades" },
        { to: "/analytics", label: "Analytics" },
        { to: "/affiliate", label: "Affiliate" },
        { to: "/billing", label: "Billing" },
        { to: "/settings", label: "Settings" },
        { to: "/faq", label: "FAQ" },
    ];
    return (
        <section>
            <H2 id="faq" icon={Zap}>14. Quick links</H2>
            <P>Jump straight to any feature page:</P>
            <div className="flex flex-wrap gap-2 mt-3">
                {links.map(l => (
                    <Link key={l.to} to={l.to}
                        data-testid={`guide-link-${l.to.replace(/\//g, "") || "home"}`}
                        className="font-mono text-[10px] tracking-widest px-3 py-2 border border-[#1F1F1F] hover:border-[#00FF41]/40 hover:text-[#00FF41] text-[#A1A1AA] transition-colors">
                        {l.label.toUpperCase()} →
                    </Link>
                ))}
            </div>
            <Callout kind="info">
                Still stuck? Open <Link to="/faq" className="text-[#00FF41] hover:underline">FAQ</Link> for the 40 most common
                questions, or use the <Link to="/commander" className="text-[#00FF41] hover:underline">Risk Commander</Link> chat for ad-hoc questions about market state, the bot&apos;s reasoning, or your portfolio.
            </Callout>
        </section>
    );
}
