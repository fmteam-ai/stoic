import { useCallback, useEffect, useState } from "react";
import { Ban, Unlock, ShieldAlert } from "lucide-react";
import api from "@/lib/api";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";

// A7d (operator spec) — shown while any account has NEW entries paused after two late
// fills in 24 h. There is no auto-release: only an ADMIN can resume, after checking the
// EA / VPS (step-up). Owners see the brake and who to contact.
export function ExecutionBrakeBanner() {
    const { user } = useAuth();
    const isAdmin = user?.role === "admin";
    const [rows, setRows] = useState([]);
    const [busy, setBusy] = useState(null);

    const load = useCallback(async () => {
        try {
            // R-5 — admins see EVERY braked account (any owner); owners see their own.
            if (isAdmin) {
                const { data } = await api.get("/admin/execution-brakes");
                setRows(data?.accounts || []);
                return;
            }
            const { data } = await api.get("/accounts");
            const list = Array.isArray(data) ? data : (data?.accounts || []);
            setRows(list.filter((a) => a?.execution_brake?.active));
        } catch { /* banner is best-effort */ }
    }, [isAdmin]);

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
            toast.error(e?.response?.data?.detail?.message || e?.response?.data?.detail || "Release failed");
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
                        {a.owner_email && <span className="text-[#71717A]" data-testid={`execution-brake-owner-${a.id}`}> · {a.owner_email}</span>}
                        <span className="text-[#A1A1AA]"> · {a.execution_brake.reason}</span>
                        <div className="text-[10px] text-[#71717A] mt-0.5" data-testid={`execution-brake-note-${a.id}`}>
                            New entries paused since {String(a.execution_brake.since || "").slice(11, 16)} UTC; managed exits continue.
                            No automatic release — an admin resumes after checking the EA / VPS.
                        </div>
                    </div>
                    {isAdmin ? (
                        <button onClick={() => release(a.id)} disabled={busy === a.id}
                                className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#FF3B30]/60 text-[#FF3B30] hover:bg-[#FF3B30]/10 text-[10px] font-mono tracking-widest disabled:opacity-50"
                                data-testid={`execution-brake-release-${a.id}`}>
                            <Unlock className="w-3 h-3" /> {busy === a.id ? "RESUMING…" : "ADMIN RESUME (2FA)"}
                        </button>
                    ) : (
                        <span className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] text-[10px] font-mono tracking-widest"
                              data-testid={`execution-brake-contact-${a.id}`}>
                            <ShieldAlert className="w-3 h-3" /> ADMIN RESUME REQUIRED
                        </span>
                    )}
                </div>
            ))}
        </div>
    );
}

export default ExecutionBrakeBanner;
