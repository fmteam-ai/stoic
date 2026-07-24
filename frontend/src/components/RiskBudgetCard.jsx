import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { PieChart } from "lucide-react";

const LABELS = {
    trend: "TREND", scalp: "SCALP", breakout: "BREAKOUT",
    mean_reversion: "MEAN REVERSION", experimental: "EXPERIMENTAL",
};

export const RiskBudgetCard = ({ accountId }) => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get(`/risk/budget${accountId ? `?account_id=${accountId}` : ""}`);
            setD(data);
        } catch { setHidden(true); }
    }, [accountId]);

    useEffect(() => {
        load();
        const id = setInterval(load, 60000);
        return () => clearInterval(id);
    }, [load]);

    if (hidden || !d) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid="risk-budget-card">
            <div className="flex items-center gap-2 flex-wrap">
                <PieChart className="w-4 h-4 text-[#00FF41]" />
                <span className="font-display font-bold text-sm text-white">Daily Risk Budget</span>
                <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]"
                    data-testid="risk-budget-pool">
                    POOL {d.pool_risk_pct}% / DAY · RESETS 00:00 UTC
                </span>
            </div>
            <div className="mt-4 space-y-2.5">
                {d.strategies.map(s => {
                    const used = s.budget_risk_pct > 0
                        ? Math.min(100, (s.spent_risk_pct / s.budget_risk_pct) * 100) : 0;
                    const shrunk = s.perf_weight < 1;
                    return (
                        <div key={s.strategy} data-testid={`risk-budget-${s.strategy}`}>
                            <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                                <span className="text-white">
                                    {LABELS[s.strategy]} · {s.allocation_pct}%
                                    {shrunk && (
                                        <span className="text-[#FFD700] ml-2"
                                            title="Allocation automatically shrunk on recent performance">
                                            ×{s.perf_weight} PERF
                                        </span>
                                    )}
                                </span>
                                <span className="text-[#A1A1AA]">
                                    {s.spent_risk_pct.toFixed(2)} / {s.budget_risk_pct.toFixed(2)}% RISK
                                </span>
                            </div>
                            <div className="h-1.5 bg-[#141414] mt-1">
                                <div className={`h-full ${used >= 100 ? "bg-[#FF3B30]" : used >= 75 ? "bg-[#FFD700]" : "bg-[#00FF41]"}`}
                                    style={{ width: `${used}%` }} />
                            </div>
                        </div>
                    );
                })}
            </div>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-4">
                UNDERPERFORMING STRATEGIES ARE SHRUNK AUTOMATICALLY — NEVER DISABLED
            </div>
        </div>
    );
};
