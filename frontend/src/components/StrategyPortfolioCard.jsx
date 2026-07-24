import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Layers } from "lucide-react";

const NAMES = {
    trend: "TREND", scalp: "SCALP", breakout: "BREAKOUT",
    mean_reversion: "MEAN REV", experimental: "EXPERIMENTAL",
};

const posNeg = (v) => (v > 0 ? "#00FF41" : v < 0 ? "#FF3B30" : "#71717A");

export const StrategyPortfolioCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/risk/strategy-portfolio")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d) return null;
    const dyn = d.dynamic_allocation;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="strategy-portfolio-card">
            <div className="flex items-center gap-2 flex-wrap">
                <Layers className="w-4 h-4 text-[#FFD700]" />
                <span className="font-display font-bold text-sm text-white">Multi-Strategy Portfolio</span>
                <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${dyn ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#52525B]/40 text-[#A1A1AA]"}`}
                    data-testid="strategy-portfolio-basis">
                    {dyn ? "DYNAMIC ALLOCATION" : "STATIC — GATHERING EVIDENCE"}
                </span>
                <span className="font-mono text-[10px] text-[#52525B] ml-auto">{d.window_days}D · {d.total_trades} TRADES</span>
            </div>
            <div className="mt-3 overflow-x-auto">
                <table className="w-full text-left">
                    <thead>
                        <tr className="font-mono text-[9px] tracking-widest text-[#52525B]">
                            <th className="pb-1 pr-3">STRATEGY</th>
                            <th className="pb-1 pr-3">E[R]/DAY</th>
                            <th className="pb-1 pr-3">VOL/DAY</th>
                            <th className="pb-1 pr-3">MAX DD</th>
                            <th className="pb-1 pr-3">CORR</th>
                            <th className="pb-1 pr-3">CAPACITY</th>
                            <th className="pb-1 pr-3">CONF</th>
                            <th className="pb-1">ALLOC</th>
                        </tr>
                    </thead>
                    <tbody className="font-mono text-[11px]">
                        {Object.entries(d.strategies).map(([k, m]) => {
                            const w = dyn ? dyn[k] : d.base_allocation[k];
                            return (
                                <tr key={k} className="border-t border-[#141414]" data-testid={`strategy-row-${k}`}>
                                    <td className="py-1.5 pr-3 text-white">{NAMES[k] || k.toUpperCase()}
                                        <span className="text-[#52525B] ml-1">×{m.n_trades}</span></td>
                                    <td className="py-1.5 pr-3" style={{ color: posNeg(m.expected_return_usd_day) }}>
                                        ${m.expected_return_usd_day}</td>
                                    <td className="py-1.5 pr-3 text-[#A1A1AA]">${m.volatility_usd_day}</td>
                                    <td className="py-1.5 pr-3" style={{ color: m.max_drawdown_usd > 0 ? "#FF8C00" : "#71717A" }}>
                                        {m.max_drawdown_usd > 0 ? `-$${m.max_drawdown_usd}` : "$0"}</td>
                                    <td className="py-1.5 pr-3 text-[#A1A1AA]">{m.avg_pos_correlation}</td>
                                    <td className="py-1.5 pr-3 text-[#A1A1AA]" title={m.capacity.detail}>{m.capacity.score}</td>
                                    <td className="py-1.5 pr-3 text-[#A1A1AA]" title={m.confidence.detail}>{m.confidence.score}</td>
                                    <td className="py-1.5 font-bold" style={{ color: dyn ? "#00FF41" : "#A1A1AA" }}
                                        data-testid={`strategy-alloc-${k}`}>
                                        {(w * 100).toFixed(1)}%</td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {Object.keys(d.by_asset || {}).length > 0 && (
                <div className="flex gap-4 flex-wrap mt-3 pt-2 border-t border-[#141414]">
                    {Object.entries(d.by_asset).map(([a, v]) => (
                        <span key={a} className="font-mono text-[10px] text-[#71717A]" data-testid={`asset-${a}`}>
                            {a.toUpperCase()} <span style={{ color: posNeg(v.pnl) }}>${v.pnl}</span>
                            <span className="text-[#52525B]"> ·{v.trades}t</span>
                        </span>
                    ))}
                </div>
            )}
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-2">
                E[R] · VOLATILITY · DRAWDOWN · CORRELATION · CAPACITY · CONFIDENCE → THE DAILY RISK POOL FOLLOWS THE EVIDENCE, NOT A FIXED SPLIT.
            </div>
        </div>
    );
};
