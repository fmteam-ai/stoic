import { useEffect, useState } from "react";
import api from "@/lib/api";
import { GitBranch } from "lucide-react";

const CAT_META = {
    normal_statistical_loss: { label: "NORMAL STATISTICAL LOSS", color: "#A1A1AA" },
    signal_failure: { label: "SIGNAL FAILURE", color: "#FFB000" },
    regime_failure: { label: "REGIME FAILURE", color: "#A855F7" },
    execution_failure: { label: "EXECUTION FAILURE", color: "#F97316" },
    risk_failure: { label: "RISK FAILURE", color: "#FF3B30" },
    data_failure: { label: "DATA FAILURE", color: "#38BDF8" },
    operational_failure: { label: "OPERATIONAL FAILURE", color: "#FF8C00" },
};

export const FailureTaxonomyCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/learning/failure-summary?days=30")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="failure-taxonomy-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <GitBranch className="w-4 h-4 text-[#F97316]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">FAILURE TAXONOMY · LAST {d.window_days}D</div>
                        <div className="font-display font-bold text-base tracking-tight">Which component owns the losses</div>
                    </div>
                </div>
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]" data-testid="failure-classified-count">
                    {d.classified_losses} CLASSIFIED
                </span>
            </div>
            {d.categories.length === 0 ? (
                <div className="p-6 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="failure-empty">
                    NO CLASSIFIED LOSSES YET — RECORDS BUILD AS THE BOT TRADES
                </div>
            ) : (
                <div className="divide-y divide-[#141414]">
                    {d.categories.map(c => {
                        const meta = CAT_META[c.category] || { label: c.category, color: "#52525B" };
                        return (
                            <div key={c.category} className="px-4 py-3" data-testid={`failure-cat-${c.category}`}>
                                <div className="flex items-center gap-2 flex-wrap">
                                    <span className="font-mono text-[10px] tracking-widest" style={{ color: meta.color }}>
                                        {meta.label}
                                    </span>
                                    <span className="font-mono text-xs text-white tabular-nums">{c.count}× · {c.share_pct}%</span>
                                    <span className={`font-mono text-xs tabular-nums ml-auto ${c.pnl < 0 ? "text-[#FF3B30]" : "text-[#00FF41]"}`}>
                                        {c.pnl < 0 ? "-" : "+"}${Math.abs(c.pnl).toFixed(0)}
                                    </span>
                                </div>
                                <div className="h-1 bg-[#141414] mt-1.5">
                                    <div className="h-full" style={{ width: `${c.share_pct}%`, background: meta.color }} />
                                </div>
                                <div className="font-mono text-[9px] text-[#71717A] mt-1.5">
                                    FIX → {c.route_fix_to.toUpperCase()}
                                </div>
                                {c.examples?.[0] && (
                                    <div className="text-[10px] text-[#52525B] mt-0.5 truncate" title={c.examples.join(" · ")}>
                                        e.g. {c.examples[0]}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
            <div className="px-4 py-2 font-mono text-[9px] tracking-widest text-[#52525B] border-t border-[#141414]">
                {d.principle?.toUpperCase()}
            </div>
        </div>
    );
};
