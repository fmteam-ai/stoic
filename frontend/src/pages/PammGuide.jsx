import { Briefcase, ShieldCheck, Users, Store, Scale, AlertTriangle, LifeBuoy, GitPullRequest, Landmark } from "lucide-react";
import { Link } from "react-router-dom";
import { AppLayout } from "@/components/AppLayout";

const LIMITS = [
    ["Daily loss", "5% of value", "Trading halts for the day"],
    ["Weekly loss", "10% of value", "Trading halts"],
    ["Monthly loss", "15% of value", "Trading halts"],
    ["Max drawdown from peak", "20%", "All positions closed + halt"],
    ["Market exposure", "200% of value", "Trading halts"],
    ["Same-currency positions", "3 max", "Trading halts"],
    ["News blackout", "30 min before / 15 min after high-impact news", "New trades blocked"],
];

const STATES = [
    ["RUNNING", "#00FF41", "Normal operation."],
    ["RISK REDUCED", "#A3E635", "New trades allowed at half size."],
    ["NEW TRADES PAUSED", "#FFB000", "No new trades; open positions managed."],
    ["BROKER UNCERTAIN", "#FF8C00", "Broker connection in doubt — frozen until verified."],
    ["CLOSE-RISK ONLY", "#FF8C00", "Only risk-reducing actions permitted."],
    ["EMERGENCY FLATTEN", "#FF3B30", "Every position is closed and VERIFIED closed with the broker."],
    ["LOCKED", "#FF3B30", "Fully frozen. Unlocking requires TWO different admins."],
];

function Section({ icon: Icon, id, title, children }) {
    return (
        <section id={id} className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid={`pamm-guide-${id}`}>
            <div className="flex items-center gap-2 mb-3">
                <Icon className="w-4 h-4 text-[#A855F7]" />
                <h2 className="font-display font-bold text-base md:text-lg">{title}</h2>
            </div>
            <div className="text-sm text-[#A1A1AA] leading-relaxed space-y-2">{children}</div>
        </section>
    );
}

export default function PammGuide() {
    return (
        <AppLayout>
        <div className="px-4 md:px-8 py-6 space-y-6 max-w-4xl" data-testid="pamm-guide-page">
            <div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">LEARN · MANAGED MONEY</div>
                <h1 className="font-display font-bold text-2xl md:text-3xl">PAMM Accounts — How They Work</h1>
                <p className="text-sm text-[#A1A1AA] mt-2 max-w-2xl">
                    A PAMM program lets a professional manager trade one master account while investors
                    participate proportionally. Your money always sits at the <span className="text-white">broker</span> —
                    STOIC is the strategy, risk-control and transparency layer on top.
                </p>
            </div>

            <Section icon={Landmark} id="custody" title="Who holds the money?">
                <p><span className="text-white">The broker does — always.</span> STOIC never touches balances, never
                    calculates your share, and cannot move funds. Deposits, allocations and account value (NAV) are
                    executed and computed by the broker's own PAMM engine. STOIC mirrors that data, decides whether
                    trading is <em>allowed</em>, and records everything for audit.</p>
            </Section>

            <Section icon={Users} id="roles" title="The three roles">
                <ul className="list-disc pl-5 space-y-1">
                    <li><span className="text-white">Investor</span> — any STOIC user. Browse the <Link to="/marketplace" className="text-[#A855F7] underline">Marketplace</Link>, request to join a program, track your requests and the program's performance.</li>
                    <li><span className="text-white">Manager</span> — runs the strategy on the program's master account. Can pause trading, tune risk protections (tightening only, instantly), publish to the marketplace and approve investors.</li>
                    <li><span className="text-white">Admin</span> — platform operator. Creates programs, registers brokers, clears emergency states and approves any change that weakens protection.</li>
                </ul>
            </Section>

            <Section icon={Store} id="join" title="Joining as an investor">
                <ol className="list-decimal pl-5 space-y-1">
                    <li>Open the <Link to="/marketplace" className="text-[#A855F7] underline">Marketplace</Link> — only published, active programs appear, each with live return and drawdown figures computed from broker NAV.</li>
                    <li>Send a <span className="text-white">join request</span> with your intended amount. One pending request per program.</li>
                    <li>The manager reviews it. On approval, the <span className="text-white">broker</span> onboards you and allocates your amount — STOIC mirrors the result and notifies you in-app either way.</li>
                    <li>From then on your share rises and falls with the program's NAV. The manager's performance fee (shown on every listing, e.g. 20%) is handled broker-side.</li>
                </ol>
            </Section>

            <Section icon={ShieldCheck} id="protections" title="Automatic protections (always on)">
                <p>Every program ships with hard safety rails, checked automatically about once a minute and before every trade:</p>
                <div className="overflow-x-auto mt-2">
                    <table className="w-full text-xs font-mono">
                        <thead><tr className="text-[#52525B] text-left">
                            <th className="py-1 pr-4">PROTECTION</th><th className="py-1 pr-4">DEFAULT CAP</th><th className="py-1">WHAT HAPPENS AT THE CAP</th>
                        </tr></thead>
                        <tbody>
                            {LIMITS.map(([n, t, a]) => (
                                <tr key={n} className="border-t border-[#141414]">
                                    <td className="py-1.5 pr-4 text-white">{n}</td>
                                    <td className="py-1.5 pr-4 text-[#FFB000]">{t}</td>
                                    <td className="py-1.5 text-[#A1A1AA]">{a}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
                <p className="mt-2">Trades are not simply allowed or blocked: as a limit's utilization grows, every new
                    trade's size is <span className="text-white">scaled down progressively</span> (APPROVE → REDUCE → REJECT),
                    so the program glides toward its caps instead of slamming into them.</p>
            </Section>

            <Section icon={AlertTriangle} id="states" title="Emergency states — the safety ladder">
                <p>Each program lives in exactly one operating state. Automation may only move <em>down</em> the ladder
                    (safer). Moving back up requires a human with fresh two-factor verification — and leaving
                    LOCKED requires <span className="text-white">two different admins</span>.</p>
                <div className="mt-2 space-y-1">
                    {STATES.map(([s, c, d]) => (
                        <div key={s} className="flex items-start gap-2">
                            <span className="font-mono text-[10px] px-1.5 py-0.5 border shrink-0 mt-0.5" style={{ color: c, borderColor: `${c}40` }}>{s}</span>
                            <span className="text-xs">{d}</span>
                        </div>
                    ))}
                </div>
                <p className="mt-2">An emergency flatten is never assumed to have worked: STOIC re-checks the broker and
                    only stands down when the broker <span className="text-white">confirms zero open positions</span>. If not,
                    it retries, opens a formal incident, and pages an operator — unresolved exposure is treated as a
                    first-class emergency.</p>
            </Section>

            <Section icon={Scale} id="truth" title="Position Truth — trust, but verify">
                <p>STOIC keeps its own record of what positions <em>should</em> exist and regularly compares it against
                    the broker's actual book. Any unexplained difference beyond the program's drift tolerance
                    <span className="text-white"> freezes trading instantly</span> and opens an incident. An admin must
                    explicitly review and acknowledge reality before trading can even be considered again — and resuming
                    is a separate, verified step.</p>
            </Section>

            <Section icon={GitPullRequest} id="dualauth" title="Two-person rule for weakening protection">
                <p>Anything that would make a program <em>less</em> safe — raising a loss cap, disabling a protection,
                    shrinking the news blackout, increasing drift tolerance, unlocking a LOCKED program — cannot be done
                    by one person. It creates a pending change request that a <span className="text-white">second, different
                    admin</span> must approve within 48 hours. Tightening protections, by contrast, applies immediately.</p>
            </Section>

            <Section icon={Briefcase} id="managers" title="For managers — running a program">
                <ul className="list-disc pl-5 space-y-1">
                    <li>Your console lives at <Link to="/managed" className="text-[#A855F7] underline">Managed Strategy</Link> (admin-granted manager role required).</li>
                    <li>Monitor NAV, performance, allocations, risk status and the event log per program.</li>
                    <li>Pause any time; resuming needs fresh two-factor verification.</li>
                    <li>Tighten risk limits instantly; loosening routes through the two-person rule.</li>
                    <li>Publish with a pitch to appear in the Marketplace; review and approve join requests (approval triggers the broker-side allocation automatically).</li>
                    <li>Every risky action is step-up verified, rate-limited and written to the audit trail.</li>
                </ul>
            </Section>

            <Section icon={LifeBuoy} id="more" title="Questions?">
                <p>Deeper technical detail (API reference, data model, broker certification) lives in the repository at
                    <span className="font-mono text-white"> docs/PAMM.md</span>. For anything else, visit the
                    <Link to="/help" className="text-[#A855F7] underline"> Help Center</Link> or
                    <Link to="/support" className="text-[#A855F7] underline"> Support</Link>.</p>
            </Section>
        </div>
        </AppLayout>
    );
}
