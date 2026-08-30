import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { X, Route } from "lucide-react";

const SECTIONS = [
    ["why_opened", "WHY WAS IT OPENED?"],
    ["why_this_time", "WHY AT THAT TIME?"],
    ["why_this_size", "WHY THAT SIZE?"],
    ["why_this_stop", "WHY THAT STOP?"],
    ["why_this_target", "WHY THAT TARGET?"],
    ["what_changed", "WHAT CHANGED?"],
    ["why_closed", "WHY WAS IT CLOSED?"],
];

const fmtTime = (v) => {
    if (!v) return "";
    try { return new Date(v).toISOString().replace("T", " ").slice(5, 19); }
    catch { return String(v).slice(0, 19); }
};

export const TraceModal = ({ trade, onClose }) => {
    const [d, setD] = useState(null);
    const [tl, setTl] = useState(null);
    const [err, setErr] = useState(null);

    useEffect(() => {
        api.get(`/trades/${trade.id}/trace`)
            .then(r => setD(r.data))
            .catch(e => setErr(formatApiError(e)));
        api.get(`/trades/${trade.id}/timeline`)
            .then(r => setTl(r.data))
            .catch(() => {});
    }, [trade.id]);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
            onClick={onClose} data-testid="trace-modal">
            <div className="w-full max-w-2xl max-h-[85vh] overflow-y-auto border border-[#1F1F1F] bg-[#0A0A0A]"
                onClick={e => e.stopPropagation()}>
                <div className="sticky top-0 flex items-center gap-2 border-b border-[#1F1F1F] bg-[#0A0A0A] px-5 py-3">
                    <Route className="w-4 h-4 text-[#FFD700]" />
                    <span className="font-display font-bold text-sm text-white">
                        Execution Trace — {trade.action} {trade.symbol}
                    </span>
                    <button onClick={onClose} data-testid="trace-close"
                        className="ml-auto text-[#71717A] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                {err && (
                    <div className="px-5 py-3 font-mono text-xs text-[#FF3B30]" data-testid="trace-error">{err}</div>
                )}
                {!d && !err && (
                    <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="trace-loading">
                        COMPOSING TRACE…
                    </div>
                )}
                {d && (
                    <div className="p-5 space-y-4">
                        {tl && (
                            <div data-testid="trace-decision-timeline">
                                <div className="font-mono text-[9px] tracking-widest text-[#38BDF8]">DECISION TIMELINE</div>
                                <div className="flex items-center gap-1 flex-wrap mt-1.5">
                                    {tl.stages.map((s, i) => (
                                        <span key={s.stage} className="flex items-center gap-1" title={s.summary || ""}>
                                            <span data-testid={`timeline-stage-${s.stage}`}
                                                className={`font-mono text-[8px] tracking-widest px-1.5 py-0.5 border ${s.status === "complete"
                                                    ? "border-[#00FF41]/40 text-[#00FF41]"
                                                    : "border-[#3F3F46] text-[#52525B]"}`}>
                                                {s.stage.toUpperCase().replace("_", "")}
                                            </span>
                                            {i < tl.stages.length - 1 && <span className="text-[#3F3F46] text-[9px]">→</span>}
                                        </span>
                                    ))}
                                </div>
                            </div>
                        )}
                        {SECTIONS.map(([key, title]) => {
                            const s = d[key];
                            if (!s) return null;
                            return (
                                <div key={key} data-testid={`trace-${key}`}>
                                    <div className="font-mono text-[9px] tracking-widest text-[#FFD700]">{title}</div>
                                    <div className="font-mono text-[11px] text-white mt-1 leading-relaxed">{s.answer}</div>
                                    {key === "what_changed" && s.timeline?.length > 0 && (
                                        <div className="mt-2 space-y-1 border-l border-[#2A2A2A] pl-3">
                                            {s.timeline.map((e, i) => (
                                                <div key={i} className="font-mono text-[10px]" data-testid={`trace-event-${i}`}>
                                                    <span className="text-[#52525B]">{fmtTime(e.at)} </span>
                                                    <span className="text-[#00FF41]">{e.kind}</span>
                                                    <span className="text-[#A1A1AA]"> — {e.detail}</span>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                    {key !== "what_changed" && s.evidence?.reasoning && (
                                        <div className="font-mono text-[10px] text-[#71717A] mt-1">{s.evidence.reasoning}</div>
                                    )}
                                </div>
                            );
                        })}
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B] pt-2 border-t border-[#141414]">
                            EVERY ANSWER CITES DATA RECORDED AT DECISION TIME — NOTHING IS RECONSTRUCTED AFTER THE FACT.
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};
