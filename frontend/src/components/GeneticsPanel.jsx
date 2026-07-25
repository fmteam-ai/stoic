import { useEffect, useState } from "react";
import api from "@/lib/api";

const fmtPnl = (v) => v == null ? "—" : `${v >= 0 ? "+$" : "-$"}${Math.abs(v).toFixed(2)}`;
const pnlCls = (v) => v > 0 ? "text-[#00FF41]" : v < 0 ? "text-[#FF3B30]" : "text-[#A1A1AA]";

const KIND_CLS = {
    governance: "text-[#0099FF] border-[#0099FF]/40",
    auto_guard: "text-[#FFD700] border-[#FFD700]/40",
    research: "text-[#00FF41] border-[#00FF41]/40",
    tuning: "text-[#A78BFA] border-[#A78BFA]/40",
};

export function GeneticsPanel() {
    const [g, setG] = useState(null);
    const [showAll, setShowAll] = useState(false);
    useEffect(() => {
        api.get("/genetics/lineage").then(({ data }) => setG(data)).catch(() => {});
    }, []);
    const versions = g?.versions || [];
    const events = g?.events || [];
    const shown = showAll ? events : events.slice(0, 8);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="genetics-panel">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                STRATEGY GENETICS · VERSION LINEAGE
            </div>
            <div className="font-mono text-[10px] text-[#3F3F46] mb-3">
                What changed, who approved it, and whether it helped — measured from version-stamped trades. Policies: {g?.policies?.risk_policy} · {g?.policies?.execution_policy} · {g?.policies?.feature_schema}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 mb-3">
                {versions.map((v) => (
                    <div key={v.engine} className="border border-[#141414] p-2"
                        data-testid={`genetics-version-${v.engine}`}>
                        <div className="font-mono text-[10px] text-white">{v.current_version}</div>
                        {v.performance ? (
                            <div className="font-mono text-[10px] text-[#52525B] mt-0.5">
                                {v.performance.n} trades · {v.performance.win_rate}% WR ·{" "}
                                <span className={pnlCls(v.performance.pnl)}>{fmtPnl(v.performance.pnl)}</span>
                                {v.performance.profit_factor != null && <span> · PF {v.performance.profit_factor}</span>}
                            </div>
                        ) : (
                            <div className="font-mono text-[10px] text-[#3F3F46] mt-0.5">no stamped trades yet</div>
                        )}
                    </div>
                ))}
            </div>
            {shown.length ? shown.map((e, i) => (
                <div key={i} className="flex flex-wrap items-start gap-2 py-1.5 border-t border-[#141414]"
                    data-testid={`genetics-event-${i}`}>
                    <span className={`font-mono text-[9px] px-1.5 py-0.5 border shrink-0 ${KIND_CLS[e.kind] || "text-[#A1A1AA] border-[#1F1F1F]"}`}>
                        {e.kind.toUpperCase()}
                    </span>
                    <div className="min-w-0 flex-1">
                        <div className="font-mono text-[10px] text-[#A1A1AA] truncate">{e.what}</div>
                        {e.why && <div className="font-mono text-[9px] text-[#3F3F46] truncate">{e.why}</div>}
                    </div>
                    <div className="font-mono text-[9px] text-[#52525B] shrink-0 text-right">
                        <div>{e.who} · {e.status}</div>
                        <div>{String(e.ts).slice(0, 16).replace("T", " ")}</div>
                        {e.helped != null && (
                            <div className={pnlCls(Number(e.helped))}>impact {typeof e.helped === "number" ? e.helped.toFixed?.(2) ?? e.helped : String(e.helped)}</div>
                        )}
                    </div>
                </div>
            )) : (
                <div className="font-mono text-xs text-[#52525B]" data-testid="genetics-empty">
                    No recorded strategy changes yet — the genome is at its baseline.
                </div>
            )}
            {events.length > 8 && (
                <button onClick={() => setShowAll(!showAll)} data-testid="genetics-show-all"
                    className="mt-2 font-mono text-[10px] text-[#0099FF] hover:text-white">
                    {showAll ? "SHOW LESS" : `SHOW ALL ${events.length} EVENTS`}
                </button>
            )}
        </div>
    );
}
