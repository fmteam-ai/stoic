import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Check, Sparkles, Loader2, Shield, TrendingUp } from "lucide-react";

const PLAN_ACCENTS = {
    monthly: { ring: "border-[#1F1F1F]", badge: "" },
    quarterly: { ring: "border-[#FFB000]/40", badge: "SAVE 10%" },
    semi_annual: { ring: "border-[#00FF41]/40", badge: "SAVE 20%" },
    annual: { ring: "border-[#00FF41] shadow-[0_0_30px_rgba(0,255,65,0.25)]", badge: "BEST VALUE · SAVE 40%" },
};

export default function Subscription() {
    const navigate = useNavigate();
    const [plans, setPlans] = useState([]);
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(true);
    const [creating, setCreating] = useState(null); // plan_id currently being checked out
    const [err, setErr] = useState("");

    useEffect(() => {
        let cancel = false;
        (async () => {
            try {
                const [pRes, sRes] = await Promise.all([
                    api.get("/subscription/plans"),
                    api.get("/subscription/status"),
                ]);
                if (cancel) return;
                setPlans(pRes.data);
                setStatus(sRes.data);
            } catch (e) {
                setErr(formatApiError(e));
            } finally {
                if (!cancel) setLoading(false);
            }
        })();
        return () => { cancel = true; };
    }, []);

    const startCheckout = async (planId) => {
        setCreating(planId);
        setErr("");
        try {
            const { data } = await api.post("/subscription/checkout", {
                plan_id: planId,
                origin: window.location.origin,
            });
            window.location.href = data.checkout_url;
        } catch (e) {
            setErr(formatApiError(e));
            setCreating(null);
        }
    };

    const entitlement = status?.entitlement;
    const sub = status?.subscription;
    const validUntil = sub?.valid_until ? new Date(sub.valid_until) : null;
    const isGrandfather = sub?.current_plan_id === "admin_grandfather";

    return (
        <AppLayout>
            <PageHeader
                title="Subscription"
                subtitle="Unlock live MT5 execution. Paper trading is always free."
                testid="subscription-header"
            />

            <div className="p-4 md:p-8 space-y-8 max-w-6xl">
                {err && (
                    <div data-testid="subscription-error"
                        className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">
                        {err}
                    </div>
                )}

                {/* Current status banner */}
                {!loading && entitlement && (
                    <div
                        data-testid="subscription-banner"
                        className={`border p-4 flex items-center justify-between ${
                            entitlement.active
                                ? "border-[#00FF41]/40 bg-[#00FF41]/5"
                                : "border-[#FFB000]/40 bg-[#FFB000]/5"
                        }`}
                    >
                        <div className="flex items-center gap-3">
                            <Shield className={`w-5 h-5 ${entitlement.active ? "text-[#00FF41]" : "text-[#FFB000]"}`} />
                            <div>
                                <div className="font-mono text-[10px] tracking-widest mb-0.5 text-[#52525B]">CURRENT STATUS</div>
                                <div className="text-sm">
                                    {isGrandfather && "Grandfathered admin · full access forever"}
                                    {!isGrandfather && entitlement.active && entitlement.in_grace &&
                                        `Grace period — expires ${new Date(entitlement.grace_until).toLocaleDateString()}. Subscribe to continue.`}
                                    {!isGrandfather && entitlement.active && !entitlement.in_grace && validUntil &&
                                        `Active · ${sub.current_plan_id || "plan"} · renews ${validUntil.toLocaleDateString()}`}
                                    {!entitlement.active &&
                                        "No active subscription — live trading is paused. Paper trading still works."}
                                </div>
                            </div>
                        </div>
                        {!isGrandfather && validUntil && (
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                {Math.max(0, Math.ceil((validUntil - new Date()) / (24 * 3600 * 1000)))} DAYS LEFT
                            </div>
                        )}
                    </div>
                )}

                {/* Plans grid */}
                {loading ? (
                    <div className="flex items-center justify-center py-12 text-[#52525B]">
                        <Loader2 className="w-5 h-5 animate-spin" />
                    </div>
                ) : (
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4" data-testid="plans-grid">
                        {plans.map((p) => {
                            const accent = PLAN_ACCENTS[p.id] || PLAN_ACCENTS.monthly;
                            return (
                                <div
                                    key={p.id}
                                    data-testid={`plan-${p.id}`}
                                    className={`relative bg-[#0A0A0A] border ${accent.ring} p-6 flex flex-col`}
                                >
                                    {accent.badge && (
                                        <div className="absolute -top-3 left-4 px-2 py-0.5 bg-[#00FF41] text-black text-[10px] font-mono tracking-widest">
                                            {accent.badge}
                                        </div>
                                    )}
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                                        {p.label.toUpperCase()}
                                    </div>
                                    <div className="flex items-baseline gap-1 mb-1">
                                        <span className="font-display font-bold text-4xl tracking-tight">
                                            ${p.effective_monthly_usd.toFixed(2)}
                                        </span>
                                        <span className="text-xs text-[#A1A1AA] font-mono">/mo</span>
                                    </div>
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-4">
                                        ${p.amount_usd.toFixed(2)} billed every {p.duration_months}
                                        {p.duration_months === 1 ? " month" : " months"}
                                    </div>
                                    {p.savings_usd > 0 && (
                                        <div className="font-mono text-[10px] text-[#00FF41] tracking-widest mb-4">
                                            SAVE ${p.savings_usd.toFixed(2)}
                                        </div>
                                    )}
                                    <p className="text-xs text-[#A1A1AA] leading-relaxed mb-4 flex-1">
                                        {p.description}
                                    </p>
                                    <ul className="text-xs text-[#A1A1AA] space-y-1.5 mb-5">
                                        <Feature>Live MT5 execution</Feature>
                                        <Feature>Dual-AI signals (Claude 4.5)</Feature>
                                        <Feature>All 4 risk profiles</Feature>
                                        <Feature>NL Risk Commander</Feature>
                                        <Feature>AI Co-Pilot</Feature>
                                        {p.id === "annual" && <Feature>Priority support</Feature>}
                                    </ul>
                                    <button
                                        onClick={() => startCheckout(p.id)}
                                        disabled={creating !== null}
                                        data-testid={`subscribe-${p.id}`}
                                        className={`w-full py-2.5 text-xs font-mono tracking-widest flex items-center justify-center gap-2 transition-colors ${
                                            p.id === "annual"
                                                ? "bg-[#00FF41] text-black hover:bg-[#00E53A]"
                                                : "bg-[#121212] border border-[#1F1F1F] hover:border-[#00FF41] hover:text-[#00FF41]"
                                        } disabled:opacity-40`}
                                    >
                                        {creating === p.id ? (
                                            <Loader2 className="w-4 h-4 animate-spin" />
                                        ) : (
                                            <Sparkles className="w-3.5 h-3.5" />
                                        )}
                                        SUBSCRIBE
                                    </button>
                                </div>
                            );
                        })}
                    </div>
                )}

                {/* Trust footer */}
                <div className="border-t border-[#1F1F1F] pt-6 flex flex-wrap items-center gap-6 text-xs text-[#52525B] font-mono">
                    <span className="flex items-center gap-2">
                        <Shield className="w-3.5 h-3.5" /> Secure checkout · Stripe
                    </span>
                    <span className="flex items-center gap-2">
                        <TrendingUp className="w-3.5 h-3.5" /> No auto-renew — you control renewals
                    </span>
                    <span className="tracking-widest">PAPER TRADING ALWAYS FREE</span>
                </div>
            </div>
        </AppLayout>
    );
}

function Feature({ children }) {
    return (
        <li className="flex items-start gap-2">
            <Check className="w-3.5 h-3.5 text-[#00FF41] shrink-0 mt-0.5" />
            <span>{children}</span>
        </li>
    );
}
