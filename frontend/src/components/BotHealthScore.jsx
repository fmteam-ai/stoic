import { useEffect, useState, useCallback, useMemo } from "react";
import api from "@/lib/api";
import { Activity, AlertTriangle, ShieldCheck, AlertCircle, Info, ChevronDown } from "lucide-react";

const STATUS_STYLE = {
    excellent: { color: "#00FF41", label: "EXCELLENT" },
    good:      { color: "#FFD700", label: "GOOD" },
    degraded:  { color: "#FFB000", label: "DEGRADED" },
    critical:  { color: "#FF3B30", label: "CRITICAL" },
};

const ISSUE_ICON = {
    error:   AlertCircle,
    warning: AlertTriangle,
    info:    Info,
};
const ISSUE_COLOR = {
    error:   "#FF3B30",
    warning: "#FFB000",
    info:    "#A1A1AA",
};

export function BotHealthScore({ refreshSignal }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [expanded, setExpanded] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data: d } = await api.get("/bot/health-score");
            setData(d);
        } catch { /* stay on last good value */ }
        finally { setLoading(false); }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 30_000);   // refresh every 30s
        return () => clearInterval(t);
    }, [load, refreshSignal]);

    const style = useMemo(() => STATUS_STYLE[data?.status] || STATUS_STYLE.good, [data]);

    if (loading && !data) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] px-5 py-4 font-mono text-[10px] text-[#52525B] tracking-widest"
                 data-testid="health-score-loading">
                LOADING BOT HEALTH…
            </div>
        );
    }
    if (!data) return null;

    const hasIssues = (data.issues || []).length > 0;

    return (
        <div className="border bg-[#0A0A0A]"
             style={{ borderColor: `${style.color}40` }}
             data-testid="bot-health-score">
            <button type="button" onClick={() => setExpanded(v => !v)}
                    className="w-full px-5 py-4 flex items-center gap-4 text-left hover:bg-[#FFFFFF05] transition-colors"
                    data-testid="health-score-toggle">
                <ScoreDial score={data.score} color={style.color} />
                <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <Activity className="w-4 h-4" style={{ color: style.color }} />
                        <div className="font-display text-base">Bot Health</div>
                        <span className="font-mono text-[10px] tracking-widest px-2 py-0.5"
                              style={{ color: style.color, background: `${style.color}15` }}
                              data-testid="health-score-status">
                            {style.label}
                        </span>
                    </div>
                    <div className="font-mono text-[11px] text-[#A1A1AA] mt-1 leading-snug">
                        {data.headline}
                    </div>
                </div>
                {hasIssues && (
                    <div className="font-mono text-[10px] tracking-widest text-[#A1A1AA] hidden sm:block">
                        {data.issues.length} ADVISOR{data.issues.length === 1 ? "Y" : "IES"}
                    </div>
                )}
                <ChevronDown className={`w-4 h-4 text-[#A1A1AA] transition-transform ${expanded ? "rotate-180" : ""}`} />
            </button>

            {expanded && hasIssues && (
                <div className="border-t border-[#1F1F1F] divide-y divide-[#1F1F1F]"
                     data-testid="health-score-issues">
                    {data.issues.map((issue, idx) => {
                        const Icon = ISSUE_ICON[issue.severity] || Info;
                        const color = ISSUE_COLOR[issue.severity] || "#A1A1AA";
                        return (
                            <div key={issue.code} className="px-5 py-3 flex items-start gap-3"
                                 data-testid={`health-issue-${issue.code}`}>
                                <Icon className="w-3.5 h-3.5 mt-0.5 shrink-0" style={{ color }} />
                                <div className="flex-1 min-w-0">
                                    <div className="font-mono text-xs text-white">{issue.label}</div>
                                    <div className="font-mono text-[10px] text-[#A1A1AA] leading-snug mt-1">
                                        {issue.fix}
                                    </div>
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
            {expanded && !hasIssues && (
                <div className="border-t border-[#1F1F1F] px-5 py-4 flex items-center gap-3"
                     data-testid="health-issues-none">
                    <ShieldCheck className="w-4 h-4 text-[#00FF41]" />
                    <div className="font-mono text-xs text-[#A1A1AA]">
                        No advisories — every subsystem is reporting clean.
                    </div>
                </div>
            )}
        </div>
    );
}

function ScoreDial({ score, color }) {
    const radius = 22;
    const circ = 2 * Math.PI * radius;
    const offset = circ * (1 - Math.max(0, Math.min(100, score)) / 100);
    return (
        <div className="relative w-14 h-14 shrink-0">
            <svg width="56" height="56" viewBox="0 0 56 56" className="-rotate-90">
                <circle cx="28" cy="28" r={radius}
                        stroke="#1F1F1F" strokeWidth="3" fill="none" />
                <circle cx="28" cy="28" r={radius}
                        stroke={color} strokeWidth="3" fill="none"
                        strokeDasharray={circ} strokeDashoffset={offset}
                        strokeLinecap="round"
                        style={{ transition: "stroke-dashoffset 0.6s ease" }} />
            </svg>
            <div className="absolute inset-0 flex items-center justify-center font-display text-base"
                 style={{ color }} data-testid="health-score-value">
                {score}
            </div>
        </div>
    );
}
