import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    BookOpen, Sparkles, Brain, Target, CheckCircle2, ArrowRight, ShieldCheck,
    Layers, Rocket, Settings as SettingsIcon, Zap, Clock, TrendingUp, AlertTriangle,
    Plane, Power, Repeat, Eye, Cpu, Search, Send, Bookmark, DollarSign, Server, Bitcoin,
    Stethoscope, Activity, Wand2, BrainCircuit,
} from "lucide-react";

// ─── Table-of-contents ──────────────────────────────────────────────────────
const SECTIONS = [
    { id: "intro",        title: "1. What is STOIC?",                  icon: BookOpen },
    { id: "edge",         title: "2. Why STOIC is different",          icon: Sparkles },
    { id: "engine",       title: "3. The Dual-AI engine",              icon: Brain },
    { id: "agents",       title: "4. The Multi-Agent Architecture",    icon: Cpu },
    { id: "cascade",      title: "5. The 10-Layer Veto Cascade",       icon: Layers },
    { id: "explain",      title: "6. Explainable AI Trading",          icon: Sparkles },
    { id: "multi-bot",    title: "7. Multi-Bot · Multi-Account",       icon: Layers },
    { id: "risk",         title: "8. Risk & Capital Preservation",     icon: ShieldCheck },
    { id: "setup",        title: "9. Setup — sign-up to autopilot",    icon: Rocket },
    { id: "autopilot",    title: "10. Autopilot mode explained",       icon: Plane },
    { id: "shadow",       title: "11. Paper Shadow Mode + Report",     icon: Eye },
    { id: "adaptive",     title: "12. Adaptive Intelligence (NEW)",    icon: Sparkles },
    { id: "optimizer",    title: "13. AI Strategy Optimizer (NEW)",    icon: BrainCircuit },
    { id: "doctor",       title: "14. Bot Doctor — self-diagnosis",    icon: Stethoscope },
    { id: "auto-broker",  title: "15. Auto Broker Detection (NEW)",    icon: Wand2 },
    { id: "daily",        title: "16. Daily 5-min routine",            icon: Clock },
    { id: "advanced",     title: "17. Advanced — tuning & auditing",   icon: SettingsIcon },
    { id: "presets",      title: "18. Strategy Presets",               icon: Bookmark },
    { id: "crypto",       title: "19. Crypto · Binance Spot",          icon: Bitcoin },
    { id: "vps",          title: "20. VPS — 24/7 uptime",              icon: Server },
    { id: "going-live",   title: "21. Going live (paper → real)",      icon: TrendingUp },
    { id: "affiliate",    title: "22. Earn 20% recurring (affiliate)", icon: DollarSign },
    { id: "faq",          title: "23. Quick links",                    icon: Zap },
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
                    <ExplainSection />
                    <MultiBotSection />
                    <RiskSection />
                    <SetupSteps />
                    <Autopilot />
                    <ShadowSection />
                    <AdaptiveSection />
                    <OptimizerSection />
                    <BotDoctorSection />
                    <AutoBrokerSection />
                    <DailyFlow />
                    <Advanced />
                    <PresetsSection />
                    <CryptoSection />
                    <VpsSection />
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
    const palettes = {
        info:    { bd: "border-[#1F1F1F]",     bg: "bg-[#0A0A0A]",     fg: "text-[#A1A1AA]" },
        good:    { bd: "border-[#00FF41]/30",  bg: "bg-[#00FF41]/10",  fg: "text-[#00FF41]" },
        success: { bd: "border-[#00FF41]/30",  bg: "bg-[#00FF41]/10",  fg: "text-[#00FF41]" },
        warn:    { bd: "border-[#FFB000]/30",  bg: "bg-[#FFB000]/10",  fg: "text-[#FFB000]" },
        warning: { bd: "border-[#FFB000]/30",  bg: "bg-[#FFB000]/10",  fg: "text-[#FFB000]" },
    };
    const palette = palettes[kind] || palettes.info;
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
            <H2 id="risk" icon={ShieldCheck}>8. Risk & Capital Preservation</H2>
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
            <H2 id="setup" icon={Rocket}>9. Setup — sign-up to autopilot</H2>
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
            <H2 id="autopilot" icon={Plane}>10. Autopilot mode explained</H2>
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

function AdaptiveSection() {
    return (
        <section id="adaptive">
            <H2 id="adaptive" icon={Sparkles}>12. Adaptive Intelligence — the bot adjusts itself</H2>
            <P>
                STOIC&apos;s three adaptive layers make trades respond to live market conditions without
                you touching a slider. All three are opt-in per account from the <strong>Win-Rate Adaptive Mode</strong> card
                on the Dashboard. Default for every account is OFF (classic Plan A behaviour) — flip them
                on when you want the bot to start adjusting itself.
            </P>

            <div className="space-y-4 mt-4">
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
                    <div className="font-display text-base mb-1.5 flex items-center gap-2">
                        <Target className="w-4 h-4 text-[#10F2C5]" /> 1. Profit-Taking Mode <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-auto">iter-74</span>
                    </div>
                    <P>
                        Three closing styles, one click each:
                    </P>
                    <ul className="text-sm text-[#A1A1AA] mt-2 space-y-1.5">
                        <li>• <strong className="text-white">EXPECTED VALUE</strong> (default) — ATR-driven TPs, existing partial-close / trail logic. The classic Plan A.</li>
                        <li>• <strong className="text-[#10F2C5]">WIN RATE</strong> — tight partials (0.5R / 70%), tight trail (0.5R / 0.25R), <strong>Smart Cap</strong> hard ceiling. Maximises green-trade count.</li>
                        <li>• <strong className="text-[#FFD700]">TREND FOLLOW</strong> — wide partials (1.5R / 30%), wide trail (2R / 1R). Lets runners stretch unchecked.</li>
                    </ul>
                </div>

                <div className="border border-[#10F2C5]/30 bg-[#10F2C5]/5 p-4">
                    <div className="font-display text-base mb-1.5 flex items-center gap-2">
                        <Sparkles className="w-4 h-4 text-[#10F2C5]" /> 2. Smart Cap (regime-aware TP cap) <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-auto">iter-77</span>
                    </div>
                    <P>
                        The win-rate mode&apos;s most important upgrade. The 100-pip TP ceiling no longer fires
                        blindly — it activates only when the market needs the discipline:
                    </P>
                    <ul className="text-sm text-[#A1A1AA] mt-2 space-y-1.5 font-mono text-[11px]">
                        <li>🔓 <strong className="text-[#00FF41]">TRENDING / AGGRESSIVE</strong> → cap REMOVES itself, runners stretch like Plan A</li>
                        <li>🔒 <strong className="text-[#10F2C5]">TRANSITIONAL / RANGING</strong> → 100p cap (disciplined in mixed conditions)</li>
                        <li>🔒🔒 <strong className="text-[#FFB000]">CAUTIOUS_WAIT / DEFENSIVE_SCALP</strong> → 60p cap (strict in chop)</li>
                    </ul>
                    <P>
                        <span className="text-[#A1A1AA]">Live regime + cap state shows on the Adaptive Mode card &mdash; e.g. <span className="text-[#10F2C5] font-mono">SMART CAP · 60p (CAUTIOUS_WAIT)</span> or <span className="text-[#00FF41] font-mono">SMART CAP · OFF (TRENDING → runners free)</span>. Updates every 30 s.</span>
                    </P>
                </div>

                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
                    <div className="font-display text-base mb-1.5 flex items-center gap-2">
                        <Activity className="w-4 h-4 text-[#FFD700]" /> 3. Rolling Adaptive Risk <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-auto">iter-74</span>
                    </div>
                    <P>
                        Auto-scales risk-per-trade based on your last 20 closed trades&apos; win rate:
                    </P>
                    <ul className="text-sm text-[#A1A1AA] mt-2 space-y-1 font-mono text-[11px]">
                        <li>• Win rate ≥ 70% → 1.30× (press the edge)</li>
                        <li>• 60–70% → 1.15×</li>
                        <li>• 50–60% → 1.00× (neutral)</li>
                        <li>• 40–50% → 0.70×</li>
                        <li>• &lt; 40% → 0.50× (capital protect)</li>
                    </ul>
                    <P>Needs ≥ 5 closed trades to activate; defaults to 1.0× otherwise. Live multiplier shown on the Adaptive card.</P>
                </div>

                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
                    <div className="font-display text-base mb-1.5 flex items-center gap-2">
                        <Layers className="w-4 h-4 text-[#FFD700]" /> 4. Regime-Aware Auto-Preset <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-auto">iter-74</span>
                    </div>
                    <P>
                        Bot auto-swaps Strategy Preset each tick based on live regime — no manual switching:
                    </P>
                    <ul className="text-sm text-[#A1A1AA] mt-2 space-y-1 font-mono text-[11px]">
                        <li>• AGGRESSIVE / TRENDING → <strong className="text-white">trend_rider</strong></li>
                        <li>• DEFENSIVE_SCALP → <strong className="text-white">fast_scalp</strong></li>
                        <li>• CAUTIOUS_WAIT / TRANSITIONAL → <strong className="text-white">scalper</strong></li>
                        <li>• RANGING → <strong className="text-white">mean_reversion</strong></li>
                    </ul>
                </div>
            </div>
            <Callout kind="success">
                <strong>Recommended for live accounts:</strong> Smart Cap WIN-RATE + Adaptive Risk + Auto-Preset all ON.
                Keep an Adaptive vs Plan A A/B running on at least one control account so you have evidence either way.
            </Callout>
            <CTAButton to="/" testid="adaptive-cta">Configure on Dashboard <ArrowRight className="w-4 h-4" /></CTAButton>
        </section>
    );
}

function OptimizerSection() {
    return (
        <section id="optimizer">
            <H2 id="optimizer" icon={BrainCircuit}>13. AI Strategy Optimizer — reviews its own trades</H2>
            <P>
                Every account gets its own <strong className="text-white">AI performance coach</strong>. The optimizer
                reads the last <strong className="text-white">24 or 48 hours</strong> of that account&apos;s{" "}
                <strong className="text-[#10F2C5]">bot-executed</strong> closed trades (trades you open manually on the
                broker terminal are excluded — you&apos;ll see an amber <span className="font-mono text-[10px] text-[#FFB000]">N MANUAL TRADES EXCLUDED</span> badge),
                digs through win rate, profit factor, and losing patterns by session / symbol / direction / exit reason,
                and proposes concrete tuning changes to raise the win rate.
            </P>
            <div className="space-y-3 mt-4">
                <div className="border border-[#10F2C5]/30 bg-[#10F2C5]/5 p-4 space-y-2">
                    <div className="font-mono text-[10px] text-[#10F2C5] tracking-widest">SUGGEST-ONLY, ALWAYS</div>
                    <P>
                        The optimizer <strong className="text-white">never touches your config on its own</strong>. Each
                        recommendation is a card with the AI&apos;s reasoning + expected impact, and you choose{" "}
                        <strong className="text-[#10F2C5]">APPLY</strong> or <strong>DISMISS</strong> per card. Three
                        recommendation types:
                    </P>
                    <ul className="text-sm text-[#A1A1AA] space-y-1.5">
                        <li>• <strong className="text-[#10F2C5]">CONFIG CHANGE</strong> — a single setting, shown as <span className="font-mono text-xs">from → to</span> (e.g. SL COOLDOWN: OFF → ON). Values are whitelist-validated and clamped server-side.</li>
                        <li>• <strong className="text-[#FFD700]">SWITCH PRESET</strong> — move the account to a different Strategy Preset when its style doesn&apos;t fit current conditions.</li>
                        <li>• <strong className="text-[#FF3B30]">PAUSE BOT</strong> — only suggested when an account is consistently losing (heavy drawdown / sub-35% win rate on a real sample).</li>
                    </ul>
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">STRICTLY PER-ACCOUNT</div>
                    <P>
                        Each account is analyzed <strong className="text-white">separately</strong> and applying a suggestion
                        changes <strong className="text-white">only that account</strong>. If the account still inherits the
                        default profile, STOIC first clones the profile into a per-account override — so a tweak for your
                        scalping account can never leak into your swing account.
                    </P>
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">WHEN IT RUNS</div>
                    <ul className="text-sm text-[#A1A1AA] space-y-1.5">
                        <li>• <strong className="text-white">On-demand</strong> — <span className="font-mono text-xs text-[#10F2C5]">ANALYZE 24H / 48H</span> buttons in Bot Config (select an account first).</li>
                        <li>• <strong className="text-white">Scheduled</strong> — an automatic review every 24h per active account (needs ≥ 3 bot-executed closed trades in the window).</li>
                        <li>• <strong className="text-white">Dashboard card</strong> — the AI STRATEGY OPTIMIZER card lists each account&apos;s latest verdict (HEALTHY / NEEDS TUNING / UNDERPERFORMING / CRITICAL) and pending suggestions; click a row to jump straight to that account&apos;s review.</li>
                    </ul>
                </div>
            </div>
            <Callout kind="warn">
                <strong>CRITICAL verdict → Telegram alert.</strong> When a scheduled review flags an account as CRITICAL,
                you get a Telegram message with the account, win rate, P&L and pending suggestion count. Toggle it under
                Notifications → &quot;AI Optimizer: Critical Verdict&quot;.
            </Callout>
            <Callout kind="info">
                Powered by <strong>Claude Fable 5</strong> (Anthropic&apos;s newest model) with automatic fallback. Reports
                stay in history — an already-applied or dismissed card keeps its state so you always know what you acted on.
            </Callout>
            <CTAButton to="/bot-config?optimizer=1" testid="optimizer-guide-cta">Open the Optimizer in Bot Config</CTAButton>
        </section>
    );
}

function BotDoctorSection() {
    return (
        <section id="doctor">
            <H2 id="doctor" icon={Stethoscope}>14. Bot Doctor — self-diagnosis</H2>
            <P>
                STOIC analyses its own last hour of telemetry every 5 minutes (failed trades, blocked
                accounts, regime context, vetos, signal patterns, win rate) and produces a structured
                health diagnosis powered by <strong>Claude Sonnet 4.5</strong>. The result lives at the top
                of your Dashboard as the <strong>Bot Doctor</strong> tile.
            </P>
            <div className="space-y-3 mt-4">
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">WHAT YOU GET</div>
                    <ul className="text-sm text-[#A1A1AA] space-y-1.5">
                        <li>• <strong className="text-white">Status badge</strong> — healthy / watch / degraded / critical (color-coded)</li>
                        <li>• <strong className="text-white">Headline</strong> — one-line summary of the situation</li>
                        <li>• <strong className="text-white">Findings</strong> — 2–5 concrete observations (named symbols, retcodes, accounts)</li>
                        <li>• <strong className="text-white">Root-cause hypothesis</strong> — best guess with confidence %</li>
                        <li>• <strong className="text-white">Ranked actions</strong> — each tagged with effort (low/medium/high) and destructive flag</li>
                    </ul>
                </div>
                <Callout kind="info">
                    <strong>LITE mode</strong> — Bot Doctor diagnoses and recommends, but never auto-applies fixes.
                    Every action requires your manual confirmation. Click <strong>CONSULT</strong> on the tile to force a fresh diagnosis at any time (bypasses the 5 min cache).
                </Callout>
                <P>
                    <strong>When the LLM is unavailable</strong>, a deterministic rule-based fallback runs automatically — Doctor is
                    never silent. You&apos;ll see <span className="font-mono text-[10px] text-[#FFB000]">LLM FALLBACK</span> in the header when this kicks in.
                </P>
            </div>
        </section>
    );
}

function AutoBrokerSection() {
    return (
        <section id="auto-broker">
            <H2 id="auto-broker" icon={Wand2}>15. Auto Broker Detection</H2>
            <P>
                Every broker renames common symbols differently — <code className="font-mono text-[#10F2C5]">XAUUSD</code> becomes
                <code className="font-mono text-[#10F2C5]"> XAUUSD.fx</code> on Tauro, <code className="font-mono text-[#10F2C5]">XAUUSD.e</code> on OnEquity,
                <code className="font-mono text-[#10F2C5]"> XAUUSD.raw</code> on IC Markets. STOIC handles this automatically
                via a two-tier system so you almost never have to dig through MT5 Market Watch again.
            </P>
            <div className="space-y-3 mt-4">
                <Step n="1" title="EA v1.34 scans MarketWatch every hour" testid="auto-broker-step-1">
                    The EA enumerates your broker&apos;s instrument list and reports symbols matching 23 known bases
                    (XAU, BTC, EUR, GBP, USD pairs, indices, oil...) on each heartbeat.
                </Step>
                <Step n="2" title="Backend infers the broker&apos;s suffix convention" testid="auto-broker-step-2">
                    The <code className="font-mono">broker_symbol_detector</code> votes across distinct base names — if it sees
                    <code className="font-mono"> XAUUSD.fx</code>, <code className="font-mono">EURUSD.fx</code>, <code className="font-mono">BTCUSD.fx</code> all together, it concludes the broker uses <code className="font-mono">.fx</code>.
                </Step>
                <Step n="3" title="Trades route with the correct symbol" testid="auto-broker-step-3">
                    Precedence: <strong className="text-[#FFD700]">your manual override</strong> &gt; <strong className="text-[#10F2C5]">auto-detected suffix</strong> &gt; bare name.
                    You can always override from the Accounts page if auto-detect picks wrong.
                </Step>
            </div>
            <Callout kind="info">
                <strong>UI surface:</strong> On the Accounts page each MT5 account shows its current suffix with a badge —
                <span className="font-mono text-[10px] text-[#FFD700] mx-1">MANUAL</span> /
                <span className="font-mono text-[10px] text-[#10F2C5] mx-1">AUTO-DETECTED</span> /
                <span className="font-mono text-[10px] text-[#52525B] mx-1">BARE</span> — plus how many symbols the detector matched and the confidence %.
            </Callout>
            <Callout kind="success">
                <strong>Tested across 5 brokers:</strong> Tauro Markets (<code>.fx</code>), OnEquity (<code>.e</code>),
                IC Markets (<code>.raw</code>), FBS / RoboForex (<code>.std</code> or bare), FXTM (<code>pro</code>).
                The detector also handles 33 case-and-prefix variants automatically inside the EA.
            </Callout>
            <CTAButton to="/accounts" testid="auto-broker-cta">View Accounts &amp; Suffixes <ArrowRight className="w-4 h-4" /></CTAButton>
        </section>
    );
}

function DailyFlow() {
    const checks = [
        ["Open Dashboard", "Glance at the BOT STATUS row — LIVE indicator, open trade count, today's P&L, Veto Count card."],
        ["Check connection",   "Accounts page → all MT5 accounts should show ● CONNECTED. If any is stale, follow the FAQ disconnection fix."],
        ["Review HOLDs",       "AI Signals page → click ALL HOLDS to see what was skipped + why. Look for repeated BLOCKED vetoes — they tell you what's killing your edge."],
        ["Audit STRONG / MARGINAL", "If any actionable signal is MARGINAL, manually decide skip vs half-size. STRONG signals you can trust to autonomous execution."],
        ["Skim Trades log",    "Trades page → confirm any opened/closed trades match your expectations. Use the new date-range picker and WINNING / LOST / OPEN filters to slice the log fast. Telegram alerts mirror this."],
        ["Check AI Optimizer suggestions", "Dashboard → AI STRATEGY OPTIMIZER card. If an account shows pending suggestions or a NEEDS TUNING / CRITICAL verdict, click through and Apply what you trust (see section 13)."],
        ["Read Risk Commander","Optional: chat with the Commander for ad-hoc questions like \"why did the bot pass on gold today?\""],
    ];
    return (
        <section>
            <H2 id="daily" icon={Clock}>16. Daily 5-min routine</H2>
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
            <H2 id="advanced" icon={SettingsIcon}>17. Advanced — tuning & auditing</H2>
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
            <H2 id="going-live" icon={TrendingUp}>21. Going live (paper → real)</H2>
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

function VpsSection() {
    return (
        <section>
            <H2 id="vps" icon={Server}>20. VPS — 24/7 uptime (eliminate disconnects)</H2>
            <P>
                Autopilot only works while your <strong className="text-white">MT5 terminal is running and online</strong>.
                If your PC sleeps, closes its lid, reboots, or loses internet — the bot loses contact with your broker,
                pending trades stall, and stop-losses can drift. The industry-standard fix is to move MT5 onto a{" "}
                <strong className="text-[#FFD700]">virtual private server (VPS)</strong> that&apos;s online 24/7.
            </P>

            <Callout kind="info">
                <strong>RoboForex users:</strong> a Free VPS is available when your account equity is ≥ $300{" "}
                <em>and</em> you trade at least 3 standard lots per month.
                Activate it from your{" "}
                <a href="https://roboforex.com/clients/services/forex-vps/" target="_blank" rel="noreferrer"
                    className="text-[#00FF41] hover:underline">RoboForex VPS service page</a>{" "}
                (log in → Member Area → Services → Your VPS 2.0 server → Submit Application).
                If you don&apos;t meet the volume requirement, the VPS is available for $5/month from the same page.
            </Callout>

            <div className="font-display font-bold text-base mt-6 mb-2">Step-by-step · RoboForex Free VPS</div>
            <div className="space-y-3">
                <Step n="1" title="Eligibility check" testid="guide-vps-step-1">
                    Log in to your{" "}
                    <a href="https://my.roboforex.com/en/login/" target="_blank" rel="noreferrer"
                        className="text-[#00FF41] hover:underline">RoboForex Member Area</a>.
                    Free VPS requires ≥ $300 equity <em>and</em> ≥ 3 standard lots traded in the current month
                    (CFDs excluding US-stock CFDs). Check eligibility under <em>Services → Your VPS 2.0 server</em>.
                    Below threshold? The same panel offers a $5/month paid option, or use any third-party VPS.
                </Step>
                <Step n="2" title="Submit the application" testid="guide-vps-step-2">
                    From <em>Services → Your VPS 2.0 server → Submit Application</em>, RoboForex auto-picks
                    the data centre closest to the execution server (London / New York / Singapore).
                    Approval is manual but usually arrives within 15–30 minutes by email — your IP address,
                    username, and password come in that message.
                </Step>
                <Step n="3" title="Connect via RDP" testid="guide-vps-step-3">
                    On Windows: open <em>Remote Desktop Connection</em> → enter the IP from the email →
                    sign in with the provided creds.<br />
                    On Mac: install <a href="https://apps.apple.com/app/microsoft-remote-desktop/id1295203466" target="_blank" rel="noreferrer" className="text-[#00FF41] hover:underline">Microsoft Remote Desktop</a> from the App Store → add the VPS as a new PC.<br />
                    On mobile: <em>RD Client</em> by Microsoft works the same way.
                </Step>
                <Step n="4" title="Install MetaTrader 5 on the VPS" testid="guide-vps-step-4">
                    Inside the VPS, open Edge → go to{" "}
                    <a href="https://my.roboforex.com/en/clients/downloads/" target="_blank" rel="noreferrer"
                        className="text-[#00FF41] hover:underline">RoboForex Downloads → MetaTrader 5</a>{" "}
                    → install. Log in with your live account number + server (e.g.{" "}
                    <code className="font-mono text-[10px] text-[#00FF41]">RoboForex-Pro</code> or{" "}
                    <code className="font-mono text-[10px] text-[#00FF41]">RoboForex-ECN</code>).
                </Step>
                <Step n="5" title="Copy the STOIC EA to the VPS" testid="guide-vps-step-5">
                    Easiest path: in MT5 click <em>File → Open Data Folder → MQL5/Experts</em>. Then{" "}
                    <strong className="text-[#FFD700]">on the VPS</strong>, open the same MT5 → Data Folder →
                    paste <code className="font-mono text-[10px] text-[#00FF41]">EmergentTradingBridge.ex5</code>{" "}
                    into <em>MQL5/Experts</em>. Restart MT5. The EA appears in the Navigator panel.
                </Step>
                <Step n="6" title="Attach the EA + paste your bridge token" testid="guide-vps-step-6">
                    Drag <code className="font-mono text-[10px] text-[#00FF41]">EmergentTradingBridge</code> from
                    the Navigator onto any chart (XAUUSD M5 is fine — the EA listens for instructions, not the chart timeframe).
                    In the EA inputs, paste your <strong className="text-[#FFD700]">Bridge Token</strong> from{" "}
                    <Link to="/accounts" className="text-[#00FF41] hover:underline">MT5 Accounts → COPY</Link>.
                    Tick <strong>Allow Algo Trading</strong>. Click OK.
                </Step>
                <Step n="7" title="Enable Algo Trading + autostart on reboot" testid="guide-vps-step-7">
                    Top toolbar of MT5: <strong>Algo Trading</strong> button must be GREEN.<br />
                    Also: <em>Tools → Options → Expert Advisors</em> — tick:
                    <ul className="mt-2 ml-4 space-y-1 list-disc text-xs text-[#A1A1AA]">
                        <li>Allow algorithmic trading</li>
                        <li>Allow WebRequests for: <code className="font-mono text-[10px] text-[#00FF41]">https://stoic.app</code> (and the preview URL if testing)</li>
                        <li>Disable algorithmic trading when the account has been changed — UNTICK</li>
                    </ul>
                    Finally: <em>View → Strategy Tester → Settings</em> → make sure MT5 is set to auto-launch
                    when the VPS boots (Windows Run dialog → <code className="font-mono text-[10px] text-[#00FF41]">shell:startup</code> → drop an MT5 shortcut).
                </Step>
                <Step n="8" title="Verify uptime in STOIC" testid="guide-vps-step-8">
                    Disconnect from the VPS (close RDP). Wait 60 seconds. Open STOIC{" "}
                    <Link to="/" className="text-[#00FF41] hover:underline">Dashboard</Link> — the{" "}
                    <strong>Integrity widget</strong> at the top should stay green (<strong className="text-[#00FF41]">● IN SYNC</strong>)
                    with last-sync &lt; 10 seconds. That confirms your VPS is keeping the heartbeat alive without your laptop.
                </Step>
            </div>

            <div className="font-display font-bold text-base mt-7 mb-2">Alternative · paid third-party VPS</div>
            <div className="grid sm:grid-cols-2 gap-2 mt-2">
                <a href="https://forexvps.net" target="_blank" rel="noreferrer"
                    className="border border-[#1F1F1F] hover:border-[#FFD700]/40 bg-[#0A0A0A] px-3 py-2 transition-colors">
                    <div className="font-display font-bold text-sm">ForexVPS.net</div>
                    <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">~$10/mo · low-latency to MT5 brokers · 1-click MT5</div>
                </a>
                <a href="https://accuwebhosting.com/forex-vps-hosting" target="_blank" rel="noreferrer"
                    className="border border-[#1F1F1F] hover:border-[#FFD700]/40 bg-[#0A0A0A] px-3 py-2 transition-colors">
                    <div className="font-display font-bold text-sm">AccuWeb Forex VPS</div>
                    <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">~$8/mo · Windows Server 2022 · NY4 datacentre</div>
                </a>
                <a href="https://cheapforexvps.com" target="_blank" rel="noreferrer"
                    className="border border-[#1F1F1F] hover:border-[#FFD700]/40 bg-[#0A0A0A] px-3 py-2 transition-colors">
                    <div className="font-display font-bold text-sm">CheapForexVPS</div>
                    <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">$5/mo · budget pick · LD4/NY4 datacentres</div>
                </a>
                <a href="https://aws.amazon.com/lightsail/" target="_blank" rel="noreferrer"
                    className="border border-[#1F1F1F] hover:border-[#FFD700]/40 bg-[#0A0A0A] px-3 py-2 transition-colors">
                    <div className="font-display font-bold text-sm">AWS Lightsail Windows</div>
                    <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">$8/mo · full control · pick region near broker</div>
                </a>
            </div>

            <Callout kind="warn">
                <strong>Don&apos;t skip Step 7&apos;s WebRequest allowlist.</strong> MT5 will silently
                block the EA&apos;s HTTPS calls unless you whitelist the STOIC URL. Symptom: EA runs but
                heartbeat never reaches STOIC. The MT5 <em>Experts</em> tab will show{" "}
                <code className="font-mono text-[10px] text-[#FFB000]">WebRequest forbidden</code>.
            </Callout>

            <div className="mt-5">
                <CTAButton to="/accounts" testid="guide-cta-vps">OPEN MT5 ACCOUNTS</CTAButton>
            </div>
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
            <H2 id="presets" icon={Bookmark}>18. Strategy Presets</H2>
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
            <H2 id="affiliate" icon={DollarSign}>22. Earn 20% recurring (affiliate program)</H2>
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
        { to: "/crypto", label: "Crypto · Binance" },
        { to: "/trades", label: "Trades" },
        { to: "/shadow-performance", label: "Shadow Report" },
        { to: "/research", label: "Research Agent" },
        { to: "/portfolio", label: "Portfolio Risk" },
        { to: "/execution", label: "Execution Intel" },
        { to: "/analytics", label: "Analytics" },
        { to: "/affiliate", label: "Affiliate" },
        { to: "/billing", label: "Billing" },
        { to: "/settings", label: "Settings" },
        { to: "/faq", label: "FAQ" },
    ];
    return (
        <section>
            <H2 id="faq" icon={Zap}>23. Quick links</H2>
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


// ─── New sections (iter-34 → iter-40) ──────────────────────────────────────

function ExplainSection() {
    return (
        <section>
            <H2 id="explain" icon={Sparkles}>6. Explainable AI Trading</H2>
            <P>
                Every trade STOIC opens carries a full reasoning trail — no black-box decisions.
                Click <strong className="text-[#00FF41]">EXPLAIN</strong> on any row in the
                <Link to="/trades" className="text-[#00FF41] hover:underline mx-1">Trades</Link>
                page to open a 4-section breakdown:
            </P>
            <ol className="text-sm text-[#A1A1AA] space-y-2 mt-3 ml-4 list-decimal">
                <li><strong className="text-white">WHY DID I ENTER?</strong> — strategy summary + technical / macro / news bias scores that nudged the AI toward BUY or SELL.</li>
                <li><strong className="text-white">WHY THIS SIZE?</strong> — original lot, vol-parity scaling, Kelly fraction, blended sizing multiplier, 30-day win-rate, and the human-readable reason.</li>
                <li><strong className="text-white">WHAT FACTORS MATTERED?</strong> — top-3 ranked contributors tagged <span className="text-[#FFD700]">HIGH</span> or <span className="text-[#0099FF]">MEDIUM</span> impact (regime, session, macro alignment, etc.).</li>
                <li><strong className="text-white">WHAT RISKS EXIST?</strong> — exact SL/TP prices, pip distance, max-loss in USD, % of equity at risk, and macro-gate state.</li>
            </ol>
            <Callout kind="info">
                Snapshots are frozen at entry, so the explanation never drifts even if the bot
                changes config later. Older trades fall back to a live-composition view.
            </Callout>
            <Callout kind="success">
                Combine with the <strong>opt-in auto-accept</strong> on the Research Agent —
                proposals with ≥ 10% backtest improvement can auto-apply to your bot. The explainer
                shows exactly why the new strategy thinks it&apos;ll do better.
            </Callout>
            <CTAButton to="/trades" testid="cta-trades-explain">Open Trades →</CTAButton>
        </section>
    );
}

function MultiBotSection() {
    return (
        <section>
            <H2 id="multi-bot" icon={Layers}>7. Multi-Bot · Multi-Account</H2>
            <P>
                You can attach as many MT5 accounts and Binance Spot accounts as you want.
                Each one gets its own independent bot config — different risk levels,
                different symbols, different strategy styles — all running in parallel.
            </P>
            <Step n="1" title="Pick the scope per page" testid="multi-bot-scope">
                On <Link to="/bot" className="text-[#00FF41] hover:underline">Bot Config</Link>,
                a scope selector at the top lets you edit the <strong>default profile</strong> (applies to every account that has no override) or a specific account. The same pattern shows up on Trades, Safety Blocks, Portfolio Risk, and Execution Intel.
            </Step>
            <Step n="2" title="Apply strategies smartly" testid="multi-bot-apply">
                When you click <strong>DEPLOY</strong> on the Strategies page or
                <strong> APPLY TO BOT</strong> on a Research proposal, you&apos;ll see a
                target dropdown:
                <ul className="text-xs text-[#A1A1AA] mt-2 ml-4 list-disc space-y-1">
                    <li><strong className="text-white">Matching-symbol bots (default)</strong> — only updates bots whose symbols overlap the proposal. Safe & smart.</li>
                    <li><strong className="text-white">All bots</strong> — broadcast to everything.</li>
                    <li><strong className="text-white">Only · &lt;account name&gt;</strong> — surgical single-account update.</li>
                </ul>
            </Step>
            <Step n="3" title="Audit trail on every change" testid="multi-bot-audit">
                Every config update stamps the bot doc with <code className="text-[#FFD700]">last_change_source</code>
                (research / nl_strategy / risk_commander / ui_manual),
                <code className="text-[#FFD700] mx-1">last_change_target_mode</code>,
                and <code className="text-[#FFD700]">last_change_applied_at</code>. You can
                always answer &quot;why is this bot set up like this?&quot;.
            </Step>
            <Step n="4" title="Fleet at a glance — Bot Pulse" testid="multi-bot-pulse">
                The <strong>Bot Pulse</strong> panel on the Dashboard shows every account&apos;s live bot state
                with a <strong className="text-white">strategy chip</strong> (which preset each account runs).
                Click a chip to deep-link straight into that account&apos;s preset in Bot Config. Running more
                than 10 accounts? The panel automatically collapses into a compact dropdown to save space.
            </Step>
            <Callout kind="info">
                <strong>Telegram quick controls</strong> (<code>/run</code>, <code>/stop</code>, <code>/panic</code>)
                broadcast to ALL bots by default. Toast / Telegram reply tells you
                exactly how many bots were affected.
            </Callout>
        </section>
    );
}

function ShadowSection() {
    return (
        <section>
            <H2 id="shadow" icon={Eye}>11. Paper Shadow Mode + Performance Report</H2>
            <P>
                The fastest, safest way to validate your bot config before risking $1.
                Shadow Mode runs the <em>full</em> AI pipeline — every signal, every veto,
                every sizing decision — but skips execution. Signals are logged with
                <code className="text-[#06B6D4] mx-1">origin=&quot;shadow&quot;</code>.
                After 1-2 weeks you have a real personal backtest of YOUR config against the live market.
            </P>
            <Step n="1" title="Enable Shadow Mode" testid="shadow-enable">
                Open <Link to="/bot" className="text-[#00FF41] hover:underline">Bot Config</Link>,
                scroll to the <strong>Paper Shadow Mode</strong> toggle (eye icon). Turn it ON.
                You can leave the bot itself OFF — Shadow Mode runs even when the bot is paused.
            </Step>
            <Step n="2" title="Wait for signals to accumulate" testid="shadow-wait">
                The bot ticks once per minute. Expect ~5-15 shadow signals per day depending on
                volatility and your symbols. HOLD signals don&apos;t count toward the report.
            </Step>
            <Step n="3" title="Open the Shadow Performance Report" testid="shadow-report">
                <Link to="/shadow-performance" className="text-[#00FF41] hover:underline">Shadow Report</Link>
                {" "}aggregates everything into actionable quant metrics:
                <ul className="text-xs text-[#A1A1AA] mt-2 ml-4 list-disc space-y-1">
                    <li><strong className="text-white">Win rate, expectancy (R), total R</strong> — the only numbers that matter.</li>
                    <li><strong className="text-white">Sharpe-lite</strong> — mean ÷ stdev of per-signal R, gives a feel for consistency.</li>
                    <li><strong className="text-white">Per-symbol breakdown</strong> — see whether your gold edge is better than your BTC edge.</li>
                    <li><strong className="text-white">Full signal log</strong> with each signal&apos;s outcome (TP / SL / open) and R-multiple.</li>
                </ul>
            </Step>
            <Callout kind="warning">
                Outcomes resolve at <strong>daily-candle granularity</strong>. When both TP and SL
                are hit in the same day, the report conservatively assumes SL filled first
                (bad-case for you). Real intraday execution would resolve faster.
            </Callout>
            <Callout kind="success">
                After 2 weeks of shadow data showing positive expectancy + healthy win-rate, you can
                flip Shadow Mode OFF and turn on the bot for real — with calibrated expectations
                of how it&apos;ll perform.
            </Callout>
            <CTAButton to="/shadow-performance" testid="cta-shadow">Open Shadow Report →</CTAButton>
        </section>
    );
}

function CryptoSection() {
    return (
        <section>
            <H2 id="crypto" icon={Bitcoin}>19. Crypto · Binance Spot</H2>
            <P>
                STOIC trades BTC/USDT on Binance Spot via the CCXT REST API — server-side,
                no EA needed. Runs the same Safety Guardian veto cascade, the same Explainable AI
                snapshot, and the same multi-agent pipeline as MT5.
            </P>
            <Step n="1" title="Get a Binance Testnet API key" testid="crypto-testnet-key">
                Go to <a href="https://testnet.binance.vision/" target="_blank" rel="noopener noreferrer"
                    className="text-[#FFD700] hover:underline">testnet.binance.vision</a>,
                generate a key with <strong>Enable Spot Trading</strong> permission.
                <strong className="text-[#FF3B30]"> Do NOT grant Withdrawals.</strong>
            </Step>
            <Step n="2" title="Link the account" testid="crypto-link">
                Open <Link to="/crypto" className="text-[#00FF41] hover:underline">Crypto · Binance</Link>,
                click <strong>ADD BINANCE ACCOUNT</strong>, paste your key + secret. The bot
                verifies the keys against Binance before saving — if testnet is geo-blocked from your
                location, you&apos;ll see a clear error. Keys are AES-256-GCM encrypted at rest.
            </Step>
            <Step n="3" title="Verify defence-in-depth" testid="crypto-defence">
                A trade only goes live when ALL three of these are true:
                <ul className="text-xs text-[#A1A1AA] mt-2 ml-4 list-disc space-y-1">
                    <li><code className="text-[#FFD700]">account.testnet = false</code></li>
                    <li><code className="text-[#FFD700]">account.live = true</code></li>
                    <li><code className="text-[#FFD700]">BINANCE_LIVE_ENABLED=true</code> in backend env (admin-only kill-switch)</li>
                </ul>
                Any one false → testnet. The dashboard banner shows the master state.
            </Step>
            <Step n="4" title="Tighter crypto risk caps" testid="crypto-cap">
                Crypto trades have a separate per-trade risk cap (default 0.5% of equity, vs
                MT5&apos;s 1%). You can tighten it per-account in
                <Link to="/bot" className="text-[#00FF41] hover:underline mx-1">Bot Config</Link>
                via the <strong>Crypto Risk Cap (per-account override)</strong> field —
                e.g. set 0.1% on a volatile altcoin pair.
            </Step>
            <Callout kind="info">
                INSPECT modal on any Binance account shows live BTC/USDT ticker (bid/ask/last)
                + your full balance breakdown.
            </Callout>
            <CTAButton to="/crypto" testid="cta-crypto">Open Crypto · Binance →</CTAButton>
        </section>
    );
}
