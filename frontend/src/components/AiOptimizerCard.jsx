/* AiOptimizerCard — Dashboard summary card for the AI Strategy Optimizer.

   Shows the latest AI trade-review per account scope: verdict, win rate,
   and how many suggestions are waiting. Deep-links to Bot Config
   (?account=<id>&optimizer=1) where the user can review & apply. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import api from "@/lib/api";
import { BrainCircuit, ArrowRight } from "lucide-react";

const VERDICT_CLS = {
    healthy: "text-[#00FF41] border-[#00FF41]/40",
    needs_tuning: "text-[#FFD700] border-[#FFD700]/40",
    underperforming: "text-[#FF6B00] border-[#FF6B00]/40",
    critical: "text-[#FF3B30] border-[#FF3B30]/40",
    insufficient_data: "text-[#52525B] border-[#1F1F1F]",
    unavailable: "text-[#52525B] border-[#1F1F1F]",
};

export default function AiOptimizerCard() {
    const [summary, setSummary] = useState(null);

    useEffect(() => {
        let alive = true;
        api.get("/optimizer/summary")
            .then(({ data }) => { if (alive) setSummary(data); })
            .catch(() => { if (alive) setSummary({ reports: [], total_pending: 0 }); });
        return () => { alive = false; };
    }, []);

    const reports = summary?.reports || [];

    return (
        <div className="border border-[#10F2C5]/30 bg-[#0A0A0A]" data-testid="ai-optimizer-card">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                    <BrainCircuit className="w-4 h-4 text-[#10F2C5]" />
                    <span className="font-mono text-[10px] text-[#10F2C5] tracking-widest">AI STRATEGY OPTIMIZER</span>
                </div>
                {summary?.total_pending > 0 && (
                    <span className="px-2 py-0.5 border border-[#10F2C5]/40 font-mono text-[10px] text-[#10F2C5] tracking-widest"
                        data-testid="optimizer-pending-badge">
                        {summary.total_pending} SUGGESTION{summary.total_pending > 1 ? "S" : ""}
                    </span>
                )}
            </div>
            <div className="p-4">
                {!summary ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                ) : reports.length === 0 ? (
                    <div className="text-xs text-[#A1A1AA] leading-relaxed" data-testid="optimizer-card-empty">
                        No AI trade reviews yet. Run one from{" "}
                        <Link to="/bot-config?optimizer=1" className="text-[#10F2C5] hover:underline">Bot Config</Link>{" "}
                        — the bot analyzes your last 24–48h of trades and suggests tuning to raise the win rate.
                        Auto-reviews run every 24h while the bot is active.
                    </div>
                ) : (
                    <div className="space-y-2">
                        {reports.slice(0, 6).map(r => (
                            <Link key={r.report_id}
                                to={`/bot-config?account=${r.account_id || "default"}&optimizer=1`}
                                data-testid={`optimizer-card-row-${r.account_id || "default"}`}
                                className="flex items-center justify-between gap-2 border border-[#1F1F1F] hover:border-[#333333] bg-black/40 px-3 py-2 transition-colors group">
                                <div className="min-w-0">
                                    <div className="font-mono text-xs text-white truncate">{r.account_label}</div>
                                    <div className="font-mono text-[10px] text-[#52525B] mt-0.5">
                                        {r.total_trades ?? 0} trades · WR {r.win_rate ?? "—"}%
                                        {r.pending_recommendations > 0 && (
                                            <span className="text-[#10F2C5]"> · {r.pending_recommendations} pending</span>
                                        )}
                                    </div>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                    <span className={`px-1.5 py-0.5 border font-mono text-[9px] tracking-widest ${VERDICT_CLS[r.verdict] || VERDICT_CLS.unavailable}`}>
                                        {(r.verdict || "—").replace(/_/g, " ").toUpperCase()}
                                    </span>
                                    <ArrowRight className="w-3.5 h-3.5 text-[#52525B] group-hover:text-[#10F2C5] transition-colors" />
                                </div>
                            </Link>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
