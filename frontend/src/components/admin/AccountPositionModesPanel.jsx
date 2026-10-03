import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Layers } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

const SRC = {
    admin: { cls: "text-[#00FF41]", label: "ADMIN" },
    ea: { cls: "text-[#A1A1AA]", label: "TERMINAL" },
    registry: { cls: "text-[#00BFFF]", label: "REGISTRY" },
    default: { cls: "text-[#FFB000]", label: "ASSUMED (no setting, no registry entry)" },
};

export const AccountPositionModesPanel = () => {
    const [rows, setRows] = useState(null);
    const [target, setTarget] = useState(null);
    const [form, setForm] = useState({ mode: "netting", reason: "", password: "", otp: "" });
    const [saving, setSaving] = useState(false);

    const load = useCallback(async () => {
        try { setRows((await api.get("/admin/account-position-modes")).data.accounts); }
        catch (e) { toast.error(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const submit = async () => {
        setSaving(true);
        try {
            await api.post(`/admin/account-position-modes/${target.account_id}`, { ...form, otp: form.otp || null });
            toast.success(form.mode === "auto" ? "Admin setting cleared — registry/default applies" : `Position mode set to ${form.mode.toUpperCase()}`);
            setTarget(null); setForm({ mode: "netting", reason: "", password: "", otp: "" }); load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setSaving(false); }
    };

    return (
        <div className="mt-8" data-testid="account-position-modes-panel">
            <div className="mb-3">
                <h2 className="font-display font-bold text-base md:text-lg flex items-center gap-2"><Layers className="w-4 h-4 text-[#00BFFF]" /> Position Modes (netting / hedging)</h2>
                <p className="text-xs text-[#A1A1AA] mt-1">The EA does not report the account margin mode. A broker that NETS positions merges several bot fills into one ticket — STOIC then keys each row by its entry deal. Order of authority: admin setting here → broker-server registry entry → HEDGING assumed. Accounts shown as ASSUMED on a netting broker must be set explicitly.</p>
            </div>
            {rows === null ? <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /></div> : rows.length === 0 ? (
                <div className="border border-[#1F1F1F] p-4 font-mono text-[10px] text-[#52525B]" data-testid="account-position-modes-empty">No broker accounts.</div>
            ) : (
                <div className="border border-[#1F1F1F] rounded-lg overflow-x-auto">
                    <table className="w-full text-xs">
                        <thead className="bg-[#0A0A0A] text-[#52525B] font-mono">
                            <tr><th className="text-left px-4 py-2">ACCOUNT</th><th className="text-left px-4 py-2">MODE</th><th className="text-left px-4 py-2">SOURCE</th><th className="text-right px-4 py-2">ACTION</th></tr>
                        </thead>
                        <tbody>
                            {rows.map((r, i) => (
                                <tr key={r.account_id} data-testid={`account-pm-row-${r.account_id}`} className={i % 2 === 0 ? "bg-[#0A0A0A]" : "bg-[#0F0F0F]"}>
                                    <td className="px-4 py-2.5"><div className="text-white">{r.label || r.account_number}</div><div className="font-mono text-[10px] text-[#52525B]">{r.broker} · {r.server || "—"} · {r.account_type}</div></td>
                                    <td className="px-4 py-2.5 font-mono text-[10px]" data-testid={`account-pm-mode-${r.account_id}`}>
                                        <span className={`px-1.5 py-0.5 border ${r.mode === "netting" ? "border-[#00BFFF]/50 text-[#00BFFF]" : "border-[#52525B] text-[#A1A1AA]"}`}>{r.mode.toUpperCase()}</span>
                                    </td>
                                    <td className={`px-4 py-2.5 font-mono text-[10px] ${(SRC[r.source] || SRC.default).cls}`} data-testid={`account-pm-source-${r.account_id}`}>
                                        {(SRC[r.source] || SRC.default).label}
                                        {r.override && <span className="block text-[#52525B]">{r.override.by} · {(r.override.at || "").slice(0, 10)}{r.override.reason ? ` · ${r.override.reason}` : ""}</span>}
                                    </td>
                                    <td className="px-4 py-2.5 text-right">
                                        <button data-testid={`account-pm-set-${r.account_id}`} onClick={() => { setTarget(r); setForm({ mode: r.mode === "netting" ? "hedging" : "netting", reason: "", password: "", otp: "" }); }}
                                            className="text-[#00BFFF] font-mono text-[10px]">SET MODE</button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
            <Dialog open={!!target} onOpenChange={(o) => !o && setTarget(null)}>
                <DialogContent className="bg-[#0A0A0A] border-[#1F1F1F]" data-testid="account-pm-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-mono text-sm">Position mode · {target?.label || target?.account_number}</DialogTitle>
                        <DialogDescription className="text-[#71717A] text-xs">Recorded in the admin audit chain. Re-authenticate to apply. Currently {target?.mode?.toUpperCase()} ({(SRC[target?.source] || SRC.default).label}).</DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3">
                        <div className="flex gap-2" data-testid="account-pm-mode-options">
                            {["netting", "hedging", "auto"].map((m) => (
                                <button key={m} data-testid={`account-pm-option-${m}`} onClick={() => setForm({ ...form, mode: m })}
                                    className={`px-3 py-1.5 font-mono text-[10px] tracking-widest border ${form.mode === m ? "border-[#00BFFF] text-[#00BFFF]" : "border-[#1F1F1F] text-[#52525B]"}`}>
                                    {m === "auto" ? "AUTO (registry / hedging)" : m.toUpperCase()}
                                </button>
                            ))}
                        </div>
                        <Input data-testid="account-pm-reason" placeholder="reason (optional)" value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
                        <Input data-testid="account-pm-password" type="password" placeholder="your password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="current-password" />
                        <Input data-testid="account-pm-otp" inputMode="numeric" placeholder="authenticator code (if enabled)" value={form.otp} onChange={(e) => setForm({ ...form, otp: e.target.value })} autoComplete="one-time-code" />
                        <div className="flex justify-end gap-2 pt-1">
                            <Button variant="ghost" data-testid="account-pm-cancel" onClick={() => setTarget(null)}>Cancel</Button>
                            <Button data-testid="account-pm-submit" disabled={saving || !form.password} className="disabled:opacity-40" onClick={submit}>{saving ? "Applying…" : "Confirm"}</Button>
                        </div>
                    </div>
                </DialogContent>
            </Dialog>
        </div>
    );
};
