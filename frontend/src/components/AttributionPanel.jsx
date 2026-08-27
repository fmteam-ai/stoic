import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { PieChart } from "lucide-react";

const CAT_LABEL = {
    ALPHA_ERROR: "SIGNAL WRONG",
    REGIME_ERROR: "REGIME SHIFT",
    TIMING_ERROR: "LATE FILLS",
    SIZING_ERROR: "SIZING",
    EXECUTION_ERROR: "EXECUTION",
    BROKER_ERROR: "BROKER",
    INFRASTRUCTURE_ERROR: "INFRASTRUCTURE",
    NEWS_SHOCK: "NEWS SHOCK",
    CORRELATION_ERROR: "CORRELATION",
    NORMAL_VARIANCE: "NORMAL VARIANCE",
    UNEXPLAINED: "UNEXPLAINED",
};
const CAT_COLOR = {
    ALPHA_ERROR: "#FF3B30", REGIME_ERROR: "#FF8C00",
    TIMING_ERROR: "#FFB000", SIZING_ERROR: "#FFB000",
    EXECUTION_ERROR: "#0099FF", BROKER_ERROR: "#A855F7",
    INFRASTRUCTURE_ERROR: "#EC4899", NEWS_SHOCK: "#F59E0B",
    CORRELATION_ERROR: "#22D3EE", NORMAL_VARIANCE: "#52525B",
    UNEXPLAINED: "#6B7280",
};

export function AttributionPanel() {
    const [data, setData] = useState(null);
    const [days, setDays] = useState(30);
    const load = useCallback(async () => {
        try {
            const r = await api.get(`/attribution/summary?days=${days}`);
            setData(r.data);
        } catch { /* non-fatal */ }
    }, [days]);
    useEffect(() => { load(); }, [load]);
    if (!data) return null;
    const cats = Object.entries(data.categories || {})
        .map(([k, v]) => ({ key: k, ...v }))
        .sort((a, b) => a.loss_r - b.loss_r);
    const maxLoss = Math.max(0.01, ...cats.map(c => Math.abs(c.loss_r)));
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="attribution-panel">
            <div className="px-4 py-3 border-b border-[#141414] flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <PieChart className="w-4 h-4 text-[#0099FF]" />
                    <div className="font-display font-bold text-sm">Outcome Attribution</div>
                    <span className="font-mono text-[10px] text-[#52525B]">WHY results happened — not just that they did</span>
                </div>
                <div className="flex items-center gap-1">
                    {[7, 30, 90].map(d => (
                        <button key={d} onClick={() => setDays(d)} data-testid={`attribution-days-${d}`}
                            className={`font-mono text-[10px] px-2 py-0.5 border ${days === d ? "border-[#0099FF] text-[#0099FF]" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>
                            {d}D
                        </button>
                    ))}
                </div>
            </div>
            <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-5 gap-3 border-b border-[#141414]">
                <div><div className="font-mono text-[9px] text-[#52525B]">ATTRIBUTED TRADES</div>
                    <div className="font-mono text-sm text-white" data-testid="attribution-total">{data.total}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">WINS / LOSSES</div>
                    <div className="font-mono text-sm text-white">{data.wins} / {data.losses}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">ALPHA-CLEAN</div>
                    <div className="font-mono text-sm text-[#00FF41]" data-testid="attribution-alpha-clean">{data.alpha_clean}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">AVG CONFIDENCE</div>
                    <div className="font-mono text-sm text-[#0099FF]" data-testid="attribution-confidence">
                        {data.avg_confidence != null ? data.avg_confidence : "—"}
                        {data.avg_unexplained != null && (
                            <span className="text-[9px] text-[#52525B]"> ({Math.round((data.avg_unexplained || 0) * 100)}% unexpl.)</span>
                        )}
                    </div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">BIGGEST DRAG</div>
                    <div className="font-mono text-sm text-[#FF3B30]" data-testid="attribution-worst">
                        {data.worst_category ? CAT_LABEL[data.worst_category] || data.worst_category : "—"}
                    </div></div>
            </div>
            {data.total === 0 ? (
                <div className="px-4 py-6 text-center font-mono text-xs text-[#52525B]" data-testid="attribution-empty">
                    No attributed outcomes yet — every trade that closes from now on is decomposed automatically.
                </div>
            ) : (
                <div className="px-4 py-3 space-y-1.5">
                    {cats.map(c => (
                        <div key={c.key} className="flex items-center gap-2" data-testid={`attribution-cat-${c.key}`}>
                            <div className="font-mono text-[9px] text-[#A1A1AA] w-32 shrink-0">{CAT_LABEL[c.key] || c.key}</div>
                            <div className="flex-1 h-2 bg-[#141414]">
                                <div className="h-2" style={{
                                    width: `${Math.min(100, (Math.abs(c.loss_r) / maxLoss) * 100)}%`,
                                    backgroundColor: CAT_COLOR[c.key] || "#52525B",
                                }} />
                            </div>
                            <div className="font-mono text-[10px] w-20 text-right"
                                style={{ color: c.loss_r < 0 ? "#FF3B30" : "#52525B" }}>
                                {c.loss_r < 0 ? `${c.loss_r}R lost` : "—"}
                            </div>
                            <div className="font-mono text-[9px] text-[#52525B] w-16 text-right">{c.trades} trades</div>
                        </div>
                    ))}
                </div>
            )}
            {data.lesson && (
                <div className="px-4 pb-3 font-mono text-[10px] text-[#FFB000]" data-testid="attribution-lesson">{data.lesson}</div>
            )}
        </div>
    );
}
