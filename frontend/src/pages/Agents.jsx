import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    Brain, Search, ShieldCheck, Send, Activity, RefreshCw,
    CheckCircle2, XCircle, AlertTriangle, Clock, Cpu,
} from "lucide-react";

const AGENT_META = {
    research: { icon: Search, label: "Research", color: "#00FF41",
                blurb: "Pulls news sentiment, COT, TIPS, DXY and FRED macro data." },
    strategy: { icon: Brain, label: "Strategy", color: "#FFD700",
                blurb: "Claude Sonnet 4.5 + Logistic Regression → candidate signal." },
    risk:     { icon: ShieldCheck, label: "Risk", color: "#FF3B30",
                blurb: "Portfolio-level checks: cross-asset correlation, exposure caps." },
    execution: { icon: Send, label: "Execution", color: "#A855F7",
                 blurb: "Routes approved orders to the MT5 bridge / paper engine." },
};

const STATUS_PALETTE = {
    ok:      { fg: "text-[#00FF41]", bd: "border-[#00FF41]/40", bg: "bg-[#00FF41]/10", icon: CheckCircle2 },
    vetoed:  { fg: "text-[#FF3B30]", bd: "border-[#FF3B30]/40", bg: "bg-[#FF3B30]/10", icon: XCircle },
    failed:  { fg: "text-[#FFB000]", bd: "border-[#FFB000]/40", bg: "bg-[#FFB000]/10", icon: AlertTriangle },
    skipped: { fg: "text-[#A1A1AA]", bd: "border-[#1F1F1F]",     bg: "bg-[#0A0A0A]",     icon: Clock },
};

const ACTION_COLOR = {
    BUY:  "text-[#00FF41]",
    SELL: "text-[#FF3B30]",
    HOLD: "text-[#A1A1AA]",
};

function fmtTime(iso) {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleTimeString("en-US", { hour12: false });
    } catch { return iso.slice(11, 19); }
}

function fmtDate(iso) {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "2-digit" });
    } catch { return iso.slice(0, 10); }
}

export default function Agents() {
    const [activity, setActivity] = useState([]);
    const [macro, setMacro] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        setErr("");
        try {
            const [act, mac] = await Promise.all([
                api.get("/agents/activity?limit=30"),
                api.get("/agents/macro").catch(() => ({ data: null })),
            ]);
            setActivity(act.data.items || []);
            setMacro(mac.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);
    useEffect(() => {
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);

    return (
        <AppLayout>
            <PageHeader
                title="Agent Pipeline"
                subtitle="Multi-agent architecture · Research → Strategy → Risk → Execution"
                testid="agents-header"
                action={
                    <button onClick={load} data-testid="agents-refresh"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                        <RefreshCw className="w-3.5 h-3.5" /> REFRESH
                    </button>
                }
            />
            <div className="p-4 md:p-8 space-y-6 max-w-6xl">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="agents-error">{err}</div>}

                <AgentRoster />
                <MacroSnapshot macro={macro} />
                <ActivityFeed loading={loading} activity={activity} />
            </div>
        </AppLayout>
    );
}

function AgentRoster() {
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="agent-roster">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Cpu className="w-4 h-4 text-[#00FF41]" />
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">ARCHITECTURE</div>
                    <div className="font-display font-bold text-lg tracking-tight">The four agents</div>
                </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-4 divide-y md:divide-y-0 md:divide-x divide-[#1F1F1F]">
                {Object.entries(AGENT_META).map(([key, meta]) => {
                    const Icon = meta.icon;
                    return (
                        <div key={key} className="p-5" data-testid={`agent-card-${key}`}>
                            <div className="flex items-center gap-2 mb-2">
                                <Icon className="w-4 h-4" style={{ color: meta.color }} />
                                <div className="font-display font-bold text-sm tracking-tight">{meta.label}</div>
                            </div>
                            <p className="text-xs text-[#A1A1AA] leading-relaxed">{meta.blurb}</p>
                        </div>
                    );
                })}
            </div>
        </section>
    );
}

function MacroSnapshot({ macro }) {
    if (!macro) return null;
    const series = macro.series || {};
    const order = ["DFF", "DGS10", "T10YIE", "UNRATE", "VIXCLS"];
    const labels = {
        DFF: "Fed Funds", DGS10: "10Y Yield", T10YIE: "10Y Breakeven",
        UNRATE: "Unemployment", VIXCLS: "VIX",
    };
    const suffix = { DFF: "%", DGS10: "%", T10YIE: "%", UNRATE: "%", VIXCLS: "" };
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="macro-snapshot">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <Activity className="w-4 h-4 text-[#FFD700]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">RESEARCH AGENT · FRED MACRO</div>
                        <div className="font-display font-bold text-lg tracking-tight">Current macro snapshot</div>
                    </div>
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    UPDATED {fmtTime(macro.fetched_at)} UTC
                </div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-5 divide-x divide-y md:divide-y-0 divide-[#1F1F1F]">
                {order.map(k => {
                    const s = series[k];
                    if (!s) return (
                        <div key={k} className="p-4">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">{labels[k]}</div>
                            <div className="font-display font-bold text-base text-[#52525B]">—</div>
                        </div>
                    );
                    const delta = s.delta_30d;
                    return (
                        <div key={k} className="p-4" data-testid={`macro-${k}`}>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">{labels[k]}</div>
                            <div className="font-display font-bold text-xl text-white tabular-nums">
                                {s.value}{suffix[k]}
                            </div>
                            <div className="flex items-center gap-2 mt-1.5">
                                <span className="font-mono text-[9px] text-[#00FF41]/80 tracking-widest uppercase">{s.regime?.replace(/_/g, " ")}</span>
                                {delta !== null && delta !== undefined && (
                                    <span className={`font-mono text-[10px] tabular-nums ${
                                        delta > 0 ? "text-[#00FF41]" : delta < 0 ? "text-[#FF3B30]" : "text-[#52525B]"
                                    }`}>{delta > 0 ? "+" : ""}{delta}</span>
                                )}
                            </div>
                        </div>
                    );
                })}
            </div>
        </section>
    );
}

function ActivityFeed({ loading, activity }) {
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="activity-feed">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                    <Activity className="w-4 h-4 text-[#00FF41]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">LIVE ACTIVITY</div>
                        <div className="font-display font-bold text-lg tracking-tight">{activity.length} tick{activity.length === 1 ? "" : "s"} logged</div>
                    </div>
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">AUTO-REFRESH 30S</div>
            </div>
            {loading ? (
                <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest">LOADING ACTIVITY…</div>
            ) : activity.length === 0 ? (
                <div className="p-12 text-center" data-testid="activity-empty">
                    <Activity className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                    <div className="font-display font-bold text-base mb-1">No agent activity yet</div>
                    <div className="text-sm text-[#A1A1AA]">Start the bot from Bot Config to begin agent ticks.</div>
                </div>
            ) : (
                <div className="divide-y divide-[#1F1F1F]" data-testid="activity-list">
                    {activity.map(tick => <TickRow key={tick.tick_id || tick.id} tick={tick} />)}
                </div>
            )}
        </section>
    );
}

function TickRow({ tick }) {
    const actionColor = ACTION_COLOR[tick.final_action] || "text-[#A1A1AA]";
    return (
        <div className="px-5 py-4" data-testid={`tick-${tick.tick_id}`}>
            <div className="flex items-center justify-between gap-3 flex-wrap mb-3">
                <div className="flex items-center gap-3">
                    <span className="font-display font-bold text-sm tracking-tight">{tick.symbol}</span>
                    <span className={`font-display font-bold text-sm ${actionColor}`}>{tick.final_action}</span>
                    {tick.final_confidence !== null && tick.final_confidence !== undefined && (
                        <span className="font-mono text-xs text-[#A1A1AA] tabular-nums">{tick.final_confidence}%</span>
                    )}
                </div>
                <div className="flex items-center gap-3 font-mono text-[10px] text-[#52525B] tracking-widest">
                    <span>{fmtDate(tick.started_at)} {fmtTime(tick.started_at)}</span>
                    {tick.duration_ms !== null && tick.duration_ms !== undefined && (
                        <span>{tick.duration_ms}ms</span>
                    )}
                </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                {tick.steps.map((step, i) => {
                    const meta = AGENT_META[step.agent] || { label: step.agent, icon: Activity, color: "#52525B" };
                    const palette = STATUS_PALETTE[step.status] || STATUS_PALETTE.skipped;
                    const StatusIcon = palette.icon;
                    const Icon = meta.icon;
                    return (
                        <div key={i} className={`border ${palette.bd} ${palette.bg} px-3 py-2`}
                            data-testid={`step-${step.agent}`}>
                            <div className="flex items-center justify-between mb-1">
                                <div className="flex items-center gap-1.5">
                                    <Icon className="w-3 h-3" style={{ color: meta.color }} />
                                    <span className="font-display font-bold text-[11px] tracking-tight">{meta.label.toUpperCase()}</span>
                                </div>
                                <div className="flex items-center gap-1">
                                    <StatusIcon className={`w-3 h-3 ${palette.fg}`} />
                                    <span className={`font-mono text-[9px] tracking-widest ${palette.fg}`}>
                                        {step.status?.toUpperCase()}
                                    </span>
                                </div>
                            </div>
                            <div className="text-[11px] text-[#A1A1AA] leading-relaxed truncate" title={step.summary || step.error}>
                                {step.summary || step.error || "—"}
                            </div>
                            {step.took_ms !== null && step.took_ms !== undefined && (
                                <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1">{step.took_ms}ms</div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
