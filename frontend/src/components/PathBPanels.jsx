import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";

export function PathBStatusLadder({ deploymentId }) {
    const [s, setS] = useState(null);
    const load = useCallback(() => {
        api.get(`/infra/deployments/${deploymentId}/pathb-status`).then(({ data }) => setS(data)).catch(() => {});
    }, [deploymentId]);
    useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);
    if (!s) return null;
    const idx = s.ladder.indexOf(s.status);
    return (
        <div className="mt-1" data-testid={`pathb-ladder-${deploymentId}`}>
            <div className="flex flex-wrap gap-1">
                {s.ladder.map((step, i) => (
                    <span key={step} className={`font-mono text-[7px] px-1.5 py-0.5 border ${i < idx ? "border-[#00FF41]/30 text-[#00FF41]/60" : i === idx ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#3F3F46]"}`}>
                        {step.replace(/_/g, " ")}
                    </span>
                ))}
            </div>
            {s.diagnostics && <div className="font-mono text-[8px] text-[#FFD700] mt-1" data-testid="pathb-diagnostics">{s.diagnostics}</div>}
        </div>
    );
}

export function DiscoveredTerminals({ deploymentId }) {
    const [terms, setTerms] = useState([]);
    const load = useCallback(() => {
        api.get(`/infra/deployments/${deploymentId}/discovery`).then(({ data }) => setTerms(data.terminals || [])).catch(() => {});
    }, [deploymentId]);
    useEffect(() => { load(); }, [load]);
    if (!terms.length) return null;

    const decide = async (id, action) => {
        const consent = action === "manage"
            ? window.confirm("Managing modifies your existing production terminal. Cloning into an isolated STOIC instance is safer. Continue with MANAGE?")
            : false;
        if (action === "manage" && !consent) return;
        try {
            const { data } = await api.post(`/infra/discovery/${id}/decision`, { action, consent });
            toast.success(`Terminal → ${data.decision}${data.directory ? ` (${data.directory})` : ""}`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    return (
        <div className="mt-2 space-y-1" data-testid={`discovered-${deploymentId}`}>
            <div className="font-mono text-[8px] text-[#52525B] tracking-widest">DISCOVERED MT5 TERMINALS</div>
            {terms.map((t) => (
                <div key={t.discovery_id} className="border border-[#141414] p-2" data-testid={`terminal-${t.discovery_id}`}>
                    <div className="font-mono text-[9px] text-white">
                        {t.broker_hint || "Unknown MT5"} {t.account_login ? `· Account ${t.account_login}` : "· No account connected"}
                        <span className={`ml-2 ${t.running ? "text-[#00FF41]" : "text-[#52525B]"}`}>{t.running ? "Running" : "Stopped"}</span>
                        <span className={`ml-2 ${t.ea_installed ? "text-[#00FF41]" : "text-[#FFD700]"}`}>{t.ea_installed ? "EA installed" : "EA not installed"}</span>
                    </div>
                    <div className="font-mono text-[8px] text-[#3F3F46]">{t.path}</div>
                    {t.decision === "pending" ? (
                        <div className="flex gap-1.5 mt-1">
                            <button onClick={() => decide(t.discovery_id, "clone")} data-testid={`clone-${t.discovery_id}`}
                                className="font-mono text-[8px] px-2 py-1 border border-[#00FF41]/40 text-[#00FF41]">CLONE (SAFE)</button>
                            <button onClick={() => decide(t.discovery_id, "manage")} data-testid={`manage-${t.discovery_id}`}
                                className="font-mono text-[8px] px-2 py-1 border border-[#FFD700]/40 text-[#FFD700]">MANAGE</button>
                            <button onClick={() => decide(t.discovery_id, "unmanage")} data-testid={`unmanage-${t.discovery_id}`}
                                className="font-mono text-[8px] px-2 py-1 border border-[#27272A] text-[#A1A1AA]">LEAVE UNMANAGED</button>
                        </div>
                    ) : (
                        <div className="font-mono text-[8px] text-[#0099FF] mt-1">decision: {t.decision}</div>
                    )}
                </div>
            ))}
        </div>
    );
}

export function AgentHealthCard({ agentId }) {
    const [h, setH] = useState(null);
    const [cmd, setCmd] = useState("run_diagnostics");
    const load = useCallback(() => {
        api.get(`/infra/agents/${agentId}/health`).then(({ data }) => setH(data)).catch(() => {});
    }, [agentId]);
    useEffect(() => { load(); }, [load]);
    if (!h) return null;
    const send = async () => {
        try {
            await api.post(`/infra/agents/${agentId}/commands`, { command: cmd, params: {} });
            toast.success(`${cmd} queued`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
    };
    return (
        <div className="mt-2 border-t border-[#141414] pt-2" data-testid={`agent-health-${agentId}`}>
            {h.commands_frozen && <div className="font-mono text-[8px] text-[#FF3B30]">COMMANDS FROZEN — INCIDENT WORKFLOW ACTIVE (VPS unreachable)</div>}
            {h.policy_flags?.order_entry_disabled && <div className="font-mono text-[8px] text-[#FF3B30]">ORDER ENTRY DISABLED — time drift detected</div>}
            <div className="flex items-center gap-1.5 mt-1">
                <select value={cmd} onChange={(e) => setCmd(e.target.value)} data-testid={`cmd-select-${agentId}`}
                    className="bg-black border border-[#1F1F1F] font-mono text-[8px] px-1 py-1 text-white">
                    {["run_diagnostics", "restart_terminal", "rotate_logs", "install_ea", "update_agent", "freeze"].map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <button onClick={send} data-testid={`cmd-send-${agentId}`}
                    className="font-mono text-[8px] px-2 py-1 border border-[#0099FF]/40 text-[#0099FF]">QUEUE COMMAND</button>
            </div>
            {(h.recent_commands || []).slice(0, 5).map((c) => (
                <div key={c.command_id} className="font-mono text-[8px] text-[#52525B] mt-0.5">
                    {c.command} · <span className={c.status === "failed" ? "text-[#FF3B30]" : c.status === "acked" ? "text-[#00FF41]" : "text-[#FFD700]"}>{c.status}</span> · {c.issued_by}
                </div>
            ))}
        </div>
    );
}

export function EaDeploymentsCard() {
    const [d, setD] = useState(null);
    useEffect(() => {
        api.get("/infra/ea-deployments").then(({ data }) => setD(data)).catch(() => {});
    }, []);
    if (!d || !d.deployments.length) return null;
    const active = d.states.filter((s) => s !== "FAILED");
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="ea-deployments-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                EA DEPLOYMENTS · CONNECTED ONLY AFTER A VERIFIED HEARTBEAT
            </div>
            <div className="font-mono text-[8px] text-[#3F3F46] mb-2">
                One account → one installation identity → one MT5 terminal → one Windows host. Execution-owner lease: {d.lease_seconds}s.
            </div>
            {d.deployments.map((dep) => {
                const idx = active.indexOf(dep.state);
                return (
                    <div key={dep.ea_deployment_id} className="border border-[#141414] p-2 mb-2" data-testid={`ea-deploy-${dep.ea_deployment_id}`}>
                        <div className="font-mono text-[9px] text-white">
                            acct {dep.account_id.slice(0, 8)}…
                            <span className={`ml-2 ${dep.connected ? "text-[#00FF41]" : dep.state === "FAILED" ? "text-[#FF3B30]" : "text-[#FFD700]"}`}
                                data-testid={`ea-deploy-status-${dep.ea_deployment_id}`}>
                                {dep.connected ? "CONNECTED" : dep.state === "FAILED" ? "FAILED" : `NOT CONNECTED — ${dep.state.replace(/_/g, " ")}`}
                            </span>
                            {dep.execution_owner && (
                                <span className={`ml-2 font-mono text-[8px] ${dep.lease_active ? "text-[#0099FF]" : "text-[#52525B]"}`}>
                                    owner {dep.execution_owner}{dep.lease_active ? " (lease active)" : " (lease expired)"}
                                </span>
                            )}
                        </div>
                        <div className="flex flex-wrap gap-1 mt-1">
                            {active.map((s, i) => (
                                <span key={s} className={`font-mono text-[7px] px-1 py-0.5 border ${dep.state === "FAILED" ? "border-[#1F1F1F] text-[#3F3F46]" : i < idx ? "border-[#00FF41]/30 text-[#00FF41]/60" : i === idx ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#3F3F46]"}`}>
                                    {s.replace(/_/g, " ")}
                                </span>
                            ))}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

export function FailureMatrixCard() {
    const [m, setM] = useState(null);
    const [open, setOpen] = useState(false);
    useEffect(() => { api.get("/infra/failure-matrix").then(({ data }) => setM(data)).catch(() => {}); }, []);
    if (!m) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="failure-matrix-card">
            <button onClick={() => setOpen((o) => !o)} className="font-mono text-[10px] text-[#52525B] tracking-widest" data-testid="failure-matrix-toggle">
                FAILURE HANDLING · EVERY AUTOMATIC STEP HAS A RECOVERY ACTION {open ? "▾" : "▸"}
            </button>
            {open && (
                <div className="mt-2">
                    {m.matrix.map((r) => (
                        <div key={r.failure} className="flex font-mono text-[9px] border-t border-[#141414] py-1">
                            <span className="w-1/2 text-[#A1A1AA]">{r.failure.replace(/_/g, " ")}</span>
                            <span className="w-1/2 text-[#00FF41]">{r.response}</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
