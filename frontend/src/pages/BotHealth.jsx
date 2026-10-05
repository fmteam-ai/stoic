import { useEffect, useState, useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { matchFixShortcut } from "@/lib/fixShortcuts";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { toast } from "sonner";
import {
    RefreshCw, Activity, AlertTriangle, CheckCircle2, ShieldCheck, TrendingDown,
    Brain, Clock, FlaskConical, BarChart3, Stethoscope, Zap, Wand2, HeartPulse,
    Wrench, Loader2,
} from "lucide-react";
import { ExecutionHealthPanel } from "../components/ExecutionHealthPanel";
import { ForecastHealthCard } from "../components/ForecastHealthCard";
import { CalibrationHealthCard } from "../components/CalibrationHealthCard";
import { ReadinessCard } from "../components/ReadinessCard";
import { AiLatencyCard } from "../components/AiLatencyCard";
import { ChaosDrillsCard } from "../components/ChaosDrillsCard";
import { StressTestCard } from "../components/StressTestCard";
import { SoakReportCard } from "../components/DemoReadinessPanels";
import { CapitalStageCard, SubsystemHealthCard, RealtimeRiskCard, OperatorConsole, ModeGuardianCard } from "../components/LiveOpsPanels";
import { AlertsCard } from "../components/AlertsCard";
import { ValidationCard } from "../components/ValidationCard";
import { StageCard } from "../components/StageCard";
import { LearningPipelineCard } from "../components/LearningPipelineCard";
import { ModelApprovalPanel } from "../components/ModelApprovalPanel";
import { LearningSpeedsCard } from "../components/LearningSpeedsCard";

const SEV_STYLE = {
    excellent:{ fg:"text-[#00FF41]", bd:"border-[#00FF41]/30", bg:"bg-[#00FF41]/5" },
    healthy:  { fg:"text-[#00FF41]", bd:"border-[#00FF41]/30", bg:"bg-[#00FF41]/5" },
    degraded: { fg:"text-[#FFB000]", bd:"border-[#FFB000]/30", bg:"bg-[#FFB000]/5" },
    warning:  { fg:"text-[#FFB000]", bd:"border-[#FFB000]/30", bg:"bg-[#FFB000]/5" },
    critical: { fg:"text-[#FF3B30]", bd:"border-[#FF3B30]/30", bg:"bg-[#FF3B30]/5" },
    info:     { fg:"text-[#A1A1AA]", bd:"border-[#1F1F1F]",    bg:"bg-[#0A0A0A]"   },
};
const styleFor = k => SEV_STYLE[k] || SEV_STYLE.info;

function HeadlineScore({ data, onAckAll, acking }) {
    const navigate = useNavigate();
    if (!data) return null;
    const score = data.score ?? 0;
    const s = styleFor(score >= 80 ? "excellent" : score >= 60 ? "warning" : "critical");
    return (
        <div className={`border ${s.bd} ${s.bg} p-6`} data-testid="health-headline">
            <div className="flex items-start gap-4 flex-wrap">
                <div className="flex flex-col items-center justify-center min-w-[140px]">
                    <div className={`text-6xl font-display font-bold ${s.fg}`} data-testid="health-score">{score}</div>
                    <div className="font-mono text-[10px] tracking-widest text-[#52525B] mt-1">/ 100</div>
                </div>
                <div className="flex-1 min-w-0">
                    <div className={`font-mono text-[10px] tracking-widest ${s.fg}`}>
                        {(data.status || "—").toUpperCase()}
                    </div>
                    <div className="text-lg font-display font-bold mt-1 text-white">{data.headline}</div>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 mt-3">
                        <Kpi label="ACCOUNTS" value={`${data.context?.accounts_connected ?? 0}/${data.context?.accounts_total ?? 0}`} />
                        <Kpi label="EA VERSION" value={data.context?.ea_latest_version ?? "—"} />
                        <Kpi label="CHECKED" value={data.checked_at ? new Date(data.checked_at).toLocaleTimeString() : "—"} />
                    </div>
                </div>
            </div>
            {Array.isArray(data.issues) && data.issues.length > 0 && (
                <div className="mt-4 pt-4 border-t border-[#1F1F1F] space-y-2">
                    {data.issues.map((i, idx) => (
                        <div key={idx} className="flex items-start gap-2 text-xs">
                            <AlertTriangle className={`w-3.5 h-3.5 shrink-0 mt-0.5 ${styleFor(i.severity).fg}`} />
                            <div className="flex-1">
                                <div className={`font-mono ${styleFor(i.severity).fg}`}>{i.label}</div>
                                {i.fix && <div className="text-[#A1A1AA] leading-relaxed mt-0.5">→ {i.fix}</div>}
                                {i.code === "critical_alerts_open" && Array.isArray(i.details) && i.details.length > 0 && (
                                    <div className="mt-2 space-y-1.5" data-testid="open-critical-alerts">
                                        {i.details.map((a, ai) => {
                                            const fix = matchFixShortcut(a.message);
                                            const seen = a.last_seen_at ? new Date(a.last_seen_at).toLocaleString() : null;
                                            return (
                                                <div key={ai} className="border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2"
                                                    data-testid={`open-critical-alert-${ai}`}>
                                                    <div className="flex items-center justify-between gap-3 flex-wrap">
                                                        <span className="font-mono text-[11px] text-[#A1A1AA]">{a.message}</span>
                                                        {fix && (
                                                            <button onClick={() => navigate(fix.to)}
                                                                data-testid={`alert-fix-shortcut-${ai}`}
                                                                className="shrink-0 font-mono text-[9px] font-bold tracking-widest px-2 py-1 border border-[#FFD700]/50 text-[#FFD700] hover:bg-[#FFD700]/10 transition-colors">
                                                                {fix.label} →
                                                            </button>
                                                        )}
                                                    </div>
                                                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1">
                                                        {a.kind?.toUpperCase()}{a.occurrences > 1 ? ` · RE-FIRED ×${a.occurrences}` : ""}{a.prior_acked > 0 ? ` · CAME BACK ${a.prior_acked}× AFTER ACKNOWLEDGE` : ""}{seen ? ` · LAST SEEN ${seen}` : ""}
                                                    </div>
                                                    {(a.occurrences > 1 || a.prior_acked > 0) && (
                                                        <div className="text-[10px] text-[#FFB000] mt-0.5">
                                                            This alert keeps returning because the underlying condition is still active — acknowledging alone won't stop it. Use the fix link.
                                                        </div>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                )}
                                {i.code === "critical_alerts_open" && onAckAll && (
                                    <button onClick={onAckAll} disabled={acking}
                                        data-testid="ack-all-alerts-btn"
                                        className="mt-1.5 font-mono text-[10px] font-bold tracking-widest px-2.5 py-1 border border-[#FFB000]/50 text-[#FFB000] hover:bg-[#FFB000]/10 disabled:opacity-50 transition-colors">
                                        {acking ? "ACKNOWLEDGING…" : "ACKNOWLEDGE ALL ALERTS"}
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function Kpi({ label, value, tone }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2">
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{label}</div>
            <div className={`font-mono text-sm ${tone || "text-white"}`}>{value}</div>
        </div>
    );
}

function DiagnosticPanel({ diag, onReload }) {
    const [fixing, setFixing] = useState(false);
    if (!diag) return null;
    const total = diag.sections?.length || 0;
    const failingSections = (diag.sections || []).filter(s =>
        (s.checks || []).some(c => c.status !== "ok" && c.status !== "pass" && c.status !== null)
    );
    const fixableCodes = diag.auto_fixable_codes || [];

    const applyFixAll = async () => {
        if (!fixableCodes.length || fixing) return;
        setFixing(true);
        try {
            const r = await api.post("/diagnostic/auto-fix", { codes: fixableCodes });
            const results = r.data?.results || {};
            const summary = Object.entries(results).map(([k, v]) => {
                if (v.error) return `${k}: ${v.error}`;
                const n = v.closed ?? v.acknowledged ?? v.cleared ?? v.released ?? 0;
                return `${k}: ${n}`;
            }).join(" · ");
            toast.success(`Auto-fix applied — ${summary}`);
            if (onReload) await onReload();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setFixing(false);
        }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="diagnostic-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2 flex-wrap">
                <Stethoscope className="w-3.5 h-3.5 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Live Diagnostic</span>
                {fixableCodes.length > 0 && (
                    <button onClick={applyFixAll} disabled={fixing}
                        data-testid="diagnostic-panel-autofix"
                        className="ml-2 px-2.5 py-1 border border-[#FFB000]/40 bg-[#FFB000]/10 text-[#FFB000] text-[10px] font-mono tracking-widest hover:bg-[#FFB000]/20 disabled:opacity-40 inline-flex items-center gap-1.5">
                        {fixing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wrench className="w-3 h-3" />}
                        AUTO-FIX ({fixableCodes.length})
                    </button>
                )}
                <span className={`ml-auto font-mono text-[10px] tracking-widest ${styleFor(diag.status === "pass" ? "healthy" : "warning").fg}`}>
                    {(diag.status || "—").toUpperCase()} · {total - failingSections.length}/{total} OK
                </span>
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                {(diag.sections || []).map(s => {
                    const bad = (s.checks || []).filter(c => c.status !== "ok" && c.status !== "pass" && c.status !== null);
                    return (
                        <div key={s.id} className="px-4 py-3">
                            <div className="flex items-center gap-2">
                                {bad.length === 0
                                    ? <CheckCircle2 className="w-3.5 h-3.5 text-[#00FF41]" />
                                    : <AlertTriangle className="w-3.5 h-3.5 text-[#FFB000]" />
                                }
                                <span className="text-sm font-mono">{s.title}</span>
                            </div>
                            {bad.length > 0 && (
                                <ul className="mt-2 pl-5 space-y-1">
                                    {bad.map((c, i) => (
                                        <li key={i} className="text-xs leading-relaxed">
                                            <span className={`font-mono ${styleFor(c.status === "fail" ? "critical" : "warning").fg}`}>
                                                {c.label}
                                            </span>
                                            {c.detail && <span className="text-[#A1A1AA]"> · {c.detail}</span>}
                                        </li>
                                    ))}
                                </ul>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}

function PulsePanel({ pulse }) {
    if (!pulse) return null;
    const items = pulse.items || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="health-pulse-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Activity className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Bot Pulse</span>
                <span className="ml-auto font-mono text-[10px] tracking-widest text-[#52525B]">
                    {items.filter(i => i.active).length} ACTIVE / {items.length}
                </span>
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                {items.map(i => {
                    const p = i.pulse || {};
                    const lvl = p.level || "info";
                    const s = styleFor(lvl === "block" ? "critical" : lvl === "warn" ? "warning" : "info");
                    return (
                        <div key={i.config_id} className="px-4 py-3 flex items-start gap-3">
                            <div className="w-32 shrink-0">
                                <div className="font-mono text-xs text-white truncate">{i.label}</div>
                                <div className="font-mono text-[10px] tracking-widest text-[#52525B] mt-0.5">
                                    {i.active ? (i.paper_shadow_mode ? "SHADOW" : "LIVE") : "OFF"}
                                </div>
                            </div>
                            <div className="flex-1 min-w-0">
                                <div className={`font-mono text-xs ${s.fg}`}>{p.action || "—"}</div>
                                <div className="text-xs text-[#A1A1AA] leading-snug mt-0.5">{p.reason || "Waiting for first cycle…"}</div>
                            </div>
                        </div>
                    );
                })}
                {items.length === 0 && <div className="px-4 py-3 text-xs text-[#52525B] font-mono">No bot configs yet.</div>}
            </div>
        </div>
    );
}

function SessionsPanel({ sessions, onReload }) {
    const [suggestion, setSuggestion] = useState(null);
    const [busy, setBusy] = useState(false);

    if (!sessions) return null;
    const buckets = (sessions.buckets || []).filter(b => b.count > 0);

    const suggest = async () => {
        setBusy(true);
        try {
            const r = await api.post("/analytics/sessions/suggest-action");
            setSuggestion(r.data);
            if (r.data.action === "no_action") {
                toast.info(r.data.rationale);
            }
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setBusy(false);
        }
    };

    const applySuggestion = async () => {
        if (!suggestion || suggestion.action !== "tighten_worst") return;
        setBusy(true);
        try {
            await api.post("/analytics/sessions/apply-action", {
                field: suggestion.field, to: suggestion.to,
            });
            toast.success(`Applied: ${suggestion.field} → ${suggestion.to}`);
            setSuggestion(null);
            if (onReload) onReload();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="health-sessions-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <BarChart3 className="w-3.5 h-3.5 text-[#FFD700]" />
                <span className="font-display font-bold text-sm">Session Edge</span>
                <div className="ml-auto flex items-center gap-3">
                    <button onClick={suggest} disabled={busy}
                        data-testid="suggest-action-btn"
                        className="flex items-center gap-1.5 px-2.5 py-1 border border-[#FFD700]/40 text-[#FFD700] hover:bg-[#FFD700]/10 text-[10px] font-mono tracking-widest transition-colors disabled:opacity-50">
                        <Wand2 className={`w-3 h-3 ${busy ? "animate-pulse" : ""}`} />
                        SUGGEST ACTION
                    </button>
                    <Link to="/analytics" className="font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-white">
                        DRILL DOWN →
                    </Link>
                </div>
            </div>

            {/* Suggestion modal/banner */}
            {suggestion && suggestion.action === "tighten_worst" && (
                <div className="border-b border-[#FFD700]/30 bg-[#FFD700]/5 px-4 py-3" data-testid="suggestion-banner">
                    <div className="flex items-start gap-3">
                        <Wand2 className="w-4 h-4 text-[#FFD700] shrink-0 mt-0.5" />
                        <div className="flex-1 min-w-0">
                            <div className="font-mono text-[10px] tracking-widest text-[#FFD700] mb-1">
                                SUGGESTED · TIGHTEN {suggestion.session}
                            </div>
                            <div className="text-xs text-[#A1A1AA] leading-relaxed">{suggestion.rationale}</div>
                            <div className="font-mono text-xs text-[#FFD700] mt-2">
                                {suggestion.field}: {suggestion.from} → <span className="font-bold">{suggestion.to}</span>
                            </div>
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                            <button onClick={() => setSuggestion(null)} disabled={busy}
                                data-testid="suggestion-dismiss"
                                className="px-2 py-1 border border-[#1F1F1F] hover:border-[#52525B] text-[10px] font-mono tracking-widest text-[#A1A1AA]">
                                DISMISS
                            </button>
                            <button onClick={applySuggestion} disabled={busy}
                                data-testid="suggestion-apply"
                                className="px-2 py-1 border border-[#00FF41]/40 bg-[#00FF41]/10 hover:bg-[#00FF41]/20 text-[10px] font-mono tracking-widest text-[#00FF41]">
                                APPLY
                            </button>
                        </div>
                    </div>
                </div>
            )}

            <div className="p-4 grid grid-cols-2 md:grid-cols-3 gap-2">
                <Kpi label="OVERALL TRADES" value={sessions.overall?.count ?? 0} />
                <Kpi label="OVERALL WIN%" value={`${sessions.overall?.win_rate ?? 0}%`}
                     tone={sessions.overall?.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FFB000]"} />
                <Kpi label="OVERALL P&L"
                     value={`${(sessions.overall?.total_pnl ?? 0) >= 0 ? "+$" : "-$"}${Math.abs(sessions.overall?.total_pnl ?? 0).toFixed(2)}`}
                     tone={(sessions.overall?.total_pnl ?? 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
            </div>
            {buckets.length > 0 && (
                <div className="border-t border-[#1F1F1F] divide-y divide-[#1F1F1F]">
                    {buckets.map(b => (
                        <div key={b.key} className="px-4 py-2 flex items-center gap-3 text-xs">
                            <span className="font-mono w-16 shrink-0">{b.key}</span>
                            <span className="text-[#52525B] font-mono">n={b.count}</span>
                            <span className="text-[#A1A1AA] font-mono">win%={b.win_rate}</span>
                            <span className={`font-mono ${(b.avg_r ?? 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                R={b.avg_r ?? "—"}
                            </span>
                            <span className={`ml-auto font-mono ${b.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {b.total_pnl >= 0 ? "+$" : "-$"}{Math.abs(b.total_pnl).toFixed(2)}
                            </span>
                        </div>
                    ))}
                </div>
            )}
            {(sessions.best_session_by_r || sessions.best_session_by_pnl) && (
                <div className="px-4 py-2 border-t border-[#1F1F1F] text-xs text-[#A1A1AA]">
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest mr-2">EDGE LIVES IN</span>
                    {sessions.best_session_by_r && <span className="text-[#00FF41] font-mono">{sessions.best_session_by_r}</span>}
                    {sessions.best_session_by_pnl && sessions.best_session_by_pnl !== sessions.best_session_by_r && (
                        <> · top P&L <span className="text-[#FFD700] font-mono">{sessions.best_session_by_pnl}</span></>
                    )}
                </div>
            )}
        </div>
    );
}

function ImprovementsPanel({ adjustments, patterns, blocks }) {
    // Combine auto-adjustments + loss-pattern signals + safety-block stats into
    // a single "what the bot has been learning" feed.
    const adj = adjustments?.items || [];
    const pat = patterns?.items || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="improvements-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Brain className="w-3.5 h-3.5 text-[#9333EA]" />
                <span className="font-display font-bold text-sm">Self-Improvements & Learnings</span>
                <Link to="/loss-lab" className="ml-auto font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-white">
                    LOSS LAB →
                </Link>
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                {/* Auto-tightens */}
                {adj.length > 0 ? adj.slice(0, 5).map(a => (
                    <div key={a.id} className="px-4 py-3 flex items-start gap-3" data-testid={`adj-${a.id}`}>
                        <ShieldCheck className="w-3.5 h-3.5 text-[#FFB000] shrink-0 mt-0.5" />
                        <div className="flex-1 min-w-0">
                            <div className="font-mono text-xs truncate">{a.pattern_key}</div>
                            <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">
                                {a.field}: <span className="text-[#FFB000]">{a.from} → {a.to}</span>
                                {" · "}{a.trigger_count} losses · {new Date(a.created_at).toLocaleString()}
                            </div>
                        </div>
                    </div>
                )) : (
                    <div className="px-4 py-3 text-xs text-[#52525B] font-mono">
                        No auto-adjustments yet — opt in on <Link to="/loss-lab" className="underline hover:text-white">Loss Lab</Link>.
                    </div>
                )}
                {/* Loss patterns detected */}
                {pat.length > 0 && (
                    <>
                        <div className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">
                            RECURRING LOSS PATTERNS · 30d
                        </div>
                        {pat.slice(0, 5).map(p => (
                            <div key={p.pattern_key} className="px-4 py-2 flex items-center gap-3" data-testid={`pattern-${p.pattern_key.replace(/\|/g, "-")}`}>
                                <TrendingDown className="w-3.5 h-3.5 text-[#FF3B30] shrink-0" />
                                <span className="font-mono text-xs truncate flex-1">{p.pattern_key}</span>
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest shrink-0">
                                    {p.count}× · {p.total_pnl >= 0 ? "+" : ""}${p.total_pnl}
                                </span>
                            </div>
                        ))}
                    </>
                )}
                {/* Safety blocks summary */}
                {blocks && blocks.total > 0 && (
                    <>
                        <div className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">
                            SAFETY-GUARDIAN BLOCKS · 7d · {blocks.total} TOTAL
                        </div>
                        {(blocks.by_reason || []).slice(0, 4).map(r => (
                            <div key={r.blocked_by} className="px-4 py-2 flex items-center gap-3">
                                <Zap className="w-3.5 h-3.5 text-[#0099FF] shrink-0" />
                                <span className="text-xs flex-1 truncate">{r.label}</span>
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{r.count}×</span>
                            </div>
                        ))}
                    </>
                )}
            </div>
        </div>
    );
}

function AutoHealPanel({ data, onChange }) {
    const [busy, setBusy] = useState(false);
    const enabled = !!data?.settings?.enabled;
    const log = data?.log || [];

    const toggle = async () => {
        setBusy(true);
        try {
            await api.post("/auto-heal/settings", { enabled: !enabled });
            toast.success(`Auto-Heal ${!enabled ? "ENABLED" : "disabled"} — sweeps every 5 min`);
            if (onChange) await onChange();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setBusy(false); }
    };
    const runNow = async () => {
        setBusy(true);
        try {
            const r = await api.post("/auto-heal/run-now");
            const n = r.data?.actions_taken || 0;
            toast.success(n ? `Auto-Heal ran — ${n} fix(es) applied` : "Auto-Heal ran — everything looks healthy");
            if (onChange) await onChange();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setBusy(false); }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="auto-heal-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <HeartPulse className={`w-3.5 h-3.5 ${enabled ? "text-[#00FF41]" : "text-[#52525B]"}`} />
                <span className="font-display font-bold text-sm">Auto-Heal</span>
                <span className="ml-auto flex items-center gap-2">
                    <button onClick={runNow} disabled={busy}
                        data-testid="auto-heal-run-now"
                        className="px-2.5 py-1 border border-[#1F1F1F] hover:border-[#52525B] text-[10px] font-mono tracking-widest text-[#A1A1AA] disabled:opacity-50">
                        RUN NOW
                    </button>
                    <button onClick={toggle} disabled={busy}
                        data-testid="auto-heal-toggle"
                        className={`px-2.5 py-1 border text-[10px] font-mono tracking-widest transition-colors ${
                            enabled
                                ? "border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]"
                                : "border-[#52525B] text-[#A1A1AA] hover:border-white hover:text-white"
                        }`}>
                        {enabled ? "● ENABLED" : "○ DISABLED"}
                    </button>
                </span>
            </div>

            <div className="px-4 py-3 text-xs text-[#A1A1AA] leading-relaxed border-b border-[#1F1F1F]">
                When enabled, every 5 minutes the bot scans for fixable issues and applies <em>safe, reversible</em> patches —
                raises <code className="px-1 bg-[#1F1F1F]">min_confidence</code> on recurring loss patterns,
                runs <code className="px-1 bg-[#1F1F1F]">reconcile</code> on DB/broker drift,
                clears stale pulses. <strong>Never touches</strong> open trades or the bot ON/OFF state.
            </div>

            {log.length > 0 ? (
                <div className="divide-y divide-[#1F1F1F]">
                    <div className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">
                        RECENT ACTIONS · LAST {log.length}
                    </div>
                    {log.slice(0, 8).map(l => (
                        <div key={l.id} className="px-4 py-2 flex items-start gap-3 text-xs">
                            <Wand2 className="w-3 h-3 text-[#FFD700] shrink-0 mt-0.5" />
                            <div className="flex-1 min-w-0">
                                <div className="font-mono text-white">{l.kind}</div>
                                <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5 truncate">
                                    {JSON.stringify(l.detail).slice(0, 150)}
                                </div>
                            </div>
                            <span className="font-mono text-[9px] text-[#52525B] tracking-widest shrink-0">
                                {new Date(l.created_at).toLocaleTimeString()}
                            </span>
                        </div>
                    ))}
                </div>
            ) : (
                <div className="px-4 py-3 text-xs text-[#52525B] font-mono">
                    No actions yet. {enabled ? "Sweeps run every 5 minutes." : "Enable to start protective sweeps."}
                </div>
            )}
        </div>
    );
}


export default function BotHealth() {
    const [data, setData] = useState({});
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");
    const [last, setLast] = useState(null);
    const [acking, setAcking] = useState(false);

    const ackAll = useCallback(async () => {
        setAcking(true);
        try {
            const { data: r } = await api.post("/ops/alerts/ack-all");
            toast.success(`${r.acked} alert(s) acknowledged — re-checking health`);
            await load();
        } catch (e) {
            toast.error("Could not acknowledge alerts", { description: formatApiError(e) });
        } finally {
            setAcking(false);
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const load = useCallback(async () => {
        setLoading(true); setErr("");
        try {
            const t0 = performance.now();
            // Every panel fetch is bounded and individually caught — one slow
            // or failing endpoint degrades its own panel, never the page.
            const g = (url, fallback = null) =>
                api.get(url, { timeout: 15000 }).catch(() => ({ data: fallback, _failed: true }));
            const [hs, diag, pulse, sess, pats, adj, blocks, ahSet, ahLog, execH, fcst, calib] = await Promise.all([
                g("/bot/health-score"),
                g("/diagnostic/run"),
                g("/bot/pulse"),
                g("/analytics/sessions"),
                g("/postmortem/patterns"),
                g("/postmortem/adjustments"),
                g("/safety-blocks/stats"),
                g("/auto-heal/settings", { enabled: false }),
                g("/auto-heal/log", { items: [] }),
                g("/bot/execution-health"),
                g("/bot/forecast-status"),
                g("/bot/calibration-status"),
            ]);
            const failedCount = [hs, diag, pulse, sess, pats, adj].filter(r => r._failed).length;
            // Keep last-good data for any panel that failed this round.
            setData(prev => ({
                healthScore: hs._failed ? prev.healthScore : hs.data,
                diagnostic: diag._failed ? prev.diagnostic : diag.data,
                pulse: pulse._failed ? prev.pulse : pulse.data,
                sessions: sess._failed ? prev.sessions : sess.data,
                patterns: pats._failed ? prev.patterns : pats.data,
                adjustments: adj._failed ? prev.adjustments : adj.data,
                blocks: blocks._failed ? prev.blocks : blocks.data,
                autoHeal: ahSet._failed && ahLog._failed
                    ? prev.autoHeal
                    : { settings: ahSet.data, log: ahLog.data?.items || [] },
                execHealth: execH._failed ? prev.execHealth : execH.data,
                forecast: fcst._failed ? prev.forecast : fcst.data,
                calibration: calib._failed ? prev.calibration : calib.data,
                apiLatencyMs: Math.round(performance.now() - t0),
            }));
            if (failedCount > 0) {
                setErr(`${failedCount} panel(s) temporarily unreachable — showing last known data, retrying automatically.`);
            }
            setLast(new Date());
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 60000); // refresh every minute
        return () => clearInterval(id);
    }, [load]);

    return (
        <AppLayout>
            <PageHeader
                title="Bot Health"
                subtitle="System-wide check + what the bot has been learning."
                testid="bot-health-header"
                action={
                    <div className="flex items-center gap-2">
                        {last && (
                            <span className="font-mono text-[10px] tracking-widest text-[#52525B] hidden sm:inline">
                                <Clock className="w-3 h-3 inline mr-1" /> {last.toLocaleTimeString()}
                            </span>
                        )}
                        <button onClick={load} disabled={loading}
                            data-testid="bot-health-refresh"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                            {loading ? "CHECKING…" : "RE-CHECK"}
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                <HeadlineScore data={data.healthScore} onAckAll={ackAll} acking={acking} />

                <ReadinessCard />
                <ChaosDrillsCard />
                <StressTestCard />
                <SoakReportCard />
                <CapitalStageCard />
                <SubsystemHealthCard />
                <AiLatencyCard />
                <RealtimeRiskCard />
                <OperatorConsole onAction={load} />
                <ModeGuardianCard />
                <AlertsCard />
                <StageCard />
                <ValidationCard />
                <LearningPipelineCard />
                <ModelApprovalPanel />
                <LearningSpeedsCard />

                <AutoHealPanel data={data.autoHeal} onChange={load} />

                <ExecutionHealthPanel data={data.execHealth} apiLatencyMs={data.apiLatencyMs} />

                <ForecastHealthCard data={data.forecast} />
                <CalibrationHealthCard data={data.calibration} />

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <DiagnosticPanel diag={data.diagnostic} onReload={load} />
                    <PulsePanel pulse={data.pulse} />
                </div>

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <SessionsPanel sessions={data.sessions} onReload={load} />
                    <ImprovementsPanel
                        adjustments={data.adjustments}
                        patterns={data.patterns}
                        blocks={data.blocks} />
                </div>
            </div>
        </AppLayout>
    );
}
