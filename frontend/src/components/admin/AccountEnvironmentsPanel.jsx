import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, ShieldCheck, ShieldOff } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

const Tag = ({ v }) => (
    <span className={`font-mono text-[10px] px-1.5 py-0.5 border ${v === "LIVE" ? "border-[#FF3B30]/50 text-[#FF3B30]" : v === "DEMO" ? "border-[#00FF41]/50 text-[#00FF41]" : "border-[#52525B] text-[#A1A1AA]"}`}>{v}</span>
);

export const AccountEnvironmentsPanel = () => {
    const [rows, setRows] = useState(null);
    const [target, setTarget] = useState(null);
    const [form, setForm] = useState({ reason: "", password: "", otp: "", override: false });
    const [saving, setSaving] = useState(false);

    const load = useCallback(async () => {
        try { setRows((await api.get("/admin/account-environments")).data.accounts); }
        catch (e) { toast.error(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const submit = async () => {
        setSaving(true);
        try {
            await api.post(`/admin/account-environments/${target.account_id}`, { environment: target.next, ...form, otp: form.otp || null });
            toast.success(target.next === "DEMO" ? (form.override ? "DEMO attested via ADMIN OVERRIDE" : "DEMO attested") : "Attestation revoked");
            setTarget(null); setForm({ reason: "", password: "", otp: "", override: false }); load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setSaving(false); }
    };

    const needsOverride = target?.next === "DEMO" && !!target?.proof && !target.proof.ok;
    const missing = [];
    if (needsOverride && !form.override) missing.push("tick the override confirmation above");
    if (needsOverride && form.reason.trim().length < 10) missing.push(`reason needs ${10 - form.reason.trim().length} more character(s)`);
    if (!form.password) missing.push("enter your password");

    return (
        <div className="mt-8" data-testid="account-environments-panel">
            <div className="mb-3">
                <h2 className="font-display font-bold text-base md:text-lg">Account Environments</h2>
                <p className="text-xs text-[#A1A1AA] mt-1">A user-declared DEMO never bypasses the EX5 release proof on its own. Attest it here (password + authenticator) so an unverified local EA may trade practice money. The attestation is bound to the account's identity (broker, server, number, terminal, credentials) and is voided automatically if any of them change. LIVE accounts always need the signed release hash.</p>
            </div>
            {rows === null ? <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /></div> : rows.length === 0 ? (
                <div className="border border-[#1F1F1F] p-4 font-mono text-[10px] text-[#52525B]" data-testid="account-environments-empty">No DEMO-declared or attested accounts — live accounts always require the signed release proof.</div>
            ) : (
                <div className="border border-[#1F1F1F] rounded-lg overflow-x-auto">
                    <table className="w-full text-xs">
                        <thead className="bg-[#0A0A0A] text-[#52525B] font-mono">
                            <tr><th className="text-left px-4 py-2">ACCOUNT</th><th className="text-left px-4 py-2">DECLARED</th><th className="text-left px-4 py-2">EFFECTIVE</th><th className="text-left px-4 py-2">BROKER</th><th className="text-left px-4 py-2">DEMO PROOF</th><th className="text-left px-4 py-2">ATTESTED</th><th className="text-right px-4 py-2">ACTION</th></tr>
                        </thead>
                        <tbody>
                            {rows.map((r, i) => (
                                <tr key={r.account_id} data-testid={`account-env-row-${r.account_id}`} className={i % 2 === 0 ? "bg-[#0A0A0A]" : "bg-[#0F0F0F]"}>
                                    <td className="px-4 py-2.5"><div className="text-white">{r.label || r.account_number}</div><div className="font-mono text-[10px] text-[#52525B]">{r.broker} · {r.server} · {r.account_type}</div></td>
                                    <td className="px-4 py-2.5"><Tag v={r.declared} /></td>
                                    <td className="px-4 py-2.5"><Tag v={r.effective} /></td>
                                    <td className="px-4 py-2.5 font-mono text-[10px]" data-testid={`account-env-broker-mode-${r.account_id}`} title="ACCOUNT_TRADE_MODE as reported by the EA (1.60+)">
                                        {r.broker_trade_mode === "demo" ? <span className="text-[#00FF41]">DEMO</span>
                                            : r.broker_trade_mode ? <span className="text-[#FF3B30] font-bold">{r.broker_trade_mode.toUpperCase()} MONEY</span>
                                            : <span className="text-[#52525B]">— (EA &lt; 1.60)</span>}
                                    </td>
                                    <td className="px-4 py-2.5 font-mono text-[10px]" data-testid={`account-env-proof-${r.account_id}`}>
                                        {r.proof?.ok ? <span className="text-[#00FF41]">{r.proof?.checks?.broker_reports_demo ? "BROKER SAYS DEMO" : "EA EVIDENCE OK"}</span>
                                            : r.proof?.override_eligible ? <span className="text-[#FFB000]">SERVER NOT DEMO-NAMED · {r.proof.reported_server || "?"}</span>
                                            : <span className="text-[#FF3B30]">MISSING: {Object.entries(r.proof?.checks || {}).filter(([k, v]) => !v && k !== "server_demo_named" && k !== "broker_reports_demo").map(([k]) => k).join(", ") || "—"}</span>}
                                    </td>
                                    <td className="px-4 py-2.5 font-mono text-[10px] text-[#A1A1AA]">
                                        {r.attested_by ? `${r.attested_by} · ${(r.attested_at || "").slice(0, 10)}` : "—"}
                                        {r.verifier === "admin_override" && <span data-testid={`account-env-override-${r.account_id}`} className="block text-[#FF3B30] font-bold">ADMIN OVERRIDE — server not demo-named</span>}
                                        {r.attestation_state === "invalidated" && <span data-testid={`account-env-invalidated-${r.account_id}`} className="block text-[#FFB000]">{r.broker_trade_mode && r.broker_trade_mode !== "demo" ? `INVALIDATED — broker reports ${r.broker_trade_mode.toUpperCase()} money (ACCOUNT_TRADE_MODE)` : "INVALIDATED — bound identity changed (broker/server/number/terminal/credentials)"}</span>}
                                    </td>
                                    <td className="px-4 py-2.5 text-right">
                                        {r.attestation_state === "invalidated" ? (
                                            <span className="inline-flex items-center gap-3">
                                                {r.proof?.checks?.broker_not_real_money !== false && <button data-testid={`account-env-reattest-${r.account_id}`} onClick={() => setTarget({ ...r, next: "DEMO" })} className="text-[#00FF41] font-mono text-[10px] inline-flex items-center gap-1"><ShieldCheck className="w-3.5 h-3.5" /> RE-ATTEST</button>}
                                                <button data-testid={`account-env-revoke-${r.account_id}`} onClick={() => setTarget({ ...r, next: "LIVE" })} className="text-[#FF3B30] font-mono text-[10px] inline-flex items-center gap-1"><ShieldOff className="w-3.5 h-3.5" /> REVOKE</button>
                                            </span>
                                        ) : r.attested_by ? (
                                            <button data-testid={`account-env-revoke-${r.account_id}`} onClick={() => setTarget({ ...r, next: "LIVE" })} className="text-[#FF3B30] font-mono text-[10px] inline-flex items-center gap-1"><ShieldOff className="w-3.5 h-3.5" /> REVOKE</button>
                                        ) : r.declared === "DEMO" && r.proof?.mandatory_ok ? (
                                            <button data-testid={`account-env-attest-${r.account_id}`} onClick={() => setTarget({ ...r, next: "DEMO" })} className={`${r.proof.ok ? "text-[#00FF41]" : "text-[#FFB000]"} font-mono text-[10px] inline-flex items-center gap-1`}><ShieldCheck className="w-3.5 h-3.5" /> {r.proof.ok ? "ATTEST DEMO" : "ATTEST (OVERRIDE)"}</button>
                                        ) : r.declared === "DEMO" ? (
                                            <span className="font-mono text-[10px] text-[#52525B]">needs fresh EA evidence</span>
                                        ) : <span className="font-mono text-[10px] text-[#52525B]">live — proof required</span>}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
            <Dialog open={!!target} onOpenChange={(o) => !o && setTarget(null)}>
                <DialogContent className="bg-[#0A0A0A] border-[#1F1F1F]" data-testid="account-env-dialog">
                    <DialogHeader>
                        <DialogTitle className="font-mono text-sm">{target?.next === "DEMO" ? "Attest DEMO" : "Revoke attestation"} · {target?.label || target?.account_number}</DialogTitle>
                        <DialogDescription className="text-[#71717A] text-xs">Recorded in the admin audit chain. Re-authenticate to apply.</DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3">
                        {target?.next === "DEMO" && target?.proof && !target.proof.ok && (
                            <label data-testid="account-env-override-toggle" className="flex items-start gap-2 border border-[#FF3B30]/50 bg-[#FF3B30]/5 p-2 text-xs text-[#FF3B30]">
                                <input type="checkbox" checked={form.override} onChange={(e) => setForm({ ...form, override: e.target.checked })} className="mt-0.5" />
                                <span>The EA reports <b>{target.proof.reported_server || "an unnamed server"}</b>, which is not demo-named. I confirm this is a practice account and accept that this override is recorded in the audit chain (reason required, ≥10 chars).</span>
                            </label>
                        )}
                        <Input data-testid="account-env-reason" placeholder={needsOverride ? "reason (REQUIRED for override, ≥10 chars)" : "reason (optional)"} value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} className={needsOverride && form.reason.trim().length < 10 ? "border-[#FF3B30]/60" : ""} />
                        <Input data-testid="account-env-password" type="password" placeholder="your password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="current-password" />
                        <Input data-testid="account-env-otp" inputMode="numeric" placeholder="authenticator code (if enabled)" value={form.otp} onChange={(e) => setForm({ ...form, otp: e.target.value })} autoComplete="one-time-code" />
                        {missing.length > 0 && (
                            <ul data-testid="account-env-missing" className="text-[11px] font-mono text-[#FFB000] space-y-0.5">
                                {missing.map((m) => <li key={m}>· {m}</li>)}
                            </ul>
                        )}
                        <div className="flex justify-end gap-2 pt-1">
                            <Button variant="ghost" data-testid="account-env-cancel" onClick={() => setTarget(null)}>Cancel</Button>
                            <Button data-testid="account-env-submit" disabled={saving || missing.length > 0} className="disabled:opacity-40" onClick={submit}>{saving ? "Applying…" : "Confirm"}</Button>
                        </div>
                    </div>
                </DialogContent>
            </Dialog>
        </div>
    );
};
