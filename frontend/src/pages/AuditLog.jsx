import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ShieldCheck, RefreshCw } from "lucide-react";

const ACTION_TONE = {
    step_up_verified: "text-[#00FF41]",
    step_up_failed: "text-[#FF3B30]",
    live_activation: "text-[#FFB000]",
    risk_raise: "text-[#FFB000]",
    panic_release: "text-[#FFB000]",
    panic_triggered: "text-[#FF3B30]",
    api_key_created: "text-[#0099FF]",
};

export default function AuditLog() {
    const [items, setItems] = useState(null);
    const [filter, setFilter] = useState("all");
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/auth/audit?limit=200");
            setItems(data);
        } catch (e) { setErr(formatApiError(e)); }
    }, []);

    useEffect(() => { load(); }, [load]);

    const actions = [...new Set((items || []).map(i => i.action))];
    const shown = (items || []).filter(i => filter === "all" || i.action === filter);

    return (
        <AppLayout>
            <PageHeader title="Audit Log"
                subtitle="Append-only security trail — every sensitive action, step-up verification and panic event."
                testid="audit-log-header"
                action={
                    <button onClick={load} data-testid="audit-log-refresh"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333] text-xs font-mono tracking-widest">
                        <RefreshCw className="w-3.5 h-3.5" /> REFRESH
                    </button>
                } />
            <div className="p-4 md:p-8 space-y-4">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                <div className="flex items-center gap-2 flex-wrap" data-testid="audit-filters">
                    {["all", ...actions].map(a => (
                        <button key={a} onClick={() => setFilter(a)}
                            data-testid={`audit-filter-${a}`}
                            className={`px-2.5 py-1 font-mono text-[10px] tracking-widest border transition-colors ${
                                filter === a ? "border-[#00FF41]/60 text-[#00FF41]"
                                    : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333]"}`}>
                            {a.toUpperCase()}
                        </button>
                    ))}
                </div>

                <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="audit-log-table">
                    {items === null && (
                        <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                    )}
                    {items !== null && shown.length === 0 && (
                        <div className="p-8 text-center" data-testid="audit-log-empty">
                            <ShieldCheck className="w-6 h-6 text-[#52525B] mx-auto mb-2" />
                            <div className="text-sm text-[#A1A1AA]">No audit events recorded yet.</div>
                        </div>
                    )}
                    {shown.map((e, i) => (
                        <div key={i} className="px-4 py-2.5 border-b border-[#1F1F1F] last:border-0 flex items-center gap-3 flex-wrap"
                            data-testid={`audit-event-${i}`}>
                            <span className={`font-mono text-xs w-44 ${ACTION_TONE[e.action] || "text-white"}`}>
                                {e.action}
                            </span>
                            {e.step_up_verified && (
                                <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#00FF41]/40 text-[#00FF41]">
                                    2FA VERIFIED
                                </span>
                            )}
                            <span className="font-mono text-[10px] text-[#A1A1AA] truncate max-w-md">
                                {e.detail && Object.keys(e.detail).length
                                    ? Object.entries(e.detail).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("|") : v}`).join(" · ")
                                    : ""}
                            </span>
                            <span className="ml-auto font-mono text-[10px] text-[#52525B]">
                                {e.ip || "—"} · {e.at ? new Date(e.at).toLocaleString() : "—"}
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        </AppLayout>
    );
}
