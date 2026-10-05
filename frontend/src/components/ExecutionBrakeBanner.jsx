import { useCallback, useEffect, useState } from "react";
import { Ban, Unlock } from "lucide-react";
import api from "@/lib/api";
import { toast } from "sonner";

// A7d — execution-health brake: shown while any of the user's accounts has
// NEW entries paused after repeated late fills / rejects / slippage vetoes /
// duplicate tickets. Manual release is a step-up action.
export function ExecutionBrakeBanner() {
    const [rows, setRows] = useState([]);
    const [busy, setBusy] = useState(null);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/accounts");
            const list = Array.isArray(data) ? data : (data?.accounts || []);
            setRows(list.filter((a) => a?.execution_brake?.active));
        } catch { /* banner is best-effort */ }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 60_000);
        return () => clearInterval(t);
    }, [load]);

    const release = async (id) => {
        setBusy(id);
        try {
            await api.post(`/accounts/${id}/execution-brake/release`);
            toast.success("Execution brake released — new entries resume on the next scan.");
            await load();
        } catch (e) {
            toast.error(e?.response?.data?.detail?.message || "Release failed");
        } finally {
            setBusy(null);
        }
    };

    if (!rows.length) return null;
    return (
        <div className="space-y-2" data-testid="execution-brake-banner">
            {rows.map((a) => (
                <div key={a.id} className="flex flex-wrap items-center gap-3 px-4 py-3 border border-[#FF3B30]/50 bg-[#FF3B30]/5"
                     data-testid={`execution-brake-row-${a.id}`}>
                    <span className="inline-flex items-center gap-1.5 px-2 py-0.5 bg-[#FF3B30] text-black text-[10px] font-mono tracking-widest">
                        <Ban className="w-3 h-3" /> EXECUTION BRAKE
                    </span>
                    <div className="flex-1 min-w-[200px] text-xs text-[#E4E4E7] font-mono">
                        <span className="text-white">{a.label || a.login || a.broker}</span>
                        <span className="text-[#A1A1AA]"> · {a.execution_brake.reason}</span>
                        <div className="text-[10px] text-[#71717A] mt-0.5">
                            New entries paused; managed exits continue. Auto-release after a clean hour
                            {a.execution_brake.release_after ? ` (earliest ${String(a.execution_brake.release_after).slice(11, 16)} UTC)` : ""}.
                        </div>
                    </div>
                    <button onClick={() => release(a.id)} disabled={busy === a.id}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#FF3B30]/60 text-[#FF3B30] hover:bg-[#FF3B30]/10 text-[10px] font-mono tracking-widest disabled:opacity-50"
                            data-testid={`execution-brake-release-${a.id}`}>
                        <Unlock className="w-3 h-3" /> {busy === a.id ? "RELEASING…" : "RELEASE (2FA)"}
                    </button>
                </div>
            ))}
        </div>
    );
}

export default ExecutionBrakeBanner;
