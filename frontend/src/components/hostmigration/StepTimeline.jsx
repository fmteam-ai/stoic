import { useEffect, useRef, useState } from "react";
import { Loader2, CheckCircle2, XCircle, Circle, Clock } from "lucide-react";
import { STATUS_CLS } from "./Shared";

const Icon = ({ s }) => s === "running" ? <Loader2 className="w-4 h-4 animate-spin" />
    : s === "done" ? <CheckCircle2 className="w-4 h-4" />
    : s === "failed" ? <XCircle className="w-4 h-4" /> : <Circle className="w-4 h-4" />;

export function StepTimeline({ state }) {
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-5" data-testid="hm-timeline">
            <div className="flex items-center justify-between mb-4">
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">MIGRATION {state.id || ""}
                    {state.simulated && <span className="ml-2 px-1.5 py-0.5 border border-[#FFB020]/50 text-[#FFB020]" data-testid="hm-simulated-badge">REHEARSAL · NOTHING IS EXECUTED</span>}</span>
                <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${STATUS_CLS[state.status === "awaiting" ? "pending" : state.status === "aborted" ? "failed" : state.status] || STATUS_CLS.pending}`}
                    data-testid="hm-status">{state.status.toUpperCase()}{state.awaiting ? ` · WAITING FOR ${state.awaiting.replaceAll("_", " ").toUpperCase()}` : ""}</span>
            </div>
            <ol className="space-y-2">
                {state.steps.map((s, i) => (
                    <li key={s.id} className={`flex items-start gap-3 text-sm ${s.status === "pending" ? "text-[#52525B]" : "text-[#E4E4E7]"}`} data-testid={`hm-step-${s.id}`}>
                        <span className={`mt-0.5 ${STATUS_CLS[s.status]?.split(" ")[0]}`}><Icon s={s.status} /></span>
                        <div className="flex-1">
                            <div className="flex items-center gap-2"><span className="font-mono text-[10px] text-[#52525B]">{i + 1}</span>{s.label}</div>
                            {s.ended_at && s.started_at && (
                                <div className="text-[10px] font-mono text-[#52525B] flex items-center gap-1"><Clock className="w-3 h-3" />
                                    {Math.max(1, Math.round((new Date(s.ended_at) - new Date(s.started_at)) / 1000))}s</div>)}
                        </div>
                    </li>
                ))}
            </ol>
            {state.downtime_started_at && state.status !== "aborted" && (
                <div className="mt-4 text-xs font-mono text-[#FFB020]" data-testid="hm-downtime">
                    DOWNTIME since {new Date(state.downtime_started_at).toLocaleTimeString()} — source API + workers are stopped
                </div>
            )}
        </div>
    );
}

export function LogPanel({ log }) {
    const ref = useRef(null);
    const [follow, setFollow] = useState(true);
    useEffect(() => { if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [log, follow]);
    return (
        <div className="bg-[#050505] border border-[#1F1F1F]" data-testid="hm-log">
            <div className="px-4 py-2 border-b border-[#1F1F1F] flex items-center justify-between">
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">LIVE LOG · {log.length} lines</span>
                <label className="text-[10px] font-mono text-[#52525B] flex items-center gap-1.5">
                    <input type="checkbox" checked={follow} onChange={e => setFollow(e.target.checked)} data-testid="hm-log-follow" /> follow
                </label>
            </div>
            <div ref={ref} className="h-72 overflow-auto p-3 text-[11px] font-mono leading-relaxed">
                {log.length === 0 && <span className="text-[#3F3F46]">no output yet</span>}
                {log.map((l, i) => (
                    <div key={i} className={l.line.startsWith("!!") ? "text-[#FF3B30]" : l.line.startsWith("$") ? "text-[#00FF41]/80" : "text-[#A1A1AA]"}>
                        <span className="text-[#3F3F46]">{l.at.slice(11, 19)} {l.step.padEnd(13)}</span> {l.line}
                    </div>
                ))}
            </div>
        </div>
    );
}
