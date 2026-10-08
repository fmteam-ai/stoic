import { useEffect, useState } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Server, Loader2, Bot } from "lucide-react";

const errMsg = (e) => e?.response?.data?.detail?.message || e?.response?.data?.detail || e?.message || "request failed";

/** Phase 2 — "Install on my VPS": picks one of the user's enrolled STOIC VPS Agents and queues an
 *  install_terminal command (fresh pairing token, portable clone of the golden MT5). Hidden when no agent exists. */
export function VpsAgentInstall({ accountId }) {
    const [agents, setAgents] = useState(null);
    const [agentId, setAgentId] = useState("");
    const [busy, setBusy] = useState(false);
    const [queued, setQueued] = useState(null);

    useEffect(() => {
        let alive = true;
        api.get("/vps/agents").then(({ data }) => {
            if (!alive) return;
            const list = data.agents || [];
            setAgents(list);
            const first = list.find(a => a.online) || list[0];
            if (first) setAgentId(first.agent_id);
        }).catch(() => alive && setAgents([]));
        return () => { alive = false; };
    }, []);

    if (!agents || agents.length === 0) return null;
    const chosen = agents.find(a => a.agent_id === agentId);

    const install = async () => {
        setBusy(true);
        try {
            const { data } = await api.post(`/vps/agents/${agentId}/install-terminal`, { account_id: accountId });
            setQueued(data);
            toast.success(`Install queued on ${chosen?.hostname || agentId} — the agent picks it up within 30 s`);
        } catch (e) {
            toast.error(errMsg(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#050505] p-3 mb-3" data-testid={`vps-agent-install-${accountId}`}>
            <div className="flex items-center gap-2 mb-2">
                <Bot className="w-4 h-4 text-[#00FF41]" />
                <div className="font-mono text-[10px] tracking-widest text-[#52525B]">VPS AGENT · INSTALL WITHOUT TOUCHING THE VPS</div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
                <select value={agentId} onChange={e => setAgentId(e.target.value)} data-testid={`vps-agent-select-${accountId}`}
                    className="bg-[#0A0A0A] border border-[#1F1F1F] text-white text-xs font-mono px-2 py-1.5 focus:outline-none focus:border-[#00FF41]">
                    {agents.map(a => (
                        <option key={a.agent_id} value={a.agent_id}>
                            {(a.hostname || a.agent_id)} · {a.online ? "online" : "offline"} · {a.terminals} terminal{a.terminals === 1 ? "" : "s"}{a.golden_ready ? "" : " · golden MT5 missing"}
                        </option>
                    ))}
                </select>
                <button onClick={install} disabled={busy || !chosen?.online} data-testid={`vps-agent-install-btn-${accountId}`}
                    title={!chosen?.online ? "This agent is offline — start the STOIC VPS Agent task on that VPS" : "Clone the golden portable MT5, install the EA and start the terminal for this account"}
                    className="flex items-center gap-1.5 px-3 py-1.5 border border-[#00FF41]/50 text-[#00FF41] font-mono text-[10px] tracking-widest hover:bg-[#00FF41]/10 disabled:opacity-40">
                    {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Server className="w-3 h-3" />} INSTALL ON MY VPS
                </button>
            </div>
            <p className="text-[11px] text-[#52525B] mt-2">
                The agent clones <code>C:\STOIC\golden\MT5</code> to <code>C:\STOIC\MT5\account-&lt;login&gt;</code>, installs the EA with a fresh pairing code and starts it.
                Type the MT5 password once on the VPS (<code>Set-StoicTerminalLogin</code>) for an unattended first login — it never leaves the VPS.
                {queued && <span className="text-[#A1A1AA]" data-testid={`vps-agent-queued-${accountId}`}> Queued: {queued.command_id} for login #{queued.login}.</span>}
            </p>
        </div>
    );
}
