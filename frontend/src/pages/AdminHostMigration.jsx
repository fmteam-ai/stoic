import { useCallback, useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api from "@/lib/api";
import { Loader2, RefreshCw } from "lucide-react";
import { NotEnabled } from "@/components/hostmigration/Shared";
import { TargetForm } from "@/components/hostmigration/TargetForm";
import { StepTimeline, LogPanel } from "@/components/hostmigration/StepTimeline";
import { GatePanel } from "@/components/hostmigration/GatePanel";

export default function AdminHostMigration() {
    const [state, setState] = useState(null);
    const [enabled, setEnabled] = useState(null);
    const [err, setErr] = useState(null);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/admin/host-migration/status");
            setState(data.state); setEnabled(true); setErr(null);
        } catch (e) {
            const code = e?.response?.data?.detail?.code;
            if (code === "migrator_not_enabled") setEnabled(false);
            else setErr(e?.response?.data?.detail?.message || e?.response?.data?.detail?.error || e.message);
        }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 4000);
        return () => clearInterval(t);
    }, [load]);

    const showForm = !state || state.status === "idle" || state.status === "aborted" || state.status === "done"
        || state.awaiting === "install" || (state.current_step === "preflight" && state.status !== "awaiting");
    const showRun = state && state.status !== "idle" && state.steps?.some(s => s.status !== "pending");

    return (
        <AppLayout>
            <PageHeader title="Admin · Host Migration" testid="admin-host-migration-header"
                subtitle="Move this STOIC install to a new server with a short freeze window — same domain, same release, every secret, backup and certificate carried over."
                action={<button onClick={load} data-testid="hm-refresh-btn"
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5">
                    <RefreshCw className="w-3.5 h-3.5" /> REFRESH</button>} />

            {enabled === null && !err && <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>}
            {enabled === false && <NotEnabled />}
            {err && <div className="text-xs font-mono text-[#FF3B30] border border-[#FF3B30]/40 p-3 mb-5" data-testid="hm-load-error">{err}</div>}

            {enabled && state && (
                <div className="space-y-5">
                    {showForm && <TargetForm state={state} onPreflight={load} />}
                    {showRun && (
                        <div className="grid lg:grid-cols-[1fr_1.2fr] gap-5">
                            <div className="space-y-5">
                                <StepTimeline state={state} />
                                <GatePanel state={state} refresh={load} />
                            </div>
                            <LogPanel log={state.log || []} />
                        </div>
                    )}
                </div>
            )}
        </AppLayout>
    );
}
