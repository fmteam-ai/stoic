import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { History, RotateCcw } from "lucide-react";

export function ConfigVersionsCard({ accountQuery }) {
    const [data, setData] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(() => {
        api.get(`/config/versions${accountQuery || ""}`)
            .then(({ data }) => setData(data)).catch(() => {});
    }, [accountQuery]);
    useEffect(() => { load(); }, [load]);

    const doRollback = async () => {
        setBusy(true);
        try {
            const { data: r } = await api.post(`/config/rollback${accountQuery || ""}`);
            toast.success(r.mode_guard_applied
                ? "Rolled back — operational mode kept (promotions need the explicit gate)"
                : "Configuration rolled back to the previous version");
            load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const versions = data?.versions || [];
    if (!versions.length) return null;
    const canRollback = Boolean(data?.pointer?.previous_version_id);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="config-versions-card">
            <div className="flex items-center gap-2 mb-1">
                <History size={13} className="text-[#0099FF]" />
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    IMMUTABLE CONFIG VERSIONS · ATOMIC ROLLBACK
                </span>
                <button onClick={doRollback} disabled={!canRollback || busy} data-testid="config-rollback-btn"
                    className="ml-auto flex items-center gap-1.5 font-mono text-[10px] tracking-widest px-2.5 py-1 border border-[#FFD700]/40 text-[#FFD700] hover:bg-[#FFD700]/10 disabled:opacity-30">
                    <RotateCcw size={11} /> {busy ? "ROLLING BACK…" : "ROLLBACK"}
                </button>
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-2">
                Every change writes an immutable version and advances the active pointer. Rollback re-applies the previous version — it can never raise the operational mode.
            </div>
            {versions.slice(0, 6).map((v) => (
                <div key={v.id} className="flex items-center gap-2 py-1 border-t border-[#141414]"
                    data-testid={`config-version-${v.id}`}>
                    {v.is_active && (
                        <span className="font-mono text-[8px] px-1.5 py-0.5 border border-[#00FF41]/40 text-[#00FF41]">ACTIVE</span>
                    )}
                    {v.is_rollback_target && (
                        <span className="font-mono text-[8px] px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700]">ROLLBACK TARGET</span>
                    )}
                    <span className="font-mono text-[10px] text-[#A1A1AA] truncate">{v.label}</span>
                    <span className="font-mono text-[9px] text-[#52525B]">{v.source}</span>
                    <span className="ml-auto font-mono text-[9px] text-[#3F3F46] shrink-0">
                        {String(v.config_hash).slice(0, 8)} · {String(v.created_at).slice(0, 16).replace("T", " ")}
                    </span>
                </div>
            ))}
        </div>
    );
}
