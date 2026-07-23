import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { ClipboardCheck } from "lucide-react";

const LABELS = {
    restart_recovery: "RESTART RECOVERY",
    reconnect_recovery: "RECONNECT RECOVERY",
    duplicate_commands: "DUPLICATE COMMANDS",
    stale_acknowledgements: "STALE ACKS",
    partial_fills: "PARTIAL FILLS",
    multi_deal_fills: "MULTI-DEAL FILLS",
    netting: "NETTING",
    hedging: "HEDGING",
    rejected_orders: "REJECTED ORDERS",
    emergency_close: "EMERGENCY CLOSE",
    manual_broker_intervention: "MANUAL INTERVENTION",
    long_running_broker_sync: "LONG BROKER SYNC",
};

function ModeDot({ rec }) {
    const cls = !rec ? "bg-[#1F1F1F] text-[#52525B]"
        : rec.status === "pass" ? "bg-[#00FF41]/15 text-[#00FF41]"
            : "bg-[#FF3B30]/15 text-[#FF3B30]";
    return (
        <span className={`font-mono text-[9px] px-1.5 py-0.5 ${cls}`}
            title={rec ? `${rec.status} · ${rec.notes}` : "no evidence"}>
            {!rec ? "—" : rec.status === "pass" ? "PASS" : "FAIL"}
        </span>
    );
}

export const ValidationCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/ops/validation");
            setD(data);
        } catch { setHidden(true); }
    }, []);
    useEffect(() => { load(); }, [load]);

    if (hidden || !d) return null;
    const done = d.scenarios.filter(s => s.complete).length;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="validation-card">
            <div className="flex items-center gap-2 flex-wrap">
                <ClipboardCheck className={`w-4 h-4 ${d.complete ? "text-[#00FF41]" : "text-[#FFD700]"}`} />
                <span className="font-display font-bold text-sm text-white">MT5 Validation Campaign</span>
                <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${d.complete ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FFD700]/40 text-[#FFD700]"}`}
                    data-testid="validation-progress">
                    {done}/{d.scenarios.length} SCENARIOS
                </span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1.5 mt-3">
                {d.scenarios.map(s => (
                    <div key={s.scenario} className="flex items-center justify-between gap-2"
                        data-testid={`validation-${s.scenario}`}>
                        <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">{LABELS[s.scenario] || s.scenario}</span>
                        <span className="flex items-center gap-1.5">
                            <span className="font-mono text-[8px] text-[#52525B]">NET</span>
                            <ModeDot rec={s.modes.netting} />
                            <span className="font-mono text-[8px] text-[#52525B]">HDG</span>
                            <ModeDot rec={s.modes.hedging} />
                        </span>
                    </div>
                ))}
            </div>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-3">
                EVIDENCE IS RECORDED VIA POST /api/ops/validation/&#123;scenario&#125; — SEE docs/MT5_VALIDATION_CAMPAIGN.md
            </div>
        </div>
    );
};
