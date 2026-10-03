import { useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Lock, Unlock, Loader2, Siren } from "lucide-react";

export const GlobalPanicCard = ({ onDone }) => {
    const [busy, setBusy] = useState(null);
    const run = async (kind) => {
        const confirmMsg = kind === "panic"
            ? "ADMIN-WIDE PANIC: stop every bot and scalp runner, cancel pending orders, request closes and LOCK every account. Only an admin can release it. Continue?"
            : "Release the admin-wide PANIC lock on all accounts? Users' own PANIC locks stay in place.";
        if (!window.confirm(confirmMsg)) return;
        setBusy(kind);
        try {
            const url = kind === "panic" ? "/admin/panic" : "/admin/panic/release";
            const { data } = await api.post(url);
            toast.success(kind === "panic"
                ? `PANIC engaged · ${data.accounts_locked ?? 0} account(s) LOCKED · ${data.bots_disabled ?? 0} bot(s) stopped`
                : `Admin PANIC released on ${data.accounts_unlocked ?? 0} account(s)`);
            onDone?.();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setBusy(null);
        }
    };
    return (
        <div className="border border-[#FF3B30]/40 bg-[#0A0A0A] p-4 flex flex-wrap items-center gap-3" data-testid="global-panic-card">
            <Siren className="w-4 h-4 text-[#FF3B30]" />
            <div className="flex-1 min-w-[220px]">
                <div className="text-xs font-mono tracking-widest text-[#FF3B30]">ADMIN-WIDE PANIC</div>
                <div className="text-[11px] text-[#71717A]">Locks every account (scope: platform). Users cannot release it — only the button on the right. Step-up required.</div>
            </div>
            <button onClick={() => run("panic")} disabled={!!busy} data-testid="global-panic-btn"
                className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#FF3B30] text-[#FF3B30] hover:bg-[#FF3B30] hover:text-black flex items-center gap-1.5 disabled:opacity-50">
                {busy === "panic" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Lock className="w-3.5 h-3.5" />} PANIC ALL
            </button>
            <button onClick={() => run("release")} disabled={!!busy} data-testid="global-panic-release-btn"
                className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5 disabled:opacity-50">
                {busy === "release" ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Unlock className="w-3.5 h-3.5" />} RELEASE ADMIN LOCK
            </button>
        </div>
    );
};
