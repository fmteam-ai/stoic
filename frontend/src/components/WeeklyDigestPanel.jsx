import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import {
    Newspaper, TrendingUp, TrendingDown, Activity, Wrench, Lightbulb,
    Sparkles, Mail, CheckCircle2, AlertCircle,
} from "lucide-react";

/* Weekly AI Digest — a 7-day recap of the user's bot activity.
   Surfaces win rate, P&L, best/worst trade, auto-heal action count, top HOLD
   reasons, and a rule-based suggested next action. */

function fmtPnl(n) {
    if (n == null) return "—";
    const sign = n >= 0 ? "+" : "";
    return `${sign}$${n.toFixed(2)}`;
}

function StatTile({ label, value, color = "#FAFAFA", testid }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#050505] p-3" data-testid={testid}>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{label}</div>
            <div className="font-mono text-base font-medium mt-1" style={{ color }}>{value}</div>
        </div>
    );
}

function HorizontalBar({ label, count, total, color }) {
    const pct = total > 0 ? Math.round((count / total) * 100) : 0;
    return (
        <div className="space-y-1">
            <div className="flex items-center justify-between font-mono text-[10px]">
                <span className="text-[#A1A1AA]">{label}</span>
                <span className="text-[#52525B]">{count} · {pct}%</span>
            </div>
            <div className="h-1.5 bg-[#1F1F1F]">
                <div className="h-full transition-all duration-500"
                     style={{ width: `${pct}%`, backgroundColor: color }} />
            </div>
        </div>
    );
}

const HOLD_REASON_COLORS = {
    "Noisy entropy": "#FFB000",
    "Market closed": "#00FF41",
    "Macro freeze": "#FFD700",
    "Regime CHOP": "#FF3B30",
    "Other": "#52525B",
};

export default function WeeklyDigestPanel() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);
    const [reflectionLoading, setReflectionLoading] = useState(false);
    const [emailStatus, setEmailStatus] = useState(null);

    const fetchDigest = useCallback(async (includeAi = false) => {
        try {
            const res = await api.get(`/insights/weekly-digest${includeAi ? "?include_ai=true" : ""}`);
            setData(res.data);
            setErr(null);
        } catch (e) {
            setErr(e?.response?.data?.detail || e.message || "Failed to load digest");
        } finally {
            setLoading(false);
        }
    }, []);

    const generateReflection = useCallback(async () => {
        setReflectionLoading(true);
        try { await fetchDigest(true); }
        finally { setReflectionLoading(false); }
    }, [fetchDigest]);

    const emailDigest = useCallback(async () => {
        setEmailStatus({ state: "sending" });
        try {
            const res = await api.post("/insights/weekly-digest/email");
            if (res.data?.ok) {
                setEmailStatus({ state: "sent", recipient: res.data.recipient });
            } else {
                setEmailStatus({
                    state: "error",
                    error: res.data?.error || "Failed to send",
                });
            }
        } catch (e) {
            setEmailStatus({
                state: "error",
                error: e?.response?.data?.detail || e.message || "Email failed",
            });
        }
    }, []);

    useEffect(() => {
        fetchDigest();
        const id = setInterval(fetchDigest, 5 * 60 * 1000);  // refresh every 5 min
        return () => clearInterval(id);
    }, [fetchDigest]);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6" data-testid="weekly-digest-loading">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest animate-pulse">
                    GENERATING WEEKLY DIGEST…
                </div>
            </div>
        );
    }
    if (err || !data) return null;

    const s = data.stats || {};
    const totalHolds = (data.hold_reasons || []).reduce((acc, r) => acc + r.count, 0);
    const wrColor = s.win_rate >= 60 ? "#00FF41" : s.win_rate >= 40 ? "#FFD700" : "#FF3B30";
    const pnlColor = s.pnl_total >= 0 ? "#00FF41" : "#FF3B30";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="weekly-digest-panel">
            {/* Header */}
            <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <Newspaper className="w-4 h-4 text-[#FFD700]" />
                    <div>
                        <div className="font-mono text-xs tracking-widest">WEEKLY AI DIGEST</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                            Last {data.window_days}-day recap · auto-refreshes every 5min
                        </div>
                    </div>
                </div>
                <Activity className="w-3.5 h-3.5 text-[#52525B] animate-pulse" />
            </div>

            <div className="p-5 space-y-5">
                {/* Headline stats — 4 tiles */}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <StatTile label="TRADES CLOSED" value={s.trades || 0} testid="digest-trades" />
                    <StatTile label="WIN RATE" value={`${s.win_rate || 0}%`} color={wrColor} testid="digest-wr" />
                    <StatTile label="NET P&L" value={fmtPnl(s.pnl_total)} color={pnlColor} testid="digest-pnl" />
                    <StatTile label="AUTO-HEALS" value={s.auto_heals || 0}
                              color={(s.auto_heals || 0) > 0 ? "#FFB000" : "#FAFAFA"} testid="digest-heals" />
                </div>

                {/* Best / Worst trade */}
                {(data.best_trade || data.worst_trade) && (
                    <div className="grid md:grid-cols-2 gap-3">
                        {data.best_trade && (
                            <div className="border border-[#00FF41]/20 bg-[#00FF41]/5 p-3 flex items-center gap-3"
                                 data-testid="digest-best-trade">
                                <TrendingUp className="w-4 h-4 text-[#00FF41]" />
                                <div className="flex-1 min-w-0">
                                    <div className="font-mono text-[10px] text-[#00FF41] tracking-widest">BEST TRADE</div>
                                    <div className="font-mono text-sm mt-0.5 truncate">
                                        {data.best_trade.action} {data.best_trade.symbol}
                                    </div>
                                </div>
                                <div className="font-mono text-sm text-[#00FF41]">{fmtPnl(data.best_trade.pnl)}</div>
                            </div>
                        )}
                        {data.worst_trade && (
                            <div className="border border-[#FF3B30]/20 bg-[#FF3B30]/5 p-3 flex items-center gap-3"
                                 data-testid="digest-worst-trade">
                                <TrendingDown className="w-4 h-4 text-[#FF3B30]" />
                                <div className="flex-1 min-w-0">
                                    <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest">WORST TRADE</div>
                                    <div className="font-mono text-sm mt-0.5 truncate">
                                        {data.worst_trade.action} {data.worst_trade.symbol}
                                    </div>
                                </div>
                                <div className="font-mono text-sm text-[#FF3B30]">{fmtPnl(data.worst_trade.pnl)}</div>
                            </div>
                        )}
                    </div>
                )}

                {/* HOLD reason breakdown */}
                {totalHolds > 0 && (
                    <div className="space-y-3">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                            WHY THE BOT HELD ({totalHolds.toLocaleString()} signals)
                        </div>
                        <div className="space-y-2">
                            {(data.hold_reasons || []).map(r => (
                                <HorizontalBar
                                    key={r.label}
                                    label={r.label}
                                    count={r.count}
                                    total={totalHolds}
                                    color={HOLD_REASON_COLORS[r.label] || "#52525B"}
                                />
                            ))}
                        </div>
                    </div>
                )}

                {/* Auto-heal breakdown — only when non-empty */}
                {(data.auto_heal_breakdown || []).length > 0 && (
                    <div className="space-y-2">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest flex items-center gap-1.5">
                            <Wrench className="w-3 h-3" /> AUTO-HEAL ACTIONS
                        </div>
                        <div className="flex flex-wrap gap-2">
                            {data.auto_heal_breakdown.map(h => (
                                <span key={h.kind}
                                      className="border border-[#FFB000]/30 bg-[#FFB000]/5 px-2 py-1 font-mono text-[10px] text-[#FFB000] tracking-widest"
                                      data-testid={`digest-heal-${h.kind}`}>
                                    {h.kind.toUpperCase()} × {h.count}
                                </span>
                            ))}
                        </div>
                    </div>
                )}

                {/* Suggested action */}
                {data.suggested_action && (
                    <div className="border-l-2 border-[#FFD700] bg-[#FFD700]/5 p-3 flex items-start gap-3"
                         data-testid="digest-suggestion">
                        <Lightbulb className="w-4 h-4 text-[#FFD700] shrink-0 mt-0.5" />
                        <div>
                            <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">SUGGESTED ACTION</div>
                            <p className="font-mono text-xs text-[#FAFAFA] mt-1 leading-relaxed">
                                {data.suggested_action}
                            </p>
                        </div>
                    </div>
                )}

                {/* iter-69 — Claude-written reflection (lazy on-demand) */}
                {data.ai_reflection ? (
                    <div className="border border-[#FFD700]/20 bg-[#FFD700]/5 p-4 space-y-2"
                         data-testid="digest-ai-reflection">
                        <div className="font-mono text-[10px] text-[#FFD700] tracking-widest flex items-center gap-1.5">
                            <Sparkles className="w-3 h-3" /> AI REFLECTION · CLAUDE SONNET 4.5
                        </div>
                        <div className="text-[13px] text-[#FAFAFA] leading-relaxed whitespace-pre-line"
                             style={{ fontFamily: "Georgia, serif" }}>
                            {data.ai_reflection}
                        </div>
                    </div>
                ) : (
                    <button onClick={generateReflection} disabled={reflectionLoading}
                            data-testid="digest-generate-reflection-btn"
                            className="w-full border border-[#1F1F1F] hover:border-[#FFD700]/40 hover:bg-[#FFD700]/5
                                       p-3 font-mono text-[11px] text-[#A1A1AA] tracking-widest
                                       flex items-center justify-center gap-2 transition-colors
                                       disabled:opacity-50 disabled:cursor-not-allowed">
                        <Sparkles className={`w-3.5 h-3.5 ${reflectionLoading ? "animate-pulse" : ""}`} />
                        {reflectionLoading ? "CLAUDE IS THINKING…" : "GENERATE AI REFLECTION"}
                    </button>
                )}

                {/* iter-69 — Email me this digest (Resend) */}
                <div className="border-t border-[#1F1F1F] pt-3 flex items-center justify-between gap-3">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest leading-tight">
                        Get the digest delivered to your inbox.
                    </div>
                    <button onClick={emailDigest}
                            disabled={emailStatus?.state === "sending"}
                            data-testid="digest-email-btn"
                            className="shrink-0 border border-[#1F1F1F] hover:border-[#FFD700]/40 hover:bg-[#FFD700]/5
                                       px-3 py-1.5 font-mono text-[10px] text-[#FFD700] tracking-widest
                                       flex items-center gap-1.5 transition-colors
                                       disabled:opacity-50 disabled:cursor-not-allowed">
                        <Mail className="w-3 h-3" />
                        {emailStatus?.state === "sending" ? "SENDING…" : "EMAIL ME"}
                    </button>
                </div>
                {emailStatus?.state === "sent" && (
                    <div className="flex items-start gap-2 text-[10px] font-mono text-[#00FF41]"
                         data-testid="digest-email-success">
                        <CheckCircle2 className="w-3 h-3 mt-0.5 shrink-0" />
                        <span>Sent to {emailStatus.recipient}. Check your inbox in ~30s.</span>
                    </div>
                )}
                {emailStatus?.state === "error" && (
                    <div className="flex items-start gap-2 text-[10px] font-mono text-[#FF3030]"
                         data-testid="digest-email-error">
                        <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
                        <span>{emailStatus.error}</span>
                    </div>
                )}
            </div>
        </div>
    );
}
