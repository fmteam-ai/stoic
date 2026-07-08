import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { ClipboardList, RefreshCw } from "lucide-react";

const VERDICT_STYLE = {
    "KEEP ENFORCE":    "text-[#00FF41] border-[#00FF41]/40 bg-[#00FF41]/10",
    "CONSIDER ADVISE": "text-[#FFB000] border-[#FFB000]/40 bg-[#FFB000]/10",
    "MONITORING":      "text-[#0099FF] border-[#0099FF]/40 bg-[#0099FF]/10",
    "QUIET":           "text-[#52525B] border-[#1F1F1F] bg-transparent",
};

function money(v) {
    if (!v) return "—";
    const s = v > 0 ? "+" : "−";
    return `${s}$${Math.abs(v).toFixed(2)}`;
}

// Weekly Agent Report Card — per-agent gate activity + estimated P&L impact
// so users can make data-driven enforce/advise decisions.
export default function AgentReportCard() {
    const [card, setCard] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async (force = false) => {
        setBusy(true);
        try {
            const r = await api.get(`/agents/report-card${force ? "?force=true" : ""}`);
            setCard(r.data);
        } catch { /* silent */ } finally { setBusy(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    if (!card) return null;
    const active = (card.agents || []).filter((a) => a.hard_blocks + a.soft_flags > 0);
    const quiet = (card.agents || []).length - active.length;
    const b = card.baseline || {};

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="agent-report-card">
            <div className="flex items-center justify-between px-5 py-3 border-b border-[#1F1F1F]">
                <div className="flex items-center gap-2">
                    <ClipboardList className="w-4 h-4 text-[#00FF41]" />
                    <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">WEEKLY AGENT REPORT CARD · LAST 7 DAYS</span>
                </div>
                <button onClick={() => load(true)} disabled={busy} data-testid="report-card-refresh"
                    className="flex items-center gap-1.5 font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-[#00FF41] transition-colors disabled:opacity-50">
                    <RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} /> REBUILD
                </button>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-[#1F1F1F] border-b border-[#1F1F1F]" data-testid="report-card-baseline">
                <div className="px-5 py-3">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B]">WIN RATE ({b.window_days}D)</div>
                    <div className="font-mono text-sm text-[#E4E4E7]">{b.win_rate}% <span className="text-[#52525B] text-[10px]">of {b.closed_trades}</span></div>
                </div>
                <div className="px-5 py-3">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B]">AVG WIN / LOSS</div>
                    <div className="font-mono text-sm"><span className="text-[#00FF41]">${b.avg_win}</span> <span className="text-[#52525B]">/</span> <span className="text-[#FF3B30]">${b.avg_loss}</span></div>
                </div>
                <div className="px-5 py-3">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B]">EV / TRADE</div>
                    <div className={`font-mono text-sm ${b.ev_per_trade >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{money(b.ev_per_trade)}</div>
                </div>
                <div className="px-5 py-3">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B]">EST. TOTAL IMPACT</div>
                    <div className={`font-mono text-sm ${card.totals?.est_pnl_impact >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                         data-testid="report-card-total-impact">{money(card.totals?.est_pnl_impact)}</div>
                </div>
            </div>

            {active.length === 0 ? (
                <div className="px-5 py-6 text-center font-mono text-xs text-[#52525B]" data-testid="report-card-empty">
                    No gate activity in the last 7 days — all agents quiet.
                </div>
            ) : (
                <div className="divide-y divide-[#1F1F1F]">
                    <div className="hidden md:grid grid-cols-12 px-5 py-2 font-mono text-[9px] tracking-widest text-[#52525B]">
                        <span className="col-span-4">AGENT</span>
                        <span className="col-span-2 text-right">HARD BLOCKS</span>
                        <span className="col-span-2 text-right">SOFT FLAGS</span>
                        <span className="col-span-2 text-right">EST. IMPACT</span>
                        <span className="col-span-2 text-right">VERDICT</span>
                    </div>
                    {active.map((a) => (
                        <div key={a.slug} className="grid grid-cols-2 md:grid-cols-12 gap-y-1 px-5 py-2.5 items-center"
                             data-testid={`report-row-${a.slug}`}>
                            <span className="col-span-2 md:col-span-4 text-xs text-[#E4E4E7]">{a.name}</span>
                            <span className="md:col-span-2 md:text-right font-mono text-xs text-[#FFB000]">{a.hard_blocks || "—"}</span>
                            <span className="md:col-span-2 md:text-right font-mono text-xs text-[#A1A1AA]">{a.soft_flags || "—"}</span>
                            <span className={`md:col-span-2 md:text-right font-mono text-xs ${a.est_pnl_impact >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {money(a.est_pnl_impact)}
                            </span>
                            <span className="md:col-span-2 md:text-right">
                                <span className={`inline-block px-2 py-0.5 border font-mono text-[9px] tracking-widest ${VERDICT_STYLE[a.verdict] || VERDICT_STYLE.QUIET}`}>
                                    {a.verdict}
                                </span>
                            </span>
                        </div>
                    ))}
                </div>
            )}

            <div className="px-5 py-2.5 border-t border-[#1F1F1F] flex items-center justify-between gap-3 flex-wrap">
                <span className="font-mono text-[9px] text-[#52525B] leading-relaxed max-w-3xl">{card.method}</span>
                {quiet > 0 && <span className="font-mono text-[9px] text-[#52525B]">{quiet} quiet agents hidden</span>}
            </div>
        </div>
    );
}
