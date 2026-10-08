import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { CheckCircle2, Circle, Loader2, AlertTriangle, XOctagon, Copy, RotateCcw } from "lucide-react";
import { toast } from "sonner";

/**
 * Installer progress — five derived steps for one account's VPS terminal
 * (token → installer → .ex5 → heartbeat/WebRequest → identity). Read-only, polls
 * GET /api/setup/install-progress/{id} every 5 s so a stuck pairing is visible at a glance.
 */
const STYLE = {
    done: { icon: CheckCircle2, cls: "text-[#00FF41]" },
    waiting: { icon: Loader2, cls: "text-[#FFD700] animate-spin" },
    blocked: { icon: XOctagon, cls: "text-[#FF3B30]" },
    warn: { icon: AlertTriangle, cls: "text-[#FFB020]" },
    pending: { icon: Circle, cls: "text-[#3F3F46]" },
};

export const STATE_CHIP = {
    ready: { label: "VPS READY", cls: "border-[#00FF41]/50 text-[#00FF41]" },
    in_progress: { label: "PAIRING…", cls: "border-[#FFD700]/50 text-[#FFD700]" },
    attention: { label: "VPS ATTENTION", cls: "border-[#FFB020]/50 text-[#FFB020]" },
    blocked: { label: "VPS BLOCKED", cls: "border-[#FF3B30]/60 text-[#FF3B30]" },
    not_started: { label: "NOT PAIRED", cls: "border-[#3F3F46] text-[#71717A]" },
};

export function useInstallProgress(accountId, intervalMs = 5000) {
    const [data, setData] = useState(null);
    const fetchIt = useCallback(async () => {
        if (document.visibilityState === "hidden") return;          // N105-6 — no polling in hidden tabs
        try {
            const { data } = await api.get(`/setup/install-progress/${accountId}`);
            setData(data);
        } catch { /* keep the last snapshot */ }
    }, [accountId]);
    useEffect(() => {
        setData(null);                                                // N105-6 — never flash the previous account's data
        fetchIt();
        const t = setInterval(fetchIt, intervalMs);
        document.addEventListener("visibilitychange", fetchIt);
        return () => { clearInterval(t); document.removeEventListener("visibilitychange", fetchIt); };
    }, [fetchIt, intervalMs]);
    return data;
}

export function InstallProgressChip({ accountId }) {
    const p = useInstallProgress(accountId, 15000);
    if (!p || p.state === "not_started") return null;
    const chip = STATE_CHIP[p.state] || STATE_CHIP.not_started;
    const onClick = () => document.getElementById(`install-progress-${accountId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    return (
        <button type="button" onClick={onClick} data-testid={`install-progress-chip-${accountId}`} title={p.headline}
            className={`inline-flex items-center gap-1 px-2 py-0.5 border font-mono text-[10px] tracking-widest hover:bg-white/5 ${chip.cls}`}>
            {chip.label} · {p.done}/{p.total}
        </button>
    );
}

export function InstallProgressPanel({ accountId }) {
    const p = useInstallProgress(accountId, 5000);
    const [restarting, setRestarting] = useState(false);
    const restartTerminal = async () => {
        // A17-7 — open positions stay open at the broker but are UNMANAGED while MT5 restarts
        if (!window.confirm("Restart this MT5 terminal on the VPS?\n\nWhile it restarts, open positions stay open at the broker but are unmanaged (no stops moved, no closes) for up to a minute. Continue?")) return;
        setRestarting(true);
        try {
            const { data } = await api.post(`/vps/agents/${p.vps_terminal.agent_id}/restart-terminal`, { account_id: accountId });
            toast.success(`Restart queued (${data.command_id}) — the agent closes MT5 gracefully and starts it again`);
        } catch (e) {
            toast.error(e?.response?.data?.detail?.message || e?.response?.data?.detail || "restart refused");
        } finally { setRestarting(false); }
    };
    if (!p) return null;
    const chip = STATE_CHIP[p.state] || STATE_CHIP.not_started;
    const copyUrl = async () => {
        try { await navigator.clipboard.writeText(p.webrequest_url); toast.success("WebRequest URL copied"); }
        catch { toast.error("Clipboard blocked — select & copy manually."); }
    };
    return (
        <div id={`install-progress-${accountId}`} className="border border-[#1F1F1F] bg-[#050505] p-3 mb-3" data-testid={`install-progress-${accountId}`}>
            <div className="flex items-center justify-between gap-2 mb-2">
                <div className="font-mono text-[10px] tracking-widest text-[#52525B]">VPS TERMINAL · INSTALL PROGRESS</div>
                <span className={`px-2 py-0.5 border font-mono text-[10px] tracking-widest ${chip.cls}`} data-testid={`install-progress-state-${accountId}`}>{chip.label}</span>
            </div>
            <ol className="space-y-1.5">
                {p.steps.map((s) => {
                    const st = STYLE[s.status] || STYLE.pending;
                    const Icon = st.icon;
                    return (
                        <li key={s.id} className="flex items-start gap-2" data-testid={`install-step-${s.id}-${accountId}`} data-status={s.status}>
                            <Icon className={`w-3.5 h-3.5 mt-0.5 flex-shrink-0 ${st.cls}`} />
                            <div className="min-w-0 text-xs">
                                <span className={s.status === "pending" ? "text-[#71717A]" : "text-white"}>{s.title}</span>
                                {s.detail && <span className="text-[#A1A1AA]"> — {s.detail}</span>}
                                {s.hint && s.status !== "done" && (
                                    <div className={`font-mono text-[11px] mt-0.5 ${s.status === "blocked" ? "text-[#FF3B30]" : "text-[#71717A]"}`}>{s.hint}</div>
                                )}
                            </div>
                        </li>
                    );
                })}
            </ol>
            {p.vps_terminal && (
                <div className="mt-2 font-mono text-[11px] text-[#71717A] flex flex-wrap items-center gap-2" data-testid={`install-vps-terminal-${accountId}`} data-status={p.vps_terminal.status}>
                    <span>
                        VPS agent terminal: <span className={p.vps_terminal.status === "running" ? "text-[#00FF41]" : (p.vps_terminal.status === "failed" || p.vps_terminal.status === "restart_loop") ? "text-[#FF3B30]" : "text-[#FFD700]"}>{String(p.vps_terminal.status || "").toUpperCase()}</span>
                        {p.vps_terminal.detail && <span> — {p.vps_terminal.detail}</span>}
                        {p.vps_terminal.restarts_last_hour > 0 && <span> · {p.vps_terminal.restarts_last_hour} restart{p.vps_terminal.restarts_last_hour === 1 ? "" : "s"} this hour</span>}
                    </span>
                    {p.vps_terminal.agent_id && !["queued", "installing"].includes(p.vps_terminal.status) && (
                        <button onClick={restartTerminal} disabled={restarting} data-testid={`install-vps-restart-${accountId}`}
                            title="Ask the VPS agent to close this MT5 gracefully and start it again (never force-killed)"
                            className="flex items-center gap-1 font-mono text-[9px] tracking-widest border border-[#1F1F1F] text-[#A1A1AA] px-2 py-0.5 hover:text-white hover:border-[#00FF41]/50 disabled:opacity-50">
                            {restarting ? <Loader2 className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />} RESTART TERMINAL
                        </button>
                    )}
                </div>
            )}
            {p.webrequest_url && (
                <div className="mt-2 flex items-center gap-2 font-mono text-[11px] text-[#71717A]">
                    WebRequest URL: <code className="text-[#FFD700] select-all" data-testid={`install-webrequest-url-${accountId}`}>{p.webrequest_url}</code>
                    <button onClick={copyUrl} data-testid={`install-webrequest-copy-${accountId}`} className="p-1 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]" title="Copy URL">
                        <Copy className="w-3 h-3" />
                    </button>
                </div>
            )}
        </div>
    );
}

export default InstallProgressPanel;
