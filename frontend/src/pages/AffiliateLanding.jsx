import { useState, useMemo } from "react";
import { Link } from "react-router-dom";
import {
    DollarSign, Repeat, Clock, ShieldCheck, ArrowRight, Check, Users,
    TrendingUp, Calculator, Sparkles, AlertTriangle, MoonStar, ChevronDown,
} from "lucide-react";
import { AFFILIATE_TERMS } from "@/lib/affiliateTerms";

/**
 * Public affiliate landing page · /affiliates
 *
 * Accessible WITHOUT authentication. Designed to convert cold traffic into
 * affiliate applications. CTA routes to /login (then /affiliate after auth).
 *
 * Sections: hero, stat strip, how-it-works, earnings calculator, why-stoic,
 * terms preview, final CTA, footer disclaimer.
 */

const HERO_STATS = [
    { icon: Repeat,   label: "RECURRING COMMISSION",  value: "20%",      sub: "for the lifetime of each referral" },
    { icon: Clock,    label: "COOKIE WINDOW",          value: "60 DAYS",  sub: "from the first click" },
    { icon: DollarSign, label: "MIN PAYOUT",            value: "$50",      sub: "via Stripe or PayPal" },
    { icon: Users,    label: "PROGRAM LEVEL",          value: "TIER 1",   sub: "(community tier coming)" },
];

const STEPS = [
    { n: "01", title: "Apply", desc: "Sign up for STOIC, then submit the affiliate application from your dashboard. Manual review keeps the program premium." },
    { n: "02", title: "Share",  desc: "Get a unique 6-character referral code + tracked link. Drop it in reviews, videos, Discords, or trading communities." },
    { n: "03", title: "Earn",   desc: "20% of every subscription payment your referrals make — for as long as they stay active. Request payout at $50+." },
];

const VALUE_PROPS = [
    { icon: TrendingUp, title: "High-LTV audience",
      body: "Algorithmic traders subscribe for years, not weeks. Average referred subscriber lasts 14+ months — that's 14+ commission payments per signup." },
    { icon: Sparkles,   title: "Pre-built media kit",
      body: "Brand assets, screenshots, performance graphs, and disclosure boilerplate ready in your affiliate dashboard. No design work required." },
    { icon: ShieldCheck, title: "Transparent attribution",
      body: "Live click and conversion counters in your dashboard. Every commission is logged with timestamp, plan, and source — no black-box accounting." },
];

const FAQ = [
    { q: "When am I paid?",
      a: "Once your unpaid balance reaches $50, click REQUEST PAYOUT from your dashboard. We process all requests on or around the 15th of the following month via Stripe or PayPal." },
    { q: "Can I refer myself?",
      a: "No. Self-referrals, family-member loopholes, and rebate offers void the entire account and all unpaid balance. Section 3 of the agreement." },
    { q: "Is brand-bidding allowed?",
      a: "No. You may not bid on \"STOIC\" or its variants on any ad network. Generic trading-bot keywords are fine — promote on your own properties." },
    { q: "Do I need a license to promote a trading product?",
      a: "Local rules vary. Every published asset must include a clear risk disclaimer and affiliate disclosure. Section 5 of the agreement covers the FTC / FCA / SEC essentials." },
    { q: "What if my referral cancels?",
      a: "Commissions stop when the user's subscription ends. You keep everything earned up to that point — no clawbacks." },
];


function PageNav() {
    return (
        <header className="border-b border-[#1F1F1F] bg-[#050505]/90 backdrop-blur sticky top-0 z-30">
            <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
                <Link to="/" className="flex items-center gap-2" data-testid="brand-link">
                    <MoonStar className="w-5 h-5 text-[#FFD700]" />
                    <span className="font-display font-bold tracking-tighter text-lg">STOIC</span>
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-2">· AFFILIATES</span>
                </Link>
                <div className="flex items-center gap-3">
                    <Link to="/login" data-testid="nav-signin"
                        className="font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-white px-3 py-1.5">
                        SIGN IN
                    </Link>
                    <Link to="/register" data-testid="nav-register"
                        className="font-mono text-[10px] tracking-widest text-black bg-[#FFD700] hover:bg-[#FFE033] px-3 py-1.5 flex items-center gap-1">
                        START FREE <ArrowRight className="w-3 h-3" />
                    </Link>
                </div>
            </div>
        </header>
    );
}


function HeroSection() {
    return (
        <section className="relative overflow-hidden border-b border-[#1F1F1F]"
            data-testid="hero">
            {/* Decorative grid + gold corner glow */}
            <div className="absolute inset-0 pointer-events-none opacity-[0.04]"
                style={{
                    backgroundImage: "linear-gradient(rgba(255,215,0,1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,215,0,1) 1px, transparent 1px)",
                    backgroundSize: "48px 48px",
                }} />
            <div className="absolute -top-32 -right-32 w-[480px] h-[480px] rounded-full pointer-events-none"
                style={{ background: "radial-gradient(circle, rgba(255,215,0,0.10) 0%, rgba(255,215,0,0) 70%)" }} />

            <div className="relative max-w-6xl mx-auto px-6 py-16 md:py-24">
                <div className="font-mono text-[10px] text-[#FFD700] tracking-widest mb-5 flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-[#FFD700] pulse-dot" />
                    AFFILIATE PROGRAM · OPEN FOR APPLICATIONS
                </div>
                <h1 className="font-display font-bold tracking-tighter text-4xl sm:text-5xl lg:text-6xl leading-[0.95] mb-6 max-w-3xl">
                    Refer one trader.<br />
                    Get paid <span className="text-[#FFD700]">every month</span> they trade.
                </h1>
                <p className="text-base md:text-lg text-[#A1A1AA] max-w-2xl leading-relaxed mb-10">
                    STOIC is an AI trading platform for Gold (XAU) and Bitcoin (BTC) traders.
                    Bring us subscribers and earn <span className="text-white font-semibold">20% recurring commission</span> for
                    the full lifetime of each referred account. No caps. No clawbacks.
                </p>
                <div className="flex flex-wrap items-center gap-3 mb-12">
                    <Link to="/register" data-testid="hero-cta-apply"
                        className="px-6 py-3 text-xs font-mono tracking-widest bg-[#FFD700] hover:bg-[#FFE033] text-black flex items-center gap-2 transition-colors">
                        APPLY TO PROGRAM <ArrowRight className="w-3.5 h-3.5" />
                    </Link>
                    <a href="#how-it-works" data-testid="hero-cta-learn"
                        className="px-6 py-3 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#FFD700]/40 text-white transition-colors">
                        HOW IT WORKS
                    </a>
                </div>

                <div className="grid grid-cols-2 lg:grid-cols-4 gap-px bg-[#1F1F1F] border border-[#1F1F1F]"
                    data-testid="hero-stats">
                    {HERO_STATS.map(s => (
                        <div key={s.label} className="bg-[#0A0A0A] p-5">
                            <s.icon className="w-4 h-4 text-[#FFD700] mb-3" />
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">{s.label}</div>
                            <div className="font-display font-bold text-2xl tracking-tight">{s.value}</div>
                            <div className="font-mono text-[10px] text-[#A1A1AA] mt-1">{s.sub}</div>
                        </div>
                    ))}
                </div>
            </div>
        </section>
    );
}


function HowItWorks() {
    return (
        <section id="how-it-works" className="border-b border-[#1F1F1F]" data-testid="how-it-works">
            <div className="max-w-6xl mx-auto px-6 py-16">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">THE FLOW</div>
                <h2 className="font-display font-bold tracking-tighter text-3xl md:text-4xl mb-12 max-w-xl">
                    Three steps from signup to payout.
                </h2>
                <div className="grid md:grid-cols-3 gap-6">
                    {STEPS.map(s => (
                        <div key={s.n} className="border border-[#1F1F1F] bg-[#0A0A0A] p-6">
                            <div className="font-mono text-3xl font-bold text-[#FFD700]/40 mb-3 tracking-tighter">{s.n}</div>
                            <h3 className="font-display font-bold text-xl tracking-tight mb-2">{s.title}</h3>
                            <p className="text-sm text-[#A1A1AA] leading-relaxed">{s.desc}</p>
                        </div>
                    ))}
                </div>
            </div>
        </section>
    );
}


function EarningsCalculator() {
    const [refs, setRefs] = useState(10);
    const [planUsd, setPlanUsd] = useState(49);
    const COMMISSION = 0.20;
    const projection = useMemo(() => {
        const monthly = refs * planUsd * COMMISSION;
        return {
            monthly,
            year: monthly * 12,
            threeYear: monthly * 36,
        };
    }, [refs, planUsd]);

    return (
        <section className="border-b border-[#1F1F1F]" data-testid="calculator">
            <div className="max-w-6xl mx-auto px-6 py-16">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">THE MATH</div>
                <h2 className="font-display font-bold tracking-tighter text-3xl md:text-4xl mb-2 max-w-xl">
                    Estimate your recurring revenue.
                </h2>
                <p className="text-sm text-[#A1A1AA] mb-10">
                    Adjust the sliders. The model assumes referrals stay subscribed for the period shown — the real average is 14+ months.
                </p>

                <div className="border border-[#1F1F1F] bg-[#0A0A0A] grid md:grid-cols-2 gap-px">
                    {/* Inputs */}
                    <div className="bg-[#0A0A0A] p-6 space-y-6">
                        <div>
                            <div className="flex items-center justify-between mb-3">
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest">ACTIVE REFERRALS</label>
                                <span className="font-display font-bold text-lg text-[#FFD700]" data-testid="calc-refs-value">{refs}</span>
                            </div>
                            <input type="range" min="1" max="200" value={refs}
                                onChange={e => setRefs(Number(e.target.value))}
                                data-testid="calc-refs-slider"
                                className="w-full accent-[#FFD700]" />
                            <div className="flex justify-between font-mono text-[9px] text-[#52525B] mt-1 tracking-widest">
                                <span>1</span><span>50</span><span>100</span><span>200</span>
                            </div>
                        </div>
                        <div>
                            <div className="flex items-center justify-between mb-3">
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest">AVG MONTHLY PLAN (USD)</label>
                                <span className="font-display font-bold text-lg text-[#FFD700]" data-testid="calc-plan-value">${planUsd}</span>
                            </div>
                            <input type="range" min="19" max="199" step="1" value={planUsd}
                                onChange={e => setPlanUsd(Number(e.target.value))}
                                data-testid="calc-plan-slider"
                                className="w-full accent-[#FFD700]" />
                            <div className="flex justify-between font-mono text-[9px] text-[#52525B] mt-1 tracking-widest">
                                <span>$19</span><span>$49</span><span>$99</span><span>$199</span>
                            </div>
                        </div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest pt-2 border-t border-[#1F1F1F]">
                            FORMULA · referrals × plan × 20%
                        </div>
                    </div>

                    {/* Outputs */}
                    <div className="bg-[#0A0A0A] p-6 space-y-5">
                        <Calculator className="w-5 h-5 text-[#FFD700]" />
                        <div data-testid="calc-monthly">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">RECURRING / MONTH</div>
                            <div className="font-display font-bold text-4xl text-white tracking-tighter">
                                ${projection.monthly.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                                <span className="text-base text-[#52525B] ml-2">/ mo</span>
                            </div>
                        </div>
                        <div className="border-t border-[#1F1F1F] pt-4 grid grid-cols-2 gap-4">
                            <div data-testid="calc-yearly">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">YEAR 1</div>
                                <div className="font-display font-bold text-2xl text-[#00FF41]">
                                    ${projection.year.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                                </div>
                            </div>
                            <div data-testid="calc-threeyear">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">3 YEARS</div>
                                <div className="font-display font-bold text-2xl text-[#FFD700]">
                                    ${projection.threeYear.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                                </div>
                            </div>
                        </div>
                        <div className="border border-[#FFB000]/30 bg-[#FFB000]/5 p-3 flex items-start gap-2 mt-2">
                            <AlertTriangle className="w-3.5 h-3.5 text-[#FFB000] shrink-0 mt-0.5" />
                            <p className="font-mono text-[10px] text-[#FFB000] tracking-wide leading-relaxed">
                                Projections are illustrative. Actual earnings depend on retention, churn, and the agreement terms.
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </section>
    );
}


function ValueProps() {
    return (
        <section className="border-b border-[#1F1F1F]" data-testid="value-props">
            <div className="max-w-6xl mx-auto px-6 py-16">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">WHY STOIC</div>
                <h2 className="font-display font-bold tracking-tighter text-3xl md:text-4xl mb-12 max-w-2xl">
                    Built so affiliates win — not just survive.
                </h2>
                <div className="grid md:grid-cols-3 gap-6">
                    {VALUE_PROPS.map(v => (
                        <div key={v.title} className="border-l border-[#FFD700]/30 pl-5">
                            <v.icon className="w-5 h-5 text-[#FFD700] mb-3" />
                            <h3 className="font-display font-bold text-lg tracking-tight mb-2">{v.title}</h3>
                            <p className="text-sm text-[#A1A1AA] leading-relaxed">{v.body}</p>
                        </div>
                    ))}
                </div>
            </div>
        </section>
    );
}


function FaqSection() {
    const [open, setOpen] = useState(null);
    return (
        <section className="border-b border-[#1F1F1F]" data-testid="faq-section">
            <div className="max-w-6xl mx-auto px-6 py-16">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">FAQ</div>
                <h2 className="font-display font-bold tracking-tighter text-3xl md:text-4xl mb-10 max-w-xl">
                    Common questions, straight answers.
                </h2>
                <div className="space-y-2">
                    {FAQ.map((f, i) => {
                        const isOpen = open === i;
                        return (
                            <div key={f.q} className="border border-[#1F1F1F] bg-[#0A0A0A]">
                                <button onClick={() => setOpen(isOpen ? null : i)}
                                    data-testid={`faq-toggle-${i}`}
                                    className="w-full text-left px-5 py-4 flex items-center justify-between gap-4 hover:text-[#FFD700] transition-colors">
                                    <span className="font-display font-bold text-sm md:text-base">{f.q}</span>
                                    <ChevronDown className={`w-4 h-4 text-[#52525B] transition-transform ${isOpen ? "rotate-180" : ""}`} />
                                </button>
                                {isOpen && (
                                    <div className="px-5 pb-5 text-sm text-[#A1A1AA] leading-relaxed border-t border-[#1F1F1F] pt-4"
                                        data-testid={`faq-body-${i}`}>
                                        {f.a}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>
        </section>
    );
}


function TermsPreview() {
    const [open, setOpen] = useState(false);
    return (
        <section className="border-b border-[#1F1F1F]" data-testid="terms-section">
            <div className="max-w-6xl mx-auto px-6 py-12">
                <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">THE AGREEMENT</div>
                        <h2 className="font-display font-bold text-xl tracking-tight">Full program terms</h2>
                    </div>
                    <button onClick={() => setOpen(!open)}
                        data-testid="terms-toggle"
                        className="font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-[#FFD700] px-3 py-1.5 border border-[#1F1F1F] hover:border-[#FFD700]/40 flex items-center gap-1.5">
                        {open ? "HIDE" : "READ FULL"} <ChevronDown className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} />
                    </button>
                </div>
                {open && (
                    <pre className="whitespace-pre-wrap font-mono text-xs text-[#A1A1AA] leading-relaxed bg-[#050505] border border-[#1F1F1F] p-5 max-h-96 overflow-y-auto"
                        data-testid="terms-body">{AFFILIATE_TERMS}</pre>
                )}
            </div>
        </section>
    );
}


function FinalCta() {
    return (
        <section className="border-b border-[#1F1F1F] bg-gradient-to-b from-transparent to-[#FFD700]/5" data-testid="final-cta">
            <div className="max-w-6xl mx-auto px-6 py-20 text-center">
                <h2 className="font-display font-bold tracking-tighter text-4xl md:text-5xl mb-5 max-w-3xl mx-auto">
                    Your audience trades. <span className="text-[#FFD700]">Get paid every time they do.</span>
                </h2>
                <p className="text-base text-[#A1A1AA] max-w-xl mx-auto mb-10">
                    Create your account in 30 seconds. The affiliate application lives inside your dashboard.
                </p>
                <div className="flex flex-wrap items-center justify-center gap-3">
                    <Link to="/register" data-testid="final-cta-register"
                        className="px-7 py-3.5 text-xs font-mono tracking-widest bg-[#FFD700] hover:bg-[#FFE033] text-black flex items-center gap-2">
                        CREATE FREE ACCOUNT <ArrowRight className="w-3.5 h-3.5" />
                    </Link>
                    <Link to="/login" data-testid="final-cta-signin"
                        className="px-7 py-3.5 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#FFD700]/40 text-white">
                        I HAVE AN ACCOUNT
                    </Link>
                </div>
                <div className="mt-12 flex items-center justify-center gap-6 flex-wrap font-mono text-[10px] text-[#52525B] tracking-widest">
                    <span className="flex items-center gap-1.5"><Check className="w-3 h-3 text-[#00FF41]" /> NO SETUP FEE</span>
                    <span className="flex items-center gap-1.5"><Check className="w-3 h-3 text-[#00FF41]" /> MANUAL REVIEW</span>
                    <span className="flex items-center gap-1.5"><Check className="w-3 h-3 text-[#00FF41]" /> LIFETIME COMMISSIONS</span>
                </div>
            </div>
        </section>
    );
}


function PageFooter() {
    return (
        <footer className="bg-[#050505]" data-testid="footer">
            <div className="max-w-6xl mx-auto px-6 py-8 flex flex-wrap items-center justify-between gap-4">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    © STOIC. SOFTWARE, NOT A BROKER.
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    TRADING INVOLVES SIGNIFICANT RISK. PAST PERFORMANCE NOT INDICATIVE.
                </div>
            </div>
        </footer>
    );
}


export default function AffiliateLanding() {
    return (
        <div className="min-h-screen bg-[#050505] text-white" data-testid="affiliate-landing-page">
            <PageNav />
            <HeroSection />
            <HowItWorks />
            <EarningsCalculator />
            <ValueProps />
            <FaqSection />
            <TermsPreview />
            <FinalCta />
            <PageFooter />
        </div>
    );
}
