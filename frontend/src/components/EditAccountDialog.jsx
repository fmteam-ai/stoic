import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";

const INPUT = "w-full bg-black border border-[#1F1F1F] px-3 py-2 text-sm font-mono text-white focus:border-[#FFD700] outline-none";
const norm = (s) => String(s || "").trim();

/** M123-4 — Edit an account card: label, group and (live accounts) the exact MT5 server name.
 *  PATCH /accounts/{id}: label/group are plain; a server change is step-up MFA (api interceptor prompts), audited,
 *  and resets the verified chain so the next heartbeat re-verifies. */
export function EditAccountDialog({ account, open, onClose, onSaved }) {
    const { user } = useAuth();
    const isAdmin = user?.role === "admin";          // U3 — the server name is adopted from the EA; only admins edit it by hand
    const [label, setLabel] = useState("");
    const [group, setGroup] = useState("");
    const [server, setServer] = useState("");
    const [suggest, setSuggest] = useState(null);
    const [busy, setBusy] = useState(false);
    const isLive = account && account.mode !== "paper" && isAdmin;

    useEffect(() => {
        if (!open || !account) return;
        setLabel(account.label || ""); setGroup(account.group || ""); setServer(account.server || ""); setSuggest(null);
    }, [open, account]);

    useEffect(() => {
        if (!open || !isLive || !norm(server) || norm(server) === norm(account?.server)) { setSuggest(null); return; }
        const t = setTimeout(() => {
            api.get("/accounts/server-suggest", { params: { server: norm(server), broker: account?.broker || "" } })
                .then((r) => setSuggest(r.data)).catch(() => setSuggest(null));
        }, 350);
        return () => clearTimeout(t);
    }, [open, server, isLive, account]);

    if (!account) return null;
    const serverChanged = isLive && norm(server) !== norm(account.server);
    const changes = {
        ...(norm(label) !== norm(account.label) ? { label: norm(label) } : {}),
        ...(norm(group) !== norm(account.group) ? { group: norm(group) } : {}),
        ...(serverChanged ? { server: norm(server) } : {}),
    };
    const dirty = Object.keys(changes).length > 0;

    const save = async () => {
        if (!dirty) return;
        setBusy(true);
        try {
            await api.patch(`/accounts/${account.id}`, changes);
            toast.success(serverChanged ? `Saved — server is now ${changes.server}; the chain re-verifies on the next heartbeat` : "Account updated");
            onSaved && onSaved();
            onClose();
        } catch (e) { toast.error("Could not save", { description: formatApiError(e) }); }
        finally { setBusy(false); }
    };

    return (
        <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
            <DialogContent className="bg-[#0A0A0A] border-[#1F1F1F] text-white max-w-md" data-testid={`edit-account-dialog-${account.account_number}`}>
                <DialogHeader>
                    <DialogTitle className="font-display">Edit account</DialogTitle>
                    <DialogDescription className="font-mono text-xs text-[#71717A]">{account.broker} · #{account.account_number}{account.mode === "paper" ? " · paper" : ""}</DialogDescription>
                </DialogHeader>
                <div className="space-y-3">
                    <label className="block text-[10px] font-mono tracking-widest text-[#A1A1AA]">LABEL
                        <input value={label} onChange={(e) => setLabel(e.target.value)} className={`${INPUT} mt-1`} data-testid="edit-account-label" maxLength={80} />
                    </label>
                    <label className="block text-[10px] font-mono tracking-widest text-[#A1A1AA]">GROUP <span className="text-[#52525B]">(optional)</span>
                        <input value={group} onChange={(e) => setGroup(e.target.value)} placeholder="AGGRESSIVE, PROP, FAMILY…" className={`${INPUT} mt-1`} data-testid="edit-account-group" maxLength={40} />
                    </label>
                    {account.mode !== "paper" && !isAdmin && (
                        <div className="text-[11px] text-[#71717A]" data-testid="edit-account-server-auto">
                            MT5 server: <span className="font-mono text-[#A1A1AA]">{account.server || "— filled from the EA's first heartbeat"}</span> · adopted automatically from the EA
                        </div>
                    )}
                    {isLive && (
                        <label className="block text-[10px] font-mono tracking-widest text-[#A1A1AA]">MT5 SERVER <span className="text-[#52525B]">(exact name MT5 shows)</span>
                            <input value={server} onChange={(e) => setServer(e.target.value)} className={`${INPUT} mt-1`} data-testid="edit-account-server" maxLength={64} />
                            {serverChanged && (
                                <div className="mt-1 text-[11px] font-normal tracking-normal text-[#FFB000]" data-testid="edit-account-server-note">
                                    Step-up MFA required · audited · the verified terminal chain re-verifies on the next heartbeat.
                                    {suggest && !suggest.known && suggest.hint ? <> {suggest.hint}</> : null}
                                    {suggest?.suggestions?.length > 0 && !suggest.known && (
                                        <div className="mt-1 flex flex-wrap gap-1">
                                            {suggest.suggestions.map((s) => (
                                                <button key={s} type="button" onClick={() => setServer(s)} className="px-1.5 py-0.5 border border-[#FFB000]/40 text-[#FFD700] font-mono hover:bg-[#FFB000]/10">{s}</button>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            )}
                        </label>
                    )}
                    <div className="flex justify-end gap-2 pt-2">
                        <button type="button" onClick={onClose} className="px-3 py-1.5 text-xs font-mono tracking-widest text-[#71717A] hover:text-white" data-testid="edit-account-cancel">CANCEL</button>
                        <button type="button" onClick={save} disabled={busy || !dirty || !norm(label)} data-testid="edit-account-save"
                            className="px-4 py-1.5 text-xs font-mono tracking-widest bg-[#FFD700] text-black disabled:opacity-40 flex items-center gap-1.5">
                            {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : null} SAVE
                        </button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
