import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Gauge, ShieldAlert, ShieldCheck, Zap } from "lucide-react";

/* Per-account P&L vs Circuit-Breaker limit gauge.
   Visualizes how close each account is to its daily / weekly drawdown trip.
   Polls /api/bot/risk-gauge every 30s. */

function gaugeColor(pct, enabled) {
    if (!enabled) return "bg-[#1F1F1F]";
    if (pct >= 80) return "bg-[#FF3B30]";   // ⚠ critical
    if (pct >= 50) return "bg-[#FFB000]";   // approaching
    if (pct >= 20) return "bg-[#FFD700]";   // some pressure
    return "bg-[#00FF41]";                  // healthy
}

function GaugeRow({ label, kind, gauge, equity }) {
    const pct = gauge.enabled ? Math.min(100, Math.max(0, gauge.consumed_pct || 0)) : 0;
    const limitAmount = gauge.limit_amount || 0;
    const pnl = gauge.pnl || 0;
    return (
        <div className="space-y-1.5" data-testid={`gauge-${kind}`}>
            <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                <span className="text-[#52525B]">
                    {label}
                    {!gauge.enabled && <span className="ml-2 text-[#52525B]/60">· disabled</span>}
                </span>
                <span className={
                    !gauge.enabled ? "text-[#52525B]" :
                    pct >= 80 ? "text-[#FF3B30] font-bold" :
                    pct >= 50 ? "text-[#FFB000]" :
                                "text-[#A1A1AA]"
                }>
                    {pct.toFixed(0)}% of limit
                </span>
            </div>
            <div className="relative h-2 bg-[#1F1F1F] overflow-hidden">
                <div
                    className={`absolute inset-y-0 left-0 transition-all duration-700 ${gaugeColor(pct, gauge.enabled)}`}
                    style={{ width: `${pct}%` }}
                />
                {/* 80% danger marker */}
                <div className="absolute inset-y-0 w-px bg-[#FF3B30]/40" style={{ left: "80%" }} />
            </div>
            <div className="flex items-center justify-between font-mono text-[9px] text-[#52525B]">
                <span>
                    {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} P&L
                </span>
                <span>
                    cap ${limitAmount.toFixed(2)} ({gauge.limit_pct}% of ${equity.toLocaleString()})
                </span>
            </div>
        </div>
    );
}

function AccountGaugeCard({ a }) {
    const daily = a.daily || {};
    const weekly = a.weekly || {};
    const dailyHot = daily.enabled && (daily.consumed_pct || 0) >= 50;
    const weeklyHot = weekly.enabled && (weekly.consumed_pct || 0) >= 50;
    const hot = dailyHot || weeklyHot || a.tripped;

    return (
        <div className={`border bg-[#0A0A0A] p-4 space-y-4 transition-colors ${
            a.tripped ? "border-[#FF3B30]/60" :
            hot ? "border-[#FFB000]/40" :
                  "border-[#1F1F1F]"
        }`} data-testid={`risk-card-${a.account_id || 'default'}`}>
            {/* Header */}
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">
                        {a.broker?.toUpperCase()}
                    </div>
                    <div className="font-mono font-medium text-sm tracking-tight mt-0.5 truncate">
                        {a.label}
                    </div>
                </div>
                <div className="text-right shrink-0">
                    {a.tripped ? (
                        <span className="font-mono text-[10px] text-[#FF3B30] tracking-widest flex items-center gap-1">
                            <ShieldAlert className="w-3 h-3" /> TRIPPED
                        </span>
                    ) : hot ? (
                        <span className="font-mono text-[10px] text-[#FFB000] tracking-widest flex items-center gap-1">
                            <Zap className="w-3 h-3" /> APPROACHING
                        </span>
                    ) : (
                        <span className="font-mono text-[10px] text-[#00FF41] tracking-widest flex items-center gap-1">
                            <ShieldCheck className="w-3 h-3" /> HEALTHY
                        </span>
                    )}
                </div>
            </div>

            {a.tripped && a.tripped_reason && (
                <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 font-mono text-[11px] text-[#FF3B30]"
                     data-testid={`risk-tripped-${a.account_id || 'default'}`}>
                    {a.tripped_reason}
                </div>
            )}

            <GaugeRow label="DAILY DRAWDOWN" kind="daily" gauge={daily} equity={a.equity} />
            <GaugeRow label="WEEKLY DRAWDOWN" kind="weekly" gauge={weekly} equity={a.equity} />

            {/* Daily profit target — upside gauge (iter-65) */}
            {a.profit_target?.enabled && (
                <ProfitTargetRow pt={a.profit_target} />
            )}

            <div className="pt-2 border-t border-[#1F1F1F] flex items-center justify-between
                            font-mono text-[10px] text-[#52525B] tracking-widest">
                <span>EQUITY</span>
                <span>${a.equity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
            </div>
        </div>
    );
}

function ProfitTargetRow({ pt }) {
    const pct = pt.progress_pct || 0;
    const hit = pt.hit;
    return (
        <div className="space-y-1.5" data-testid="gauge-profit-target">
            <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                <span className="text-[#00FF41]">
                    DAILY PROFIT TARGET · {pt.target_r}R
                    <span className="ml-2 text-[#52525B]/80 normal-case">
                        ({pt.mode === "stop" ? "stop-on-hit" : "lock-on-hit"})
                    </span>
                </span>
                <span className={hit ? "text-[#00FF41] font-bold" : "text-[#A1A1AA]"}>
                    {pct}% {hit && "· HIT ✓"}
                </span>
            </div>
            <div className="relative h-2 bg-[#1F1F1F] overflow-hidden">
                <div
                    className={`absolute inset-y-0 left-0 transition-all duration-700 ${
                        hit ? "bg-[#00FF41]" : pct >= 50 ? "bg-[#FFD700]" : "bg-[#00FF41]/60"
                    }`}
                    style={{ width: `${pct}%` }}
                />
            </div>
            <div className="flex items-center justify-between font-mono text-[9px] text-[#52525B]">
                <span>
                    +${pt.current_pnl.toFixed(2)} earned today
                </span>
                <span>
                    target ${pt.target_amount.toFixed(2)} (1R = ${pt.r_dollar_value.toFixed(2)})
                </span>
            </div>
            {pt.locked_amount > 0 && (
                <div className="font-mono text-[10px] text-[#00FF41] flex items-center gap-1.5 mt-1"
                     data-testid="profit-target-locked">
                    🔒 ${pt.locked_amount.toFixed(2)} locked — subsequent trades size against reduced equity
                </div>
            )}
        </div>
    );
}

export default function RiskGaugePanel() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);

    const fetchGauge = useCallback(async () => {
        try {
            const res = await api.get("/bot/risk-gauge");
            setData(res.data);
            setErr(null);
        } catch (e) {
            setErr(e?.response?.data?.detail || e.message || "Failed to load risk gauge");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchGauge();
        const id = setInterval(fetchGauge, 30_000);
        return () => clearInterval(id);
    }, [fetchGauge]);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6" data-testid="risk-gauge-loading">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest animate-pulse">
                    LOADING RISK GAUGE…
                </div>
            </div>
        );
    }
    if (err || !data?.items?.length) return null;

    // Hide the legacy default-profile row when account-scoped configs exist.
    const hasAccountScoped = data.items.some(x => x.account_id);
    const items = data.items.filter(x => {
        if (hasAccountScoped && !x.account_id) return false;
        return x.equity > 0 || x.tripped;
    });
    if (!items.length) return null;

    const anyHot = items.some(a => a.tripped ||
        (a.daily?.enabled && (a.daily.consumed_pct || 0) >= 50) ||
        (a.weekly?.enabled && (a.weekly.consumed_pct || 0) >= 50));

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="risk-gauge-panel">
            <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center gap-3">
                <Gauge className={`w-4 h-4 ${anyHot ? "text-[#FFB000]" : "text-[#00FF41]"}`} />
                <div>
                    <div className="font-mono text-xs tracking-widest">P&amp;L vs CIRCUIT-BREAKER LIMITS</div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                        {anyHot
                            ? "One or more accounts approaching breaker — auto-halt at 100%"
                            : "All accounts healthy · breakers armed but quiet"}
                    </div>
                </div>
            </div>
            <div className={`p-5 grid gap-4 ${items.length > 1 ? "md:grid-cols-2" : ""}`}>
                {items.map(a => (
                    <AccountGaugeCard key={a.config_id} a={a} />
                ))}
            </div>
        </div>
    );
}
