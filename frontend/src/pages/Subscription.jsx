import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Check, X, Sparkles, Loader2, Shield, Crown, Rocket, BrainCircuit } from "lucide-react";

const TIER_IDS = ["starter", "trader", "professional", "elite_ai"];

const TIER_META = {
    starter: {
        label: "Starter", icon: Shield, ringHex: "#52525B",
        ringClass: "border-[#1F1F1F]",
        accent: "text-[#A1A1AA]",
        tagline: "New traders. 1 account, Shadow + Demo trading.",
    },
    trader: {
        label: "Trader", icon: Rocket, ringHex: "#00FF41",
        ringClass: "border-[#00FF41] shadow-[0_0_30px_rgba(0,255,65,0.18)]",
        accent: "text-[#00FF41]",
        tagline: "Supervised Live. 3 accounts, Replay Studio + AI Coach.",
        recommended: true,
    },
    professional: {
        label: "Professional", icon: Crown, ringHex: "#A855F7",
        ringClass: "border-[#A855F7] shadow-[0_0_30px_rgba(168,85,247,0.22)]",
        accent: "text-[#A855F7]",
        tagline: "10 accounts. Digital Twin, Research Lab, VPS management.",
    },
    elite_ai: {
        label: "Elite AI", icon: BrainCircuit, ringHex: "#FFD700",
        ringClass: "border-[#FFD700] shadow-[0_0_30px_rgba(255,215,0,0.18)]",
        accent: "text-[#FFD700]",
        tagline: "The flagship. 50 accounts, Autonomous Live after certification.",
    },
};

const MODE_LABELS = {
    demo_autopilot: "Demo + Shadow",
    supervised_live: "Supervised Live",
    autonomous_live: "Autonomous Live*",
};

// Marketing-friendly row order for the comparison table. Each row maps a
// human label to a `Features` key from backend/subscription_plans.py.
const FEATURE_ROWS = [
    { k: "max_accounts", label: "MT5 accounts",
      render: (v) => v < 0 ? "Unlimited" : `${v}` },
    { k: "max_operational_mode", label: "Max trading mode",
      render: (v) => MODE_LABELS[v] || v },
    { k: "paper_shadow_mode", label: "Shadow Mode" },
    { k: "loss_lab", label: "Loss Lab AI post-mortems" },
    { k: "replay_studio", label: "Replay Studio" },
    { k: "ai_coach", label: "AI Coach" },
    { k: "evidence_board", label: "Evidence Board" },
    { k: "broker_intelligence", label: "Broker Intelligence" },
    { k: "vps_quick_connect", label: "VPS Quick Connect" },
    { k: "vps_management", label: "VPS management dashboard" },
    { k: "multi_vps", label: "Multiple VPS management" },
    { k: "digital_twin", label: "Digital Twin" },
    { k: "research_lab", label: "Research Lab" },
    { k: "strategy_marketplace", label: "Strategy Marketplace" },
    { k: "portfolio_optimization", label: "Portfolio optimization" },
    { k: "calibrated_p_win", label: "AI probability calibration" },
    { k: "chaos_testing", label: "Chaos testing" },
    { k: "agent_report_cards", label: "Agent report cards" },
    { k: "strategy_evolution", label: "Strategy evolution" },
    { k: "hypothesis_generation", label: "AI hypothesis generation" },
    { k: "fleet_monitoring", label: "Fleet monitoring" },
    { k: "white_label_reporting", label: "White-label reporting" },
    { k: "api_access", label: "API access" },
    { k: "priority_notifications", label: "Mobile + Telegram alerts" },
    { k: "min_signal_cooldown_minutes", label: "Min signal cooldown",
      render: (v) => `${v} min` },
    { k: "support_tier", label: "Support",
      render: (v) => (v || "").charAt(0).toUpperCase() + (v || "").slice(1) },
];

const DURATIONS = [
    { id: "monthly", label: "Monthly" },
    { id: "quarterly", label: "3-month" },
    { id: "semi_annual", label: "6-month" },
    { id: "annual", label: "Annual" },
];

export default function Subscription() {
    const navigate = useNavigate();
    const [plans, setPlans] = useState([]);
    const [tierMatrix, setTierMatrix] = useState({});
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(true);
    const [creating, setCreating] = useState(null);
    const [err, setErr] = useState("");
    const [duration, setDuration] = useState("annual");
    const [preview, setPreview] = useState(null);
    const [transactions, setTransactions] = useState([]);

    useEffect(() => {
        let cancel = false;
        (async () => {
            try {
                const [pRes, sRes, tRes, upRes, txRes] = await Promise.all([
                    api.get("/subscription/plans"),
                    api.get("/subscription/status"),
                    api.get("/entitlements/tiers"),
                    api.get("/subscription/upgrade-preview").catch(() => null),
                    api.get("/subscription/transactions").catch(() => null),
                ]);
                if (cancel) return;
                setPlans(pRes.data);
                setStatus(sRes.data);
                setTierMatrix(tRes.data);
                if (upRes) setPreview(upRes.data);
                if (txRes) setTransactions(txRes.data);
            } catch (e) {
                setErr(formatApiError(e));
            } finally { if (!cancel) setLoading(false); }
        })();
        return () => { cancel = true; };
    }, []);

    // Bucket plans by tier for the duration selector to pick the active SKU.
    const plansByTier = useMemo(() => {
        const out = { starter: {}, trader: {}, professional: {}, elite_ai: {} };
        for (const p of plans) {
            if (!out[p.tier]) continue;
            out[p.tier][p.duration_months === 1 ? "monthly"
                : p.duration_months === 3 ? "quarterly"
                : p.duration_months === 6 ? "semi_annual"
                : "annual"] = p;
        }
        return out;
    }, [plans]);

    const sym = plans[0]?.currency_symbol || "$";

    const startCheckout = async (planId) => {
        setCreating(planId); setErr("");
        try {
            const { data } = await api.post("/subscription/checkout", {
                plan_id: planId, origin: window.location.origin,
            });
            window.location.href = data.checkout_url;
        } catch (e) {
            setErr(formatApiError(e));
            setCreating(null);
        }
    };

    if (loading) {
        return <AppLayout><div className="flex items-center justify-center min-h-[40vh]">
            <Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div></AppLayout>;
    }

    return (
        <AppLayout>
            <PageHeader title="Subscription" subtitle="Pick your tier and your commitment." />

            {err && <div data-testid="subscription-error"
                className="text-sm text-[#FF3B30] border border-[#FF3B30]/40 bg-[#FF3B30]/5 px-3 py-2 rounded">
                {err}</div>}

            {status?.entitlement?.active && (
                <div data-testid="active-sub-banner"
                    className="text-sm text-[#00FF41] border border-[#00FF41]/30 bg-[#00FF41]/5 px-4 py-3 rounded mb-6">
                    <Sparkles className="inline-block w-4 h-4 mr-2" />
                    {String(status.entitlement.plan_id || "").startsWith("trial_")
                        ? <span data-testid="trial-badge">Free trial · <span className="font-mono">{status.entitlement.plan_id.replace("trial_", "").replace("_", " ")}</span> tier</span>
                        : <>Active: <span className="font-mono">{status.entitlement.plan_id || "grandfathered"}</span></>}
                    {status.entitlement.valid_until && ` · until ${new Date(status.entitlement.valid_until).toLocaleDateString()}`}
                </div>
            )}

            {/* Duration selector */}
            <div className="flex flex-wrap gap-2 mb-6" data-testid="duration-selector">
                {DURATIONS.map(d => (
                    <button key={d.id} onClick={() => setDuration(d.id)}
                        data-testid={`duration-${d.id}`}
                        className={`px-4 py-2 rounded text-xs font-mono uppercase transition ${
                            duration === d.id ? "bg-[#00FF41]/10 text-[#00FF41] border border-[#00FF41]/50"
                                              : "bg-[#0F0F0F] text-[#A1A1AA] border border-[#1F1F1F] hover:border-[#52525B]"
                        }`}>
                        {d.label}
                        {(() => { const disc = plansByTier.trader?.[d.id]?.discount_pct ?? plans.find(p => p.duration_months === (d.id === "monthly" ? 1 : d.id === "quarterly" ? 3 : d.id === "semi_annual" ? 6 : 12))?.discount_pct;
                            const badge = disc > 0 ? `${d.id === "annual" ? "BEST · " : ""}−${disc}%` : null;
                            return badge && <span className={`ml-2 ${duration === d.id ? "text-[#00FF41]" : "text-[#FFB000]"}`}>{badge}</span>; })()}
                    </button>
                ))}
            </div>

            {/* Tier cards */}
            <div className="grid md:grid-cols-2 xl:grid-cols-4 gap-4 mb-12">
                {TIER_IDS.map(tierId => {
                    const meta = TIER_META[tierId];
                    const Icon = meta.icon;
                    const plan = plansByTier[tierId]?.[duration];
                    if (!plan) return null;
                    const isCurrent = status?.entitlement?.plan_id === plan.id;
                    return (
                        <div key={tierId} data-testid={`tier-card-${tierId}`}
                            className={`relative bg-[#0A0A0A] border-2 ${meta.ringClass} rounded-lg p-6`}>
                            {meta.recommended && (
                                <div className="absolute -top-3 left-6 px-2 py-0.5 text-[10px] font-mono bg-[#00FF41] text-black rounded">
                                    MOST POPULAR
                                </div>
                            )}
                            <div className="flex items-center gap-2 mb-1">
                                <Icon className={`w-5 h-5 ${meta.accent}`} />
                                <div className={`font-display text-xl ${meta.accent}`}>{meta.label}</div>
                            </div>
                            <div className="text-xs text-[#71717A] mb-4">{meta.tagline}</div>

                            <div className="mb-1">
                                <span className="font-display text-4xl text-[#FAFAFA]">{sym}{plan.amount_usd}</span>
                                <span className="text-xs text-[#71717A] ml-2">/ {plan.duration_label}</span>
                            </div>
                            {plan.duration_months > 1 && (
                                <div className="text-xs text-[#A1A1AA] mb-4">
                                    ≈ {sym}{plan.effective_monthly_usd}/mo
                                    {plan.savings_usd > 0 && (
                                        <span className="ml-2 text-[#00FF41]">save {sym}{plan.savings_usd}</span>
                                    )}
                                </div>
                            )}
                            {plan.duration_months === 1 && <div className="h-4" />}

                            <button onClick={() => startCheckout(plan.id)}
                                disabled={creating === plan.id || isCurrent}
                                data-testid={`subscribe-${tierId}-${duration}`}
                                className={`w-full py-2 rounded text-xs font-mono uppercase transition mb-4 ${
                                    isCurrent ? "bg-[#0F0F0F] text-[#52525B] cursor-not-allowed"
                                    : tierId === "trader" ? "bg-[#00FF41] text-black hover:bg-[#33FF66]"
                                    : tierId === "professional" ? "bg-[#A855F7] text-white hover:bg-[#9333EA]"
                                    : tierId === "elite_ai" ? "bg-[#FFD700] text-black hover:bg-[#FFE44D]"
                                    : "bg-[#FAFAFA] text-black hover:bg-white"
                                }`}>
                                {isCurrent ? "Current plan"
                                 : creating === plan.id ? <><Loader2 className="inline w-3 h-3 mr-2 animate-spin" />Redirecting…</>
                                 : `Subscribe — ${sym}${plan.amount_usd}`}
                            </button>

                            {(() => {
                                const p = preview?.active ? preview?.previews?.[plan.id] : null;
                                if (!p || isCurrent) return null;
                                if (p.kind === "upgrade" && p.credited_days >= 1) return (
                                    <div data-testid={`proration-hint-${tierId}`}
                                        className="text-[10px] font-mono text-[#00FF41] -mt-2 mb-4">
                                        +{Math.round(p.credited_days)} days credited from your current plan
                                    </div>
                                );
                                if (p.kind === "downgrade_scheduled") return (
                                    <div data-testid={`proration-hint-${tierId}`}
                                        className="text-[10px] font-mono text-[#FFB000] -mt-2 mb-4">
                                        Starts {new Date(p.starts_at).toLocaleDateString()} — after your current plan ends
                                    </div>
                                );
                                if (p.kind === "extend") return (
                                    <div data-testid={`proration-hint-${tierId}`}
                                        className="text-[10px] font-mono text-[#A1A1AA] -mt-2 mb-4">
                                        Extends your plan to {new Date(p.new_valid_until).toLocaleDateString()}
                                    </div>
                                );
                                return null;
                            })()}

                            {/* Per-tier highlights */}
                            <div className="space-y-1.5 text-xs">
                                {(tierMatrix[tierId] ? FEATURE_ROWS.slice(0, 8) : []).map(row => {
                                    const v = tierMatrix[tierId][row.k];
                                    const isOn = v === true || (typeof v === "number" && v !== 0) || (typeof v === "string" && v.length > 0);
                                    const display = row.render ? row.render(v) : (isOn ? "Included" : "—");
                                    return (
                                        <div key={row.k} className="flex items-start gap-2">
                                            {isOn || display !== "—"
                                                ? <Check className="w-3.5 h-3.5 mt-0.5 text-[#00FF41] flex-shrink-0" />
                                                : <X className="w-3.5 h-3.5 mt-0.5 text-[#52525B] flex-shrink-0" />}
                                            <div>
                                                <span className="text-[#A1A1AA]">{row.label}</span>
                                                {typeof display === "string" && display !== "Included" && display !== "—" && (
                                                    <span className="ml-1 text-[#FAFAFA]">{display}</span>
                                                )}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    );
                })}
            </div>

            {/* Full feature comparison */}
            <div className="border border-[#1F1F1F] rounded-lg overflow-hidden" data-testid="feature-comparison">
                <div className="bg-[#0F0F0F] px-4 py-3 font-display text-sm">Full feature comparison</div>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                    <thead className="bg-[#0A0A0A] text-[#52525B]">
                        <tr>
                            <th className="text-left px-4 py-2 font-mono">Feature</th>
                            {TIER_IDS.map(t => (
                                <th key={t} className={`text-center px-4 py-2 font-mono ${TIER_META[t].accent}`}>
                                    {TIER_META[t].label}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {FEATURE_ROWS.map((row, i) => (
                            <tr key={row.k} className={i % 2 === 0 ? "bg-[#0A0A0A]" : "bg-[#0F0F0F]"}>
                                <td className="px-4 py-2 text-[#A1A1AA]">{row.label}</td>
                                {TIER_IDS.map(t => {
                                    const v = (tierMatrix[t] || {})[row.k];
                                    const isOn = v === true || (typeof v === "number" && v !== 0)
                                                  || (typeof v === "string" && v.length > 0);
                                    const display = row.render ? row.render(v)
                                                                : (isOn ? <Check className="w-4 h-4 text-[#00FF41] mx-auto" />
                                                                       : <X className="w-4 h-4 text-[#52525B] mx-auto" />);
                                    return (
                                        <td key={t} className="text-center px-4 py-2 text-[#FAFAFA]">
                                            {display}
                                        </td>
                                    );
                                })}
                            </tr>
                        ))}
                    </tbody>
                </table>
                </div>
            </div>

            <div className="text-xs text-[#52525B] mt-4">
                *Autonomous Live activates only for accounts that pass STOIC&apos;s broker-certification
                process — a subscription alone never bypasses the promotion gate.
            </div>
            <div className="text-xs text-[#52525B] mt-2">
                All plans are prepaid. No surprise renewals — your plan stays active until the
                <span className="text-[#A1A1AA]"> &ldquo;valid until&rdquo;</span> date and we&apos;ll email you before it lapses.
                Cancel any time = simply don&apos;t renew.
            </div>

            {/* Billing history */}
            {transactions.length > 0 && (
                <div className="border border-[#1F1F1F] rounded-lg overflow-hidden mt-10"
                    data-testid="billing-history">
                    <div className="bg-[#0F0F0F] px-4 py-3 font-display text-sm">Billing history</div>
                    <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                        <thead className="bg-[#0A0A0A] text-[#52525B]">
                            <tr>
                                <th className="text-left px-4 py-2 font-mono">DATE</th>
                                <th className="text-left px-4 py-2 font-mono">PLAN</th>
                                <th className="text-right px-4 py-2 font-mono">AMOUNT</th>
                                <th className="text-left px-4 py-2 font-mono">STATUS</th>
                            </tr>
                        </thead>
                        <tbody>
                            {transactions.map((tx, i) => (
                                <tr key={tx.id} data-testid={`billing-row-${i}`}
                                    className={i % 2 === 0 ? "bg-[#0A0A0A]" : "bg-[#0F0F0F]"}>
                                    <td className="px-4 py-2 text-[#A1A1AA] font-mono">
                                        {tx.created_at ? new Date(tx.created_at).toLocaleString() : "—"}
                                    </td>
                                    <td className="px-4 py-2 text-[#FAFAFA] font-mono">
                                        {(tx.plan_id || "—").replace(/_/g, " ")}
                                    </td>
                                    <td className="px-4 py-2 text-right text-[#FAFAFA] font-mono">
                                        ${tx.amount_usd.toFixed(2)} {tx.currency}
                                    </td>
                                    <td className="px-4 py-2">
                                        <span className={`px-2 py-0.5 rounded text-[10px] font-mono uppercase ${
                                            tx.payment_status === "paid" ? "bg-[#00FF41]/10 text-[#00FF41]"
                                            : tx.payment_status === "refunded" ? "bg-[#FF3B30]/10 text-[#FF3B30]"
                                            : tx.payment_status === "expired" ? "bg-[#52525B]/10 text-[#52525B]"
                                            : "bg-[#FFB000]/10 text-[#FFB000]"
                                        }`}>
                                            {tx.payment_status}
                                        </span>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                    </div>
                </div>
            )}
        </AppLayout>
    );
}
