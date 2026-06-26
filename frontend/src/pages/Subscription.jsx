import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Check, X, Sparkles, Loader2, Shield, Crown, Rocket } from "lucide-react";

const TIER_META = {
    starter: {
        label: "Starter", icon: Shield, ringHex: "#52525B",
        ringClass: "border-[#1F1F1F]",
        accent: "text-[#A1A1AA]",
        tagline: "Essentials. Manual trading + AI signal feed.",
    },
    pro: {
        label: "Pro", icon: Rocket, ringHex: "#00FF41",
        ringClass: "border-[#00FF41] shadow-[0_0_30px_rgba(0,255,65,0.18)]",
        accent: "text-[#00FF41]",
        tagline: "Auto-execute. Multi-account. Loss Lab + Auto-Heal.",
        recommended: true,
    },
    elite: {
        label: "Elite", icon: Crown, ringHex: "#A855F7",
        ringClass: "border-[#A855F7] shadow-[0_0_30px_rgba(168,85,247,0.22)]",
        accent: "text-[#A855F7]",
        tagline: "Unlimited accounts. Drift retraining. Paper Shadow.",
    },
};

// Marketing-friendly row order for the comparison table. Each row maps a
// human label to a `Features` key from backend/subscription_plans.py.
const FEATURE_ROWS = [
    { k: "max_accounts", label: "Connected accounts",
      render: (v) => v < 0 ? "Unlimited" : `${v}` },
    { k: "allowed_symbols", label: "Tradable symbols",
      render: (v) => Array.isArray(v) && v.includes("*") ? "All" : (v || []).join(", ") },
    { k: "auto_execute", label: "Auto-execute trades" },
    { k: "min_signal_cooldown_minutes", label: "Min signal cooldown",
      render: (v) => `${v} min` },
    { k: "loss_lab", label: "Loss Lab post-mortems" },
    { k: "auto_heal", label: "Auto-Heal scheduler" },
    { k: "correlation_kelly", label: "Correlation-aware sizing + CVaR" },
    { k: "calibrated_p_win", label: "Calibrated AI probabilities" },
    { k: "drift_auto_retrain", label: "ADWIN auto-retrain on drift" },
    { k: "paper_shadow_mode", label: "Paper Shadow Mode" },
    { k: "custom_thresholds", label: "Custom A+/entropy/sector caps" },
    { k: "weekly_digest_pdf", label: "Weekly AI Digest (PDF)" },
    { k: "api_access", label: "Programmatic API access" },
    { k: "priority_notifications", label: "Telegram + email + SMS" },
    { k: "support_tier", label: "Support",
      render: (v) => v.charAt(0).toUpperCase() + v.slice(1) },
];

const DURATIONS = [
    { id: "monthly", label: "Monthly", badge: "" },
    { id: "quarterly", label: "3-month", badge: "−10%" },
    { id: "semi_annual", label: "6-month", badge: "−20%" },
    { id: "annual", label: "Annual", badge: "BEST · −40%" },
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

    useEffect(() => {
        let cancel = false;
        (async () => {
            try {
                const [pRes, sRes, tRes] = await Promise.all([
                    api.get("/subscription/plans"),
                    api.get("/subscription/status"),
                    api.get("/entitlements/tiers"),
                ]);
                if (cancel) return;
                setPlans(pRes.data);
                setStatus(sRes.data);
                setTierMatrix(tRes.data);
            } catch (e) {
                setErr(formatApiError(e));
            } finally { if (!cancel) setLoading(false); }
        })();
        return () => { cancel = true; };
    }, []);

    // Bucket plans by tier for the duration selector to pick the active SKU.
    const plansByTier = useMemo(() => {
        const out = { starter: {}, pro: {}, elite: {} };
        for (const p of plans) {
            if (!out[p.tier]) continue;
            out[p.tier][p.duration_months === 1 ? "monthly"
                : p.duration_months === 3 ? "quarterly"
                : p.duration_months === 6 ? "semi_annual"
                : "annual"] = p;
        }
        return out;
    }, [plans]);

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
                    Active: <span className="font-mono">{status.entitlement.plan_id || "grandfathered"}</span>
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
                        {d.badge && <span className={`ml-2 ${duration === d.id ? "text-[#00FF41]" : "text-[#FFB000]"}`}>{d.badge}</span>}
                    </button>
                ))}
            </div>

            {/* Tier cards */}
            <div className="grid md:grid-cols-3 gap-4 mb-12">
                {["starter", "pro", "elite"].map(tierId => {
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
                                <span className="font-display text-4xl text-[#FAFAFA]">${plan.amount_usd}</span>
                                <span className="text-xs text-[#71717A] ml-2">/ {plan.duration_label}</span>
                            </div>
                            {plan.duration_months > 1 && (
                                <div className="text-xs text-[#A1A1AA] mb-4">
                                    ≈ ${plan.effective_monthly_usd}/mo
                                    {plan.savings_usd > 0 && (
                                        <span className="ml-2 text-[#00FF41]">save ${plan.savings_usd}</span>
                                    )}
                                </div>
                            )}
                            {plan.duration_months === 1 && <div className="h-4" />}

                            <button onClick={() => startCheckout(plan.id)}
                                disabled={creating === plan.id || isCurrent}
                                data-testid={`subscribe-${tierId}-${duration}`}
                                className={`w-full py-2 rounded text-xs font-mono uppercase transition mb-4 ${
                                    isCurrent ? "bg-[#0F0F0F] text-[#52525B] cursor-not-allowed"
                                    : tierId === "pro" ? "bg-[#00FF41] text-black hover:bg-[#33FF66]"
                                    : tierId === "elite" ? "bg-[#A855F7] text-white hover:bg-[#9333EA]"
                                    : "bg-[#FAFAFA] text-black hover:bg-white"
                                }`}>
                                {isCurrent ? "Current plan"
                                 : creating === plan.id ? <><Loader2 className="inline w-3 h-3 mr-2 animate-spin" />Redirecting…</>
                                 : `Subscribe — $${plan.amount_usd}`}
                            </button>

                            {/* Per-tier highlights */}
                            <div className="space-y-1.5 text-xs">
                                {(tierMatrix[tierId] ? FEATURE_ROWS.slice(0, 7) : []).map(row => {
                                    const v = tierMatrix[tierId][row.k];
                                    const isOn = v === true || (typeof v === "number" && v !== 0) || (Array.isArray(v) && v.length > 0);
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
                <table className="w-full text-xs">
                    <thead className="bg-[#0A0A0A] text-[#52525B]">
                        <tr>
                            <th className="text-left px-4 py-2 font-mono">Feature</th>
                            {["starter", "pro", "elite"].map(t => (
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
                                {["starter", "pro", "elite"].map(t => {
                                    const v = (tierMatrix[t] || {})[row.k];
                                    const isOn = v === true || (typeof v === "number" && v !== 0)
                                                  || (Array.isArray(v) && v.length > 0);
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

            <div className="text-xs text-[#52525B] mt-4">
                All plans are prepaid. No surprise renewals — your plan stays active until the
                <span className="text-[#A1A1AA]"> &ldquo;valid until&rdquo;</span> date and we&apos;ll email you before it lapses.
                Cancel any time = simply don&apos;t renew.
            </div>
        </AppLayout>
    );
}
