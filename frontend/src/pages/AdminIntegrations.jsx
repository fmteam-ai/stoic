import { useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, KeyRound, CheckCircle2, AlertTriangle, ShieldCheck, RefreshCw } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { PlansEditor } from "@/components/admin/PlansEditor";
import { EmailTemplatesPanel } from "@/components/admin/EmailTemplatesPanel";
import { SecurityAlertsCard } from "@/components/admin/SecurityAlertsCard";
import { KeyRow } from "@/components/admin/KeyRow";

const ORDER = ["stripe", "turnstile", "email", "ai"];
const keysOf = (data, id) => Object.entries(data.keys).filter(([, k]) => k.provider === id);

function ProviderCard({ id, label, keys, signals, onEdit }) {
    const [test, setTest] = useState(null);
    const [busy, setBusy] = useState(false);
    const ok = keys.every(([, k]) => k.configured || !k.secret);
    const runTest = async () => {
        setBusy(true);
        try { const { data } = await api.post(`/admin/integrations/test/${id}`); setTest(data); }
        catch (e) { setTest({ ok: false, detail: formatApiError(e) }); }
        finally { setBusy(false); }
    };
    const signal = id === "stripe" ? (signals.stripe_last_webhook ? `last webhook ${signals.stripe_last_webhook_type} · ${signals.stripe_last_webhook.slice(0, 16)}` : "no webhook received yet")
        : id === "email" ? (signals.email_last_sent ? `last e-mail ${signals.email_last_sent.slice(0, 16)}` : "no e-mail sent yet")
        : id === "turnstile" ? `login policy ${signals.turnstile_policy_enabled ? "ENABLED" : "disabled"} (toggle in User Management)` : null;
    return (
        <div data-testid={`integration-card-${id}`} className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    {ok ? <CheckCircle2 className="w-4 h-4 text-[#00FF41]" /> : <AlertTriangle className="w-4 h-4 text-[#FFB020]" />}
                    <h3 className="text-sm font-semibold text-[#E4E4E7]">{label}</h3>
                </div>
                <button onClick={runTest} disabled={busy} data-testid={`integration-test-${id}`}
                    className="px-2.5 py-1 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5 disabled:opacity-50">
                    {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} TEST LIVE
                </button>
            </div>
            {signal && <div className="text-[11px] font-mono text-[#52525B] mb-2">{signal}</div>}
            {test && <div data-testid={`integration-test-result-${id}`}
                className={`text-xs mb-2 px-2 py-1.5 border ${test.ok ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}>{test.detail}</div>}
            {keys.map(([name, k]) => <KeyRow key={name} name={name} k={k} onEdit={onEdit} />)}
        </div>
    );
}

export default function AdminIntegrations() {
    const [data, setData] = useState(null);
    const [edit, setEdit] = useState(null);
    const [form, setForm] = useState({ value: "", password: "", otp: "" });
    const [saving, setSaving] = useState(false);

    const load = async () => {
        try { const { data } = await api.get("/admin/integrations"); setData(data); }
        catch (e) { toast.error(formatApiError(e)); }
    };
    useEffect(() => { load(); }, []);

    const save = async (clear = false) => {
        setSaving(true);
        try {
            await api.post("/admin/integrations/secret", { key: edit, value: clear ? "" : form.value, password: form.password, otp: form.otp || null });
            toast.success(`${edit} ${clear ? "cleared" : "updated"} — applied to the API immediately`);
            setEdit(null); setForm({ value: "", password: "", otp: "" }); load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setSaving(false); }
    };

    return (
        <AppLayout>
            <PageHeader title="Integrations" subtitle="Stripe · Cloudflare Turnstile · E-mail · AI · Security Telegram — status, live tests and sealed key updates" />
            {!data ? <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div> : (
                <>
                    <div data-testid="integrations-vault-note" className="mb-5 text-[11px] font-mono text-[#71717A] border border-[#1F1F1F] px-3 py-2 flex items-start gap-2">
                        <ShieldCheck className="w-3.5 h-3.5 mt-0.5 text-[#00FF41]" />
                        <span>Values set here are sealed with AES-256-GCM (master key: {data.master_key_source === "dedicated" ? "dedicated SECRETS_MASTER_KEY" : "derived from JWT_SECRET — set SECRETS_MASTER_KEY before production"}), require your password{" "}
                            + authenticator code, and are written to the admin audit chain. {data.restart_hint}
                            {data.vault_readiness && !data.vault_readiness.ok && <span data-testid="vault-readiness-warning" className="block mt-1 text-[#FF3B30]">Vault blocker: {data.vault_readiness.detail}</span>}</span>
                    </div>
                    <div className="grid gap-4 md:grid-cols-2">
                        {ORDER.map(id => (
                            <ProviderCard key={id} id={id} label={data.providers[id]} signals={data.signals} onEdit={(k) => { setEdit(k); setForm({ value: "", password: "", otp: "" }); }}
                                keys={keysOf(data, id)} />
                        ))}
                        <SecurityAlertsCard keys={keysOf(data, "security_telegram")} onEdit={(k) => { setEdit(k); setForm({ value: "", password: "", otp: "" }); }} />
                    </div>
                    <div className="mt-6 space-y-6">
                        <PlansEditor />
                        <EmailTemplatesPanel />
                    </div>
                </>
            )}
            <Dialog open={!!edit} onOpenChange={(o) => !o && setEdit(null)}>
                <DialogContent data-testid="integration-secret-dialog" className="bg-[#0A0A0A] border-[#1F1F1F] text-[#E4E4E7]">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2 font-mono text-sm"><KeyRound className="w-4 h-4 text-[#00FF41]" /> {edit}</DialogTitle>
                        <DialogDescription className="text-[#71717A] text-xs">{edit && data?.keys[edit]?.description}. Re-authenticate to apply.</DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3">
                        <Input data-testid="integration-secret-value" type={edit && data?.keys[edit]?.secret ? "password" : "text"} placeholder="new value" value={form.value}
                            onChange={(e) => setForm({ ...form, value: e.target.value })} autoComplete="off" />
                        <Input data-testid="integration-secret-password" type="password" placeholder="your password" value={form.password}
                            onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="current-password" />
                        <Input data-testid="integration-secret-otp" inputMode="numeric" placeholder="authenticator code (if enabled)" value={form.otp}
                            onChange={(e) => setForm({ ...form, otp: e.target.value })} autoComplete="one-time-code" />
                        <div className="flex justify-between gap-2 pt-1">
                            <Button variant="ghost" data-testid="integration-secret-clear" disabled={saving || !form.password} onClick={() => save(true)}
                                className="text-[#FF3B30] hover:text-[#FF3B30]">Clear value</Button>
                            <Button data-testid="integration-secret-save" disabled={saving || !form.value || !form.password} onClick={() => save(false)}
                                className="bg-[#00FF41] text-black hover:bg-[#00FF41]/90 font-mono text-xs tracking-widest">
                                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "SAVE & APPLY"}
                            </Button>
                        </div>
                    </div>
                </DialogContent>
            </Dialog>
        </AppLayout>
    );
}
