/* BotDoctorPanel — iter-75 LITE dashboard tile.
   Surfaces Claude Sonnet 4.5's structured diagnosis of the last hour
   of bot telemetry. NO auto-apply: every recommendation is presented
   for human action only. */
import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Stethoscope, RefreshCw, AlertTriangle, ShieldCheck, Activity, Sparkles, ChevronDown, ChevronUp } from "lucide-react";
import { formatApiError } from "@/lib/api";

const STATUS_STYLES = {
    healthy:  { color: "#00FF41", bg: "bg-[#00FF41]/5",  border: "border-[#00FF41]/30", Icon: ShieldCheck, label: "HEALTHY" },
    watch:    { color: "#FFD700", bg: "bg-[#FFD700]/5",  border: "border-[#FFD700]/30", Icon: Activity,    label: "WATCH" },
    degraded: { color: "#FFB000", bg: "bg-[#FFB000]/5",  border: "border-[#FFB000]/30", Icon: AlertTriangle, label: "DEGRADED" },
    critical: { color: "#FF3B30", bg: "bg-[#FF3B30]/10", border: "border-[#FF3B30]/40", Icon: AlertTriangle, label: "CRITICAL" },
};

function EffortPill({ effort }) {
    const map = { low: "text-[#00FF41] border-[#00FF41]/30",
                  medium: "text-[#FFD700] border-[#FFD700]/30",
                  high: "text-[#FFB000] border-[#FFB000]/30" };
    return (
        <span className={`font-mono text-[9px] tracking-widest border px-1.5 py-0.5 ${map[effort] || map.low}`}>
            {(effort || "low").toUpperCase()}
        </span>
    );
}

function relativeAge(iso) {
    if (!iso) return "—";
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return `${Math.round(diff)}s ago`;
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    return `${Math.round(diff / 3600)}h ago`;
}

export default function BotDoctorPanel() {
    const [diag, setDiag] = useState(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [expanded, setExpanded] = useState(true);
    const [err, setErr] = useState(null);

    const fetchDiagnosis = useCallback(async (force = false) => {
        try {
            if (force) setRefreshing(true);
            const r = await api.get(`/bot/doctor${force ? "?force_refresh=true" : ""}`);
            setDiag(r.data);
            setErr(null);
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        fetchDiagnosis();
        const id = setInterval(() => fetchDiagnosis(false), 60_000); // refresh every minute (cache TTL is 5min so this is cheap)
        return () => clearInterval(id);
    }, [fetchDiagnosis]);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid="bot-doctor-loading">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest animate-pulse">
                    BOT DOCTOR · CONSULTING…
                </div>
            </div>
        );
    }
    if (err || !diag) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid="bot-doctor-error">
                <div className="font-mono text-[10px] text-[#FFB000] tracking-widest">BOT DOCTOR · OFFLINE</div>
                <div className="font-mono text-[9px] text-[#52525B] mt-1">{err || "no diagnosis available"}</div>
            </div>
        );
    }

    const style = STATUS_STYLES[diag.status] || STATUS_STYLES.watch;
    const StatusIcon = style.Icon;
    const llmFailed = diag._llm_failed;

    return (
        <div className={`border ${style.border} ${style.bg}`} data-testid="bot-doctor-panel">
            {/* Header */}
            <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-start gap-3">
                <Stethoscope className="w-4 h-4 mt-0.5" style={{ color: style.color }} />
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <div className="font-mono text-xs tracking-widest">BOT DOCTOR</div>
                        <div className="font-mono text-[10px] tracking-widest border px-1.5 py-0.5"
                             style={{ color: style.color, borderColor: `${style.color}55` }}>
                            <StatusIcon className="w-2.5 h-2.5 inline mr-1" />
                            {style.label}
                        </div>
                        {!llmFailed && (
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest flex items-center gap-1">
                                <Sparkles className="w-2.5 h-2.5" /> CLAUDE SONNET 4.5
                            </div>
                        )}
                    </div>
                    <div className="font-display text-sm mt-1.5 leading-snug">
                        {diag.headline}
                    </div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1">
                        UPDATED {relativeAge(diag.generated_at)}
                        {diag.cache_hit ? " · CACHED" : " · FRESH"}
                        {llmFailed && " · LLM FALLBACK"}
                    </div>
                </div>
                <button
                    onClick={() => fetchDiagnosis(true)}
                    disabled={refreshing}
                    data-testid="bot-doctor-refresh"
                    className="font-mono text-[10px] tracking-widest border border-[#1F1F1F] hover:border-[#333333] px-2 py-1 text-[#A1A1AA] disabled:opacity-50"
                    title="Force fresh diagnosis (bypasses 5min cache)"
                >
                    <RefreshCw className={`w-3 h-3 inline ${refreshing ? "animate-spin" : ""}`} />
                    <span className="ml-1.5">CONSULT</span>
                </button>
                <button
                    onClick={() => setExpanded(v => !v)}
                    data-testid="bot-doctor-toggle"
                    className="font-mono text-[10px] text-[#52525B] hover:text-[#A1A1AA] p-1"
                >
                    {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                </button>
            </div>

            {expanded && (
                <div className="p-5 space-y-4">
                    {/* Findings */}
                    {diag.findings?.length > 0 && (
                        <div data-testid="bot-doctor-findings">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">FINDINGS</div>
                            <ul className="space-y-1.5">
                                {diag.findings.map((f) => (
                                    <li key={f}
                                        className="text-sm text-[#E4E4E7] flex items-start gap-2 leading-snug">
                                        <span className="text-[#52525B] mt-1 shrink-0">›</span>
                                        <span>{f}</span>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}

                    {/* Hypothesis */}
                    {diag.root_cause_hypothesis && (
                        <div data-testid="bot-doctor-hypothesis">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1.5">ROOT CAUSE HYPOTHESIS</div>
                            <div className="text-sm text-[#E4E4E7] leading-snug border-l-2 pl-3" style={{ borderColor: style.color }}>
                                {diag.root_cause_hypothesis}
                            </div>
                        </div>
                    )}

                    {/* Recommendations */}
                    {diag.recommendations?.length > 0 && (
                        <div data-testid="bot-doctor-recommendations">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">RECOMMENDED ACTIONS</div>
                            <ol className="space-y-2.5">
                                {diag.recommendations.map((r, i) => (
                                    <li key={`${r.action}-${i}`}
                                        className="border border-[#1F1F1F] bg-[#0F0F0F] p-3 space-y-1.5">
                                        <div className="flex items-start gap-2 flex-wrap">
                                            <span className="font-mono text-[10px] text-[#52525B] tracking-widest shrink-0 mt-0.5">
                                                #{i + 1}
                                            </span>
                                            <span className="text-sm text-[#E4E4E7] flex-1 min-w-0 leading-snug">
                                                {r.action}
                                            </span>
                                            <EffortPill effort={r.effort} />
                                            {r.destructive && (
                                                <span className="font-mono text-[9px] tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] px-1.5 py-0.5">
                                                    DESTRUCTIVE
                                                </span>
                                            )}
                                        </div>
                                        {r.rationale && (
                                            <div className="font-mono text-[10px] text-[#52525B] tracking-wider leading-snug pl-7">
                                                {r.rationale}
                                            </div>
                                        )}
                                    </li>
                                ))}
                            </ol>
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-3">
                                ⓘ LITE MODE · ALL ACTIONS REQUIRE MANUAL CONFIRMATION
                            </div>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
