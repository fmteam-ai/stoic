import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Rocket, ChevronRight, Check, X } from "lucide-react";

const STAGE_LABELS = {
    internal_shadow: "SHADOW",
    demo_broker: "DEMO",
    small_live: "SMALL LIVE",
    larger_live: "LARGER LIVE",
    production: "PRODUCTION",
};

export const StageCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/ops/stage");
            setD(data);
        } catch { setHidden(true); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const promote = async () => {
        setBusy(true); setErr("");
        try { await api.post("/ops/stage/promote", {}); await load(); }
        catch (e) {
            const det = e.response?.data?.detail;
            setErr(typeof det === "string" ? det : det?.detail || "Promotion blocked — criteria not met.");
        } finally { setBusy(false); }
    };

    if (hidden || !d) return null;
    const activeIdx = d.stages.indexOf(d.stage);

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="stage-card">
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <Rocket className="w-4 h-4 text-[#00FF41]" />
                    <span className="font-display font-bold text-sm text-white">Live Deployment Stage</span>
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#00FF41]/40 text-[#00FF41]"
                        data-testid="stage-current">
                        {STAGE_LABELS[d.stage] || d.stage}
                    </span>
                    {d.enforcement && (
                        <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700]">ENFORCED</span>
                    )}
                </div>
                {d.next_stage && (
                    <button onClick={promote} disabled={busy || !d.can_promote}
                        data-testid="stage-promote-btn"
                        className="font-mono text-[10px] tracking-widest border border-[#00FF41]/40 text-[#00FF41] px-3 py-1.5 hover:bg-[#00FF41]/10 disabled:opacity-40 disabled:cursor-not-allowed">
                        PROMOTE → {STAGE_LABELS[d.next_stage] || d.next_stage}
                    </button>
                )}
            </div>

            <div className="flex items-center gap-1 mt-3 flex-wrap" data-testid="stage-stepper">
                {d.stages.map((s, i) => (
                    <span key={s} className="flex items-center gap-1">
                        <span className={`font-mono text-[9px] tracking-widest px-2 py-1 border ${i < activeIdx ? "border-[#00FF41]/20 text-[#00FF41]/50"
                            : i === activeIdx ? "border-[#00FF41] text-[#00FF41] bg-[#00FF41]/10"
                                : "border-[#1F1F1F] text-[#52525B]"}`}>
                            {STAGE_LABELS[s]}
                        </span>
                        {i < d.stages.length - 1 && <ChevronRight className="w-3 h-3 text-[#52525B]" />}
                    </span>
                ))}
            </div>

            {d.promotion_criteria?.length > 0 && (
                <div className="mt-3 space-y-1" data-testid="stage-criteria">
                    {d.promotion_criteria.map(c => (
                        <div key={c.name} className="flex items-center gap-2 font-mono text-[10px] tracking-widest">
                            {c.ok ? <Check className="w-3 h-3 text-[#00FF41]" /> : <X className="w-3 h-3 text-[#FF3B30]" />}
                            <span className={c.ok ? "text-[#A1A1AA]" : "text-[#FF3B30]"}>
                                {c.name.replace(/_/g, " ").toUpperCase()} · {c.detail}
                            </span>
                        </div>
                    ))}
                </div>
            )}

            {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono mt-3" data-testid="stage-error">{err}</div>}
        </div>
    );
};
