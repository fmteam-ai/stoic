import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { ShieldCheck, ShieldAlert, ShieldX, ShieldQuestion } from "lucide-react";

const STATUS_STYLE = {
    armed: { icon: ShieldCheck, cls: "text-[#00FF41]", badge: "border-[#00FF41]/30 text-[#00FF41]" },
    tripped: { icon: ShieldX, cls: "text-[#FF3B30]", badge: "border-[#FF3B30]/40 text-[#FF3B30]" },
    degraded: { icon: ShieldAlert, cls: "text-[#FFD700]", badge: "border-[#FFD700]/40 text-[#FFD700]" },
    error: { icon: ShieldQuestion, cls: "text-[#FF3B30]", badge: "border-[#FF3B30]/40 text-[#FF3B30]" },
};

export const RiskLayersCard = ({ accountId }) => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get(`/risk/layers${accountId ? `?account_id=${accountId}` : ""}`);
            setD(data);
        } catch { setHidden(true); }
    }, [accountId]);

    useEffect(() => {
        load();
        const id = setInterval(load, 30000);
        return () => clearInterval(id);
    }, [load]);

    if (hidden || !d) return null;
    const allArmed = d.tripped === 0 && d.degraded === 0;

    return (
        <div className={`border p-5 ${d.tripped > 0 ? "border-[#FF3B30]/30 bg-[#FF3B30]/5" : "border-[#1F1F1F] bg-[#0A0A0A]"}`}
            data-testid="risk-layers-card">
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <ShieldCheck className={`w-4 h-4 ${allArmed ? "text-[#00FF41]" : d.tripped > 0 ? "text-[#FF3B30]" : "text-[#FFD700]"}`} />
                    <span className="font-display font-bold text-sm text-white">Protection Layers</span>
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]"
                        data-testid="risk-layers-summary">
                        {d.layers.filter(l => l.status === "armed").length}/{d.layers.length} ARMED
                        {d.tripped > 0 && ` · ${d.tripped} TRIPPED`}
                        {d.degraded > 0 && ` · ${d.degraded} DEGRADED`}
                    </span>
                </div>
                {d.account_label && (
                    <span className="font-mono text-[9px] tracking-widest text-[#52525B]" data-testid="risk-layers-account">
                        {d.account_label.trim().toUpperCase()}
                    </span>
                )}
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 mt-4">
                {d.layers.map(l => {
                    const s = STATUS_STYLE[l.status] || STATUS_STYLE.error;
                    const Icon = s.icon;
                    return (
                        <div key={l.layer} className="flex items-start gap-2" data-testid={`risk-layer-${l.layer}`}
                            title={l.description}>
                            <Icon className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${s.cls}`} />
                            <div className="min-w-0">
                                <div className="flex items-center gap-2">
                                    <span className="font-mono text-[10px] tracking-widest text-white">{l.label.toUpperCase()}</span>
                                    <span className={`font-mono text-[8px] tracking-widest px-1 border ${s.badge}`}
                                        data-testid={`risk-layer-${l.layer}-status`}>
                                        {l.status.toUpperCase()}
                                    </span>
                                </div>
                                <div className="font-mono text-[9px] text-[#52525B] truncate">{l.detail}</div>
                            </div>
                        </div>
                    );
                })}
            </div>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-4">
                EACH LAYER RUNS INDEPENDENTLY — ONE FAILING CAN NEVER DISABLE THE OTHERS
            </div>
        </div>
    );
};
