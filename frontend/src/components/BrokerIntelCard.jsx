import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Gauge, TrendingDown } from "lucide-react";

const COMP_LABELS = {
    spread: "SPREAD",
    slippage: "SLIPPAGE",
    fill_speed: "FILL SPEED",
    rejects: "REJECTS",
    freeze: "FREEZE LVL",
};

const scoreColor = (s) =>
    s == null ? "#52525B" : s >= 80 ? "#00FF41" : s >= 55 ? "#FFD700" : "#FF3B30";

export const BrokerIntelCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/broker-intel")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d || !d.brokers?.length) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="broker-intel-card">
            <div className="flex items-center gap-2 flex-wrap">
                <Gauge className="w-4 h-4 text-[#00FF41]" />
                <span className="font-display font-bold text-sm text-white">Live Broker Execution Score</span>
                {d.best_execution && (
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#00FF41]/40 text-[#00FF41]"
                        data-testid="broker-intel-best">
                        BEST: {d.best_execution.label?.toUpperCase()} · {d.best_execution.score}
                    </span>
                )}
            </div>
            <div className="mt-3 space-y-2">
                {d.brokers.map(b => (
                    <div key={b.account_id} className="border border-[#141414] bg-black/40 p-3"
                        data-testid={`broker-intel-${b.account_id}`}>
                        <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-mono text-xs text-white">{b.label || b.broker || b.account_id}</span>
                            <span className="font-mono text-lg font-bold" style={{ color: scoreColor(b.score) }}
                                data-testid={`broker-intel-score-${b.account_id}`}>
                                {b.score == null ? "—" : b.score}
                            </span>
                            {b.provisional && (
                                <span className="font-mono text-[9px] tracking-widest text-[#FFD700] border border-[#FFD700]/30 px-1.5 py-0.5">
                                    PROVISIONAL · {b.fills_measured} FILLS
                                </span>
                            )}
                            {b.timing?.delays > 0 && (
                                <span className="font-mono text-[9px] tracking-widest text-[#A1A1AA] ml-auto"
                                    title="Pre-send spread-spike delays and how often they improved the fill">
                                    <TrendingDown className="w-3 h-3 inline mr-1" />
                                    {b.timing.delays} TIMED · {Math.round((b.timing.improve_rate || 0) * 100)}% IMPROVED
                                </span>
                            )}
                        </div>
                        <div className="grid grid-cols-2 sm:grid-cols-5 gap-x-4 gap-y-1 mt-2">
                            {Object.entries(b.components).map(([k, c]) => (
                                <div key={k} className="min-w-0">
                                    <div className="flex items-center gap-1.5">
                                        <span className="font-mono text-[9px] tracking-widest text-[#52525B]">{COMP_LABELS[k]}</span>
                                        <span className="font-mono text-[10px]" style={{ color: scoreColor(c.score) }}>
                                            {c.score == null ? "—" : c.score}
                                        </span>
                                    </div>
                                    <div className="font-mono text-[9px] text-[#71717A] truncate" title={c.detail}>{c.detail}</div>
                                </div>
                            ))}
                        </div>
                    </div>
                ))}
            </div>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-3">
                SPREAD · SLIPPAGE · FILL SPEED · REJECTS · FREEZE LEVELS — SCORED CONTINUOUSLY FROM REAL EXECUTION EVIDENCE. DETERIORATION RAISES AN OPS ALERT.
            </div>
        </div>
    );
};
