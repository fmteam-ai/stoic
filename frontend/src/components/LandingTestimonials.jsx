// Landing-page testimonials — front-page "wall of trust".
// ─── EDIT YOUR TESTIMONIALS HERE ────────────────────────────────────
// Quotes are deliberately about experience, safety and control — not
// profit promises (compliance-safe for a trading product). Swap these
// for real customer quotes whenever you have them.
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

const Stars = ({ n }) => (
    <div className="tst-stars" aria-label={`${n} out of 5 stars`}>
        {[1, 2, 3, 4, 5].map(i => (
            <span key={i} className={i <= n ? "tst-star on" : "tst-star"}>★</span>
        ))}
    </div>
);

const Card = ({ t, idx }) => (
    <figure className="tst-card" data-testid={`testimonial-card-${idx}`}>
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
    return (
        <section className="tst-section" data-testid="testimonials-section">
            <div className="kicker">Wall of trust</div>
            <h2 className="tst-headline">
                Built for people who <em>hate</em> losing stupidly.
            </h2>
            <p className="tst-subline">
                What traders say about living with a bot that says &ldquo;no&rdquo; a lot.
            </p>
            <div className="tst-marquee" data-testid="testimonials-marquee">
                <div className="tst-track">
                    {[...rowA, ...rowA].map((t, i) => (
                        <Card key={`a${i}`} t={t} idx={i < rowA.length ? i * 2 : undefined} />
                    ))}
                </div>
                <div className="tst-track reverse">
                    {[...rowB, ...rowB].map((t, i) => (
                        <Card key={`b${i}`} t={t} idx={i < rowB.length ? i * 2 + 1 : undefined} />
                    ))}
                </div>
            </div>
            <p className="tst-honesty" data-testid="testimonials-disclaimer">
                Individual experiences — not a promise of performance. Trading involves risk.
            </p>
        </section>
    );
};
