import { useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Loader2, Server } from "lucide-react";
import { toast } from "sonner";

const norm = (s) => String(s || "").trim().toLowerCase().replace(/\s+/g, " ");

/** M122-P2 — the EA (same MT5 login) reports a different broker server NAME than this account is registered on.
 *  Re-pairing never fixes that (the installer does not touch the server name); adopting or editing it does.
 *  Both actions are step-up MFA (the api interceptor prompts) and audited; the verified chain re-verifies on the next heartbeat. */
export function ServerMismatchFix({ account, onFixed }) {
    const [busy, setBusy] = useState(false);
    const [editing, setEditing] = useState(false);
    const [server, setServer] = useState(account?.server || "");
    const reported = account?.ea_identity?.broker_server;
    const reportedLogin = account?.broker_account_id_reported;
    const sameLogin = reportedLogin != null && String(reportedLogin) === String(account?.account_number || "");
    const reasonSaysServer = /broker server mismatch/i.test(account?.broker_account_mismatch_reason || "")
        || /broker server mismatch/i.test(account?.ea_identity?.reason || "");
    const differs = !!reported && norm(reported) !== norm(account?.server);
    if (!account || account.mode === "paper" || !(reasonSaysServer || (differs && sameLogin))) return null;

    const adopt = async () => {
        setBusy(true);
        try {
            const r = await api.post(`/accounts/${account.id}/adopt-reported-server`);
            toast.success(`Server set to ${r.data.server} — the chain re-verifies on the next heartbeat`);
            onFixed && onFixed();
        } catch (e) { toast.error("Could not adopt the reported server", { description: formatApiError(e) }); }
        finally { setBusy(false); }
    };
    const save = async () => {
        setBusy(true);
        try {
            await api.patch(`/accounts/${account.id}`, { server: server.trim() });
            toast.success(`Server set to ${server.trim()}`);
            setEditing(false);
            onFixed && onFixed();
        } catch (e) { toast.error("Could not change the server", { description: formatApiError(e) }); }
        finally { setBusy(false); }
    };

    return (
        <div className="mb-4 border border-[#FFB000]/40 bg-[#FFB000]/5 p-3" data-testid={`server-mismatch-fix-${account.id}`}>
            <div className="flex items-center gap-2 text-sm text-white font-medium mb-1">
                <Server className="w-4 h-4 text-[#FFB000]" /> Broker server name differs
            </div>
            <div className="text-xs text-[#A1A1AA] mb-2.5 leading-relaxed">
                This account is registered on <code className="text-[#FFD700] font-mono">{account.server || "—"}</code>
                {reported && <> but the EA on MT5 login <code className="text-[#FFD700] font-mono">{String(reportedLogin ?? account.account_number)}</code> reports <code className="text-[#00FF41] font-mono">{reported}</code></>}.
                {" "}Re-pairing does not change this — the server name does. Step-up MFA required; audited.
            </div>
            <div className="flex flex-wrap items-center gap-2">
                {reported && sameLogin && differs && (
                    <button onClick={adopt} disabled={busy} data-testid={`adopt-reported-server-${account.id}`}
                        className="px-3 py-1.5 text-xs font-mono tracking-widest bg-[#00FF41] text-black hover:bg-[#00FF41]/90 disabled:opacity-50 flex items-center gap-1.5">
                        {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : null} USE THE SERVER THE EA REPORTS ({reported})
                    </button>
                )}
                {!editing ? (
                    <button onClick={() => setEditing(true)} disabled={busy} data-testid={`edit-server-btn-${account.id}`}
                        className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#52525B] text-[#A1A1AA]">
                        EDIT SERVER BY HAND
                    </button>
                ) : (
                    <div className="flex items-center gap-2">
                        <input value={server} onChange={e => setServer(e.target.value)} placeholder="exact MT5 server name"
                            data-testid={`edit-server-input-${account.id}`}
                            className="bg-black border border-[#1F1F1F] px-2 py-1.5 text-xs font-mono text-white w-56 focus:border-[#FFD700] outline-none" />
                        <button onClick={save} disabled={busy || !server.trim() || norm(server) === norm(account.server)}
                            data-testid={`edit-server-save-${account.id}`}
                            className="px-3 py-1.5 text-xs font-mono tracking-widest bg-[#FFD700] text-black disabled:opacity-50">SAVE</button>
                        <button onClick={() => setEditing(false)} className="text-xs font-mono text-[#71717A]">cancel</button>
                    </div>
                )}
            </div>
        </div>
    );
}
