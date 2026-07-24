import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Scale, Check, X } from "lucide-react";

const STATUS_COLOR = {
    pending: "#FFB000", auto_applied: "#00FF41",
    approved: "#38BDF8", rejected: "#52525B",
};

export const GovernanceCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(() => {
        api.get("/governance/changes?limit=20")
            .then(({ data }) => setD(data))
            .catch(() => setHidden(true));
    }, []);

    useEffect(() => { load(); }, [load]);

    if (hidden || !d) return null;

    const resolve = async (id, action) => {
        try {
            const { data } = await api.post(`/governance/changes/${id}/${action}`);
            if (data.ok) { toast.success(`Change ${data.status}`); load(); }
            else toast.error(data.error || "Failed");
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const fmt = (v) => typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="governance-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <Scale className="w-4 h-4 text-[#FFB000]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">CHANGE GOVERNANCE</div>
                        <div className="font-display font-bold text-base tracking-tight">
                            Auto-conservative · aggressive needs your approval
                        </div>
                    </div>
                </div>
                {d.pending_count > 0 && (
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#FFB000]/40 text-[#FFB000]"
                        data-testid="governance-pending-count">
                        {d.pending_count} AWAITING APPROVAL
                    </span>
                )}
            </div>
            {d.changes.length === 0 ? (
                <div className="p-6 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="governance-empty">
                    NO GOVERNED CHANGES YET — BOT PROPOSALS APPEAR HERE
                </div>
            ) : (
                <div className="divide-y divide-[#141414]">
                    {d.changes.map(c => (
                        <div key={c.id} className="px-4 py-3" data-testid={`governance-change-${c.id}`}>
                            <div className="flex items-center gap-2 flex-wrap">
                                <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border"
                                    style={{ color: STATUS_COLOR[c.status], borderColor: `${STATUS_COLOR[c.status]}55` }}>
                                    {c.status?.replace("_", " ").toUpperCase()}
                                </span>
                                <span className="font-mono text-xs text-white">{c.field}</span>
                                <span className="font-mono text-[10px] text-[#A1A1AA]">
                                    {fmt(c.old_value)} → {fmt(c.new_value)}
                                </span>
                                <span className="font-mono text-[9px] text-[#52525B] tracking-widest ml-auto">
                                    {c.classification?.toUpperCase()} · {c.source?.toUpperCase()}
                                </span>
                            </div>
                            {c.detail && <div className="text-[10px] text-[#71717A] mt-1">{c.detail}</div>}
                            {c.status === "pending" && (
                                <div className="flex items-center gap-2 mt-2">
                                    <button onClick={() => resolve(c.id, "approve")}
                                        data-testid={`governance-approve-${c.id}`}
                                        className="flex items-center gap-1 px-3 py-1.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 text-[10px] font-mono tracking-widest">
                                        <Check className="w-3 h-3" /> APPROVE
                                    </button>
                                    <button onClick={() => resolve(c.id, "reject")}
                                        data-testid={`governance-reject-${c.id}`}
                                        className="flex items-center gap-1 px-3 py-1.5 border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 text-[10px] font-mono tracking-widest">
                                        <X className="w-3 h-3" /> REJECT
                                    </button>
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
            <div className="px-4 py-2 font-mono text-[9px] tracking-widest text-[#52525B] border-t border-[#141414]">
                RISK-REDUCING CHANGES AUTO-APPLY · RISK-INCREASING PROPOSALS QUEUE HERE WITH EVIDENCE
            </div>
        </div>
    );
};
