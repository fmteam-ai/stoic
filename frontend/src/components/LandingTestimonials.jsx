// Landing-page testimonials — front-page "wall of trust".
import { useEffect, useRef, useState } from "react";
import { BACKEND_URL } from "@/lib/api";

// ─── EDIT YOUR TESTIMONIALS HERE ────────────────────────────────────
// ILLUSTRATIVE PLACEHOLDERS — these are NOT real customer reviews and
// are clearly labeled as such in the UI (section note + per-card badge
// + footer disclaimer). Swap for real, written-authorization quotes and
// remove the labels ONLY when every quote is genuine and authorized.
const TESTIMONIALS = [
    { name: "Marcus T.", role: "Former prop-desk trader", rating: 5,
      quote: "The first bot I've used that tells me WHY it refused a trade. The veto log is worth the subscription alone." },
    { name: "Elena K.", role: "Software engineer, hobby trader", rating: 5,
      quote: "Shadow Mode sold me. I watched it paper-trade my account for three weeks before I let it touch a cent." },
    { name: "David R.", role: "Runs 2 MT5 accounts", rating: 5,
      quote: "One-command VPS setup actually works. My terminal was paired and heartbeating in under ten minutes." },
    { name: "Priya S.", role: "Risk manager", rating: 5,
      quote: "Fail-closed by default. When my broker feed went stale it stopped trading instead of guessing. That's the whole point." },
    { name: "Jonas W.", role: "Swing trader, 8 yrs", rating: 4,
      quote: "It's opinionated — it will refuse trades you want. Two months in, I've stopped arguing with it." },
    { name: "Aisha B.", role: "Quant enthusiast", rating: 5,
      quote: "The explainability reports read like a junior analyst wrote them. I finally understand every position I hold." },
    { name: "Tom H.", role: "Gold & BTC only", rating: 5,
      quote: "Daily loss caps, cooldowns, kill-switch — the guardrails are the product. Everything else is a bonus." },
    { name: "Sofia M.", role: "Left her old EA for STOIC", rating: 5,
      quote: "My old EA martingaled my account to zero. This one sizes down when it's cold. Night and day." },
    { name: "Ravi P.", role: "Part-time trader", rating: 4,
      quote: "Support walked me through broker pairing on a Sunday. The identity verification is strict — and I'm glad it is." },
    { name: "Claire D.", role: "Fintech PM", rating: 5,
      quote: "No auto-renewals, cancel anytime, and it downgrades gracefully. Billing that respects you is rare." },
];
// ────────────────────────────────────────────────────────────────────

const API = BACKEND_URL;

// Cosmetic count-up only: the REAL value is always rendered (starts at the
// target, animates from 0 once the bar scrolls into view). Placeholders are
// never shown next to a "live" claim (release review P1-1).
const useCountUp = (target, animate, duration = 1600) => {
    const [val, setVal] = useState(target ?? 0);
    useEffect(() => {
        if (target == null) return;
        if (!animate) { setVal(target); return; }
        let raf; const t0 = performance.now();
        const tick = (now) => {
            const p = Math.min(1, (now - t0) / duration);
            setVal(target * (1 - Math.pow(1 - p, 3)));
            if (p < 1) raf = requestAnimationFrame(tick);
        };
        raf = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(raf);
    }, [target, animate, duration]);
    return val;
};

const Stat = ({ label, value, animate, suffix = "", decimals = 0, testId }) => {
    const v = useCountUp(value, animate);
    return (
        <div className="tst-stat" data-testid={testId}>
            <div className="tst-stat-value">
                {value == null ? "—"
                    : (animate ? v : value).toLocaleString(undefined, {
                        minimumFractionDigits: decimals,
                        maximumFractionDigits: decimals })}{suffix}
            </div>
            <div className="tst-stat-label">{label}</div>
        </div>
    );
};

// audit P1-1/P1-2 — "live" means: fresh payload, versioned populations,
// both counts present. Availability is a SEPARATE claim shown ONLY when the
// backend reports a complete, reconciled 30-day edge-probe window.
const statsAreLive = (s) => {
    if (!s || !s.as_of || !s.population_version) return false;
    if (s.published !== true) return false;            // P2-2: withheld until legal/compliance approval
    const age = (Date.now() - new Date(s.as_of).getTime()) / 1000;
    return Number.isFinite(age) && age <= (s.ttl_seconds || 900)
        && s.verified_active_accounts?.count != null
        && s.execution_intents_blocked?.count != null;
};
const availabilityIsPublishable = (s) =>
    s?.availability?.status === "reconciled" && s.availability.reconciled === true
    && typeof s.availability.value_pct === "number";

const TrustBar = ({ onStatus }) => {
    const [stats, setStats] = useState(null);
    const [visible, setVisible] = useState(false);
    const ref = useRef(null);
    useEffect(() => {
        fetch(`${API}/api/public/trust-stats`)
            .then(r => (r.ok ? r.json() : null))
            .then(s => { setStats(s); onStatus?.(statsAreLive(s) ? "live" : "unavailable"); })
            .catch(() => { setStats(null); onStatus?.("unavailable"); });
    }, [onStatus]);
    useEffect(() => {
        const el = ref.current;
        if (!el) return;
        const obs = new IntersectionObserver(
            ([e]) => e.isIntersecting && setVisible(true),
            { threshold: 0.4 });
        obs.observe(el);
        return () => obs.disconnect();
    }, [stats]);
    if (!statsAreLive(stats)) {
        return (
            <div className="tst-trustbar" data-testid="trust-bar-unavailable">
                <div className="tst-stat-label">{stats && stats.published === false ? "Platform statistics pending compliance review" : "Live statistics unavailable"}</div>
            </div>
        );
    }
    const acc = stats.verified_active_accounts;
    const blk = stats.execution_intents_blocked;
    const env = acc.environment || {};
    const envLine = ["LIVE", "DEMO", "PAPER"].filter(k => env[k]).map(k => `${env[k]} ${k.toLowerCase()}`).join(" · ") || "no environment breakdown";
    return (
        <div ref={ref} className="tst-trustbar" data-testid="trust-bar"
             data-population-version={stats.population_version}
             title={`${stats.population_version} · as of ${stats.as_of} · ${stats.source} · period ${stats.period?.days}d · ${stats.reconciliation}`}>
            <Stat label="Verified active accounts with safety policy enabled" testId="trust-stat-accounts"
                  value={acc.count} animate={visible} />
            <div className="tst-stat-divider" />
            <Stat label="Execution intents blocked by risk policy · 30d" testId="trust-stat-vetoed"
                  value={blk.count} animate={visible} />
            {availabilityIsPublishable(stats) && (
                <>
                    <div className="tst-stat-divider" />
                    <Stat label="Edge availability · 30d (SLI)" testId="trust-stat-availability"
                          value={stats.availability.value_pct} animate={visible}
                          suffix="%" decimals={2} />
                </>
            )}
            <div className="tst-stat-footnote" data-testid="trust-stat-footnote">
                <b>{stats.context}</b> · {envLine} · {acc.definition} · blocked = {blk.definition}
                {!availabilityIsPublishable(stats) && " · availability: published only after a complete, reconciled 30-day edge-probe window"}
            </div>
        </div>
    );
};

const Stars = ({ n }) => (
    // audit F-12 — visual stars render from the SAME numeric value as the
    // accessible label: filled ★ for rating, hollow ☆ for the remainder.
    <div className="tst-stars" role="img" aria-label={`${n} out of 5 stars`}>
        {[1, 2, 3, 4, 5].map(i => (
            <span key={i} aria-hidden="true"
                className={i <= n ? "tst-star on" : "tst-star"}>
                {i <= n ? "★" : "☆"}
            </span>
        ))}
    </div>
);

// audit round 8 P2-5 — decorative marquee clones carry NO semantic descendants
// (no figure/blockquote/figcaption/text nodes for AT): purely visual boxes.
const CloneCard = ({ t }) => (
    <div className="tst-card" aria-hidden="true" data-marquee-clone="true">
        <span className="tst-illustrative" aria-hidden="true">Illustrative example</span>
        <div className="tst-quote" aria-hidden="true">{`\u201C${t.quote}\u201D`}</div>
        <div className="tst-person" aria-hidden="true">
            <span className="tst-avatar">{t.name.split(" ").map(w => w[0]).join("").slice(0, 2)}</span>
            <span><span className="tst-name">{t.name}</span><span className="tst-role">{t.role}</span></span>
        </div>
    </div>
);

// P2-04 — real cards carry a UNIQUE id (clones carry none, so duplicate ids never occur)
const Card = ({ t, idx }) => (
    <figure className="tst-card" id={`testimonial-${idx}`} data-testid={`testimonial-card-${idx}`}>
        <span className="tst-illustrative">Illustrative example</span>
        <Stars n={t.rating} />
        <blockquote className="tst-quote">&ldquo;{t.quote}&rdquo;</blockquote>
        <figcaption className="tst-person">
            <span className="tst-avatar">{t.name.split(" ").map(w => w[0]).join("").slice(0, 2)}</span>
            <span>
                <span className="tst-name">{t.name}</span>
                <span className="tst-role">{t.role}</span>
            </span>
        </figcaption>
    </figure>
);

export const LandingTestimonials = () => {
    const rowA = TESTIMONIALS.filter((_, i) => i % 2 === 0);
    const rowB = TESTIMONIALS.filter((_, i) => i % 2 === 1);
    const [statsStatus, setStatsStatus] = useState("loading");
    const [reducedMotion, setReducedMotion] = useState(
        () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
    const [paused, setPaused] = useState(false);   // N99-8 — keyboard pause (Space/Enter)
    useEffect(() => {
        const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
        if (!mq) return undefined;
        const on = (e) => setReducedMotion(e.matches);
        mq.addEventListener?.("change", on);
        return () => mq.removeEventListener?.("change", on);
    }, []);
    const statsLine = statsStatus === "live"
        ? "The counts are live platform data with defined, versioned populations (aggregate, refreshed every 15 minutes). Availability is shown only from an independent, reconciled 30-day edge-probe window."
        : "Live statistics unavailable.";
    return (
        <section className="tst-section" data-testid="testimonials-section">
            <div className="kicker">Wall of trust</div>
            <h2 className="tst-headline">
                Built for people who <em>hate</em> losing stupidly.
            </h2>
            <p className="tst-subline">
                What living with a bot that says &ldquo;no&rdquo; a lot is designed to feel like.
            </p>
            <p className="tst-illustrative-note" data-testid="testimonials-illustrative-label">
                The quotes below are illustrative examples, not real customer reviews.
                {" "}<span data-testid="testimonials-stats-status">{statsLine}</span>
            </p>
            <TrustBar onStatus={setStatsStatus} />
            <div className="tst-marquee" data-testid="testimonials-marquee" data-reduced-motion={reducedMotion ? "true" : "false"}
                 role="region" aria-roledescription="carousel" tabIndex={0}
                 onKeyDown={(e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); setPaused((v) => !v); } }}
                 data-paused={paused ? "true" : "false"} aria-pressed={paused}
                 aria-label="Illustrative testimonials — press Space or Enter to pause or resume the scrolling; decorative scrolling duplicates are hidden from assistive technology">
                <div className="tst-track">
                    {rowA.map((t, i) => <Card key={`a${i}`} t={t} idx={i * 2} />)}
                    {/* audit F-19 / round-8 P2-5 — clones are decorative, semantic-free and absent under reduced motion */}
                    {!reducedMotion && rowA.map((t, i) => <div key={`ac${i}`} aria-hidden="true" inert={true}><CloneCard t={t} /></div>)}
                </div>
                <div className="tst-track reverse">
                    {rowB.map((t, i) => <Card key={`b${i}`} t={t} idx={i * 2 + 1} />)}
                    {!reducedMotion && rowB.map((t, i) => <div key={`bc${i}`} aria-hidden="true" inert={true}><CloneCard t={t} /></div>)}
                </div>
            </div>
            <p className="tst-honesty" data-testid="testimonials-disclaimer">
                Quotes are illustrative examples, not real customer reviews.
                {" "}{statsStatus === "live" ? "Platform stats are live aggregate data." : "Live statistics unavailable."}
                {" "}Not a promise of performance — trading involves risk.
            </p>
        </section>
    );
};
