/* AiOptimizerSection — Bot Config detail panel for the AI Strategy Optimizer.

   Per selected account scope:
     · ANALYZE 24H / 48H buttons → POST /api/optimizer/analyze
     · Latest report: verdict, headline, stats strip, patterns
     · Recommendation cards (suggest-only) with APPLY / DISMISS actions
   Applying a config_change / preset_switch / pause_bot calls the backend,
   then `onConfigChanged()` so the parent page reloads the live config. */
import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import {
    BrainCircuit, RefreshCw, Check, X, PauseCircle, Layers,
    SlidersHorizontal, AlertTriangle, Info, Flame,
} from "lucide-react";

const VERDICT_STYLE = {
    healthy:           { label: "HEALTHY",          cls: "text-[#00FF41] border-[#00FF41]/40" },
    needs_tuning:      { label: "NEEDS TUNING",     cls: "text-[#FFD700] border-[#FFD700]/40" },
    underperforming:   { label: "UNDERPERFORMING",  cls: "text-[#FF6B00] border-[#FF6B00]/40" },
    critical:          { label: "CRITICAL",         cls: "text-[#FF3B30] border-[#FF3B30]/40" },
    insufficient_data: { label: "NOT ENOUGH DATA",  cls: "text-[#52525B] border-[#1F1F1F]" },
    unavailable:       { label: "AI UNAVAILABLE",   cls: "text-[#52525B] border-[#1F1F1F]" },
};

const SEVERITY_ICON = {
    info:     { Icon: Info,          cls: "text-[#0099FF]" },
    warning:  { Icon: AlertTriangle, cls: "text-[#FFD700]" },
    critical: { Icon: Flame,         cls: "text-[#FF3B30]" },
};

const fieldLabel = (f) => (f || "").replace(/_/g, " ").toUpperCase();
const fmtVal = (v) => (typeof v === "boolean" ? (v ? "ON" : "OFF") : String(v ?? "—"));

function agoLabel(iso) {
    if (!iso) return "";
    const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const h = Math.round(mins / 60);
    return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

function StatCell({ label, value, accent }) {
    return (
        <div className="p-3 border border-[#1F1F1F] bg-black/40">
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className={`font-mono font-medium text-sm ${accent || "text-white"}`}>{value}</div>
        </div>
    );
}

function RecCard({ rec, onApply, onDismiss, busy }) {
    const isPending = rec.status === "pending";
    const typeMeta = rec.type === "preset_switch"
        ? { Icon: Layers, label: "SWITCH PRESET", accent: "text-[#FFD700]" }
        : rec.type === "pause_bot"
        ? { Icon: PauseCircle, label: "PAUSE BOT", accent: "text-[#FF3B30]" }
        : { Icon: SlidersHorizontal, label: "CONFIG CHANGE", accent: "text-[#10F2C5]" };
    const { Icon } = typeMeta;
    return (
        <div className={`border p-4 space-y-2 transition-opacity ${
                rec.status === "dismissed" ? "border-[#1F1F1F] opacity-40"
                : rec.status === "applied" ? "border-[#00FF41]/30"
                : "border-[#333333]"}`}
            data-testid={`optimizer-rec-${rec.id}`}>
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className={`flex items-center gap-1.5 font-mono text-[10px] tracking-widest ${typeMeta.accent}`}>
                    <Icon className="w-3.5 h-3.5" /> {typeMeta.label}
                </div>
                {rec.status === "applied" && (
                    <span className="flex items-center gap-1 font-mono text-[10px] text-[#00FF41] tracking-widest">
                        <Check className="w-3 h-3" /> APPLIED
                    </span>
                )}
                {rec.status === "dismissed" && (
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">DISMISSED</span>
                )}
            </div>
            {rec.type === "config_change" && (
                <div className="font-mono text-xs text-white">
                    {fieldLabel(rec.field)}:{" "}
                    <span className="text-[#52525B] line-through">{fmtVal(rec.from)}</span>
                    <span className="text-[#52525B] mx-1.5">→</span>
                    <span className="text-[#10F2C5]">{fmtVal(rec.to)}</span>
                </div>
            )}
            {rec.type === "preset_switch" && (
                <div className="font-mono text-xs text-white">
                    {rec.from_preset ? fieldLabel(rec.from_preset) : "CURRENT"}
                    <span className="text-[#52525B] mx-1.5">→</span>
                    <span className="text-[#FFD700]">{(rec.preset_label || rec.preset_key).toUpperCase()}</span>
                </div>
            )}
            {rec.type === "pause_bot" && (
                <div className="font-mono text-xs text-[#FF3B30]">STOP AUTO-TRADING ON THIS SCOPE</div>
            )}
            <p className="text-xs text-[#A1A1AA] leading-relaxed">{rec.reason}</p>
            {rec.expected_impact && (
                <p className="text-[11px] text-[#52525B] leading-relaxed">
                    <span className="font-mono text-[9px] tracking-widest text-[#10F2C5]">EXPECTED IMPACT · </span>
                    {rec.expected_impact}
                </p>
            )}
            {isPending && (
                <div className="flex items-center gap-2 pt-1">
                    <button onClick={() => onApply(rec)} disabled={busy}
                        data-testid={`optimizer-apply-${rec.id}`}
                        className="px-3 py-1.5 text-[10px] tracking-widest font-medium bg-[#10F2C5] hover:bg-[#0DD9B0] disabled:opacity-50 text-black flex items-center gap-1.5 transition-colors">
                        <Check className="w-3 h-3" /> APPLY
                    </button>
                    <button onClick={() => onDismiss(rec)} disabled={busy}
                        data-testid={`optimizer-dismiss-${rec.id}`}
                        className="px-3 py-1.5 text-[10px] tracking-widest font-medium border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333] disabled:opacity-50 flex items-center gap-1.5 transition-colors">
                        <X className="w-3 h-3" /> DISMISS
                    </button>
                </div>
            )}
        </div>
    );
}

export default function AiOptimizerSection({ accountId, onConfigChanged, anchorRef }) {
    const [report, setReport] = useState(null);
    const [loading, setLoading] = useState(true);
    const [analyzing, setAnalyzing] = useState(null); // 24 | 48 | null
    const [busyRec, setBusyRec] = useState(null);

    const scopeQ = accountId ? `?account_id=${accountId}` : "";

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await api.get(`/optimizer/report${scopeQ}`);
            setReport(data?.exists === false ? null : data);
        } catch (_e) { setReport(null); }
        setLoading(false);
    }, [scopeQ]);

    useEffect(() => { load(); }, [load]);

    const analyze = async (window) => {
        setAnalyzing(window);
        try {
            const sep = scopeQ ? "&" : "?";
            const { data } = await api.post(`/optimizer/analyze${scopeQ}${sep}window=${window}`);
            setReport(data);
            if (data.cached) {
                toast.info("Showing a recent report", { description: "A fresh analysis ran less than 5 minutes ago." });
            } else if (data.insufficient_data) {
                toast.warning("Not enough closed trades", { description: data.headline });
            } else {
                toast.success("AI review complete", { description: data.headline });
            }
        } catch (e) {
            toast.error("Analysis failed", { description: formatApiError(e) });
        }
        setAnalyzing(null);
    };

    const patchRecStatus = (recId, status) => {
        setReport(r => r && ({
            ...r,
            recommendations: (r.recommendations || []).map(x => x.id === recId ? { ...x, status } : x),
        }));
    };

    const applyRec = async (rec) => {
        setBusyRec(rec.id);
        try {
            await api.post(`/optimizer/report/${report.id}/rec/${rec.id}/apply`);
            patchRecStatus(rec.id, "applied");
            toast.success(rec.type === "pause_bot" ? "Bot paused" : "Recommendation applied");
            onConfigChanged?.();
        } catch (e) {
            toast.error("Apply failed", { description: formatApiError(e) });
        }
        setBusyRec(null);
    };

    const dismissRec = async (rec) => {
        setBusyRec(rec.id);
        try {
            await api.post(`/optimizer/report/${report.id}/rec/${rec.id}/dismiss`);
            patchRecStatus(rec.id, "dismissed");
        } catch (e) {
            toast.error("Dismiss failed", { description: formatApiError(e) });
        }
        setBusyRec(null);
    };

    const verdict = VERDICT_STYLE[report?.verdict] || null;
    const stats = report?.stats;

    return (
        <div ref={anchorRef} className="border border-[#10F2C5]/30 bg-[#0A0A0A]" data-testid="ai-optimizer-section">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2">
                    <BrainCircuit className="w-4 h-4 text-[#10F2C5]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#10F2C5] tracking-widest">AI STRATEGY OPTIMIZER · SUGGEST-ONLY</div>
                        <div className="font-display font-bold text-lg tracking-tight">Trade review & tuning</div>
                    </div>
                </div>
                <div className="flex items-center gap-2">
                    {[24, 48].map(w => (
                        <button key={w} onClick={() => analyze(w)} disabled={analyzing !== null}
                            data-testid={`optimizer-analyze-${w}h`}
                            className="px-3 py-1.5 text-[10px] tracking-widest font-medium border border-[#10F2C5]/40 text-[#10F2C5] hover:bg-[#10F2C5]/10 disabled:opacity-50 flex items-center gap-1.5 transition-colors">
                            <RefreshCw className={`w-3 h-3 ${analyzing === w ? "animate-spin" : ""}`} />
                            {analyzing === w ? "ANALYZING…" : `ANALYZE ${w}H`}
                        </button>
                    ))}
                </div>
            </div>

            <div className="p-5 space-y-4">
                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                ) : !report ? (
                    <div className="text-xs text-[#A1A1AA] leading-relaxed" data-testid="optimizer-empty-state">
                        No AI review yet for this scope. The optimizer reads the last 24–48h of
                        <span className="text-white"> bot-executed</span> closed trades (manual trades
                        you open on the broker terminal are excluded), finds losing patterns
                        (session, symbol, direction, exit reason) and proposes concrete config
                        changes — nothing is applied without your click. A scheduled review also
                        runs automatically every 24h while the bot is active.
                    </div>
                ) : (
                    <>
                        <div className="flex items-center justify-between gap-3 flex-wrap">
                            <div className="flex items-center gap-2 flex-wrap">
                                {verdict && (
                                    <span className={`px-2 py-1 border font-mono text-[10px] tracking-widest ${verdict.cls}`}
                                        data-testid="optimizer-verdict">{verdict.label}</span>
                                )}
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                    {report.window_hours}H WINDOW · {report.source === "scheduled" ? "AUTO" : "MANUAL"} · {agoLabel(report.created_at)}
                                    {report.model_used ? ` · ${report.model_used.toUpperCase()}` : ""}
                                </span>
                                {report.excluded_manual_trades > 0 && (
                                    <span className="font-mono text-[10px] text-[#FFB000] tracking-widest"
                                        data-testid="optimizer-excluded-manual">
                                        {report.excluded_manual_trades} MANUAL TRADE{report.excluded_manual_trades > 1 ? "S" : ""} EXCLUDED
                                    </span>
                                )}
                            </div>
                        </div>

                        {report.headline && (
                            <p className="text-sm text-white leading-relaxed font-medium" data-testid="optimizer-headline">{report.headline}</p>
                        )}
                        {report.summary && (
                            <p className="text-xs text-[#A1A1AA] leading-relaxed">{report.summary}</p>
                        )}

                        {stats && stats.total_trades > 0 && (
                            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2" data-testid="optimizer-stats">
                                <StatCell label="TRADES" value={stats.total_trades} />
                                <StatCell label="WIN RATE" value={`${stats.win_rate}%`}
                                    accent={stats.win_rate >= 55 ? "text-[#00FF41]" : stats.win_rate >= 40 ? "text-[#FFD700]" : "text-[#FF3B30]"} />
                                <StatCell label="NET P&L" value={`$${stats.total_pnl?.toLocaleString()}`}
                                    accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
                                <StatCell label="PROFIT FACTOR" value={stats.profit_factor ?? "∞"} />
                                <StatCell label="WORST STREAK" value={`${stats.worst_losing_streak}L`}
                                    accent={stats.worst_losing_streak >= 5 ? "text-[#FF3B30]" : "text-white"} />
                            </div>
                        )}

                        {(report.patterns || []).length > 0 && (
                            <div className="space-y-2" data-testid="optimizer-patterns">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">DETECTED PATTERNS</div>
                                {report.patterns.map((p, i) => {
                                    const sev = SEVERITY_ICON[p.severity] || SEVERITY_ICON.info;
                                    const SevIcon = sev.Icon;
                                    return (
                                        <div key={i} className="flex items-start gap-2 border border-[#1F1F1F] bg-black/40 p-3">
                                            <SevIcon className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${sev.cls}`} />
                                            <div>
                                                <div className="text-xs font-medium text-white">{p.title}</div>
                                                <div className="text-[11px] text-[#A1A1AA] leading-relaxed mt-0.5">{p.detail}</div>
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        )}

                        {(report.recommendations || []).length > 0 ? (
                            <div className="space-y-2" data-testid="optimizer-recommendations">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">RECOMMENDATIONS — APPLY ONLY WHAT YOU TRUST</div>
                                {report.recommendations.map(rec => (
                                    <RecCard key={rec.id} rec={rec} busy={busyRec === rec.id}
                                        onApply={applyRec} onDismiss={dismissRec} />
                                ))}
                            </div>
                        ) : !report.insufficient_data && report.verdict === "healthy" ? (
                            <div className="font-mono text-[10px] text-[#00FF41] tracking-widest">
                                NO CHANGES SUGGESTED — CURRENT SETTINGS ARE PERFORMING WELL.
                            </div>
                        ) : null}
                    </>
                )}
            </div>
        </div>
    );
}
