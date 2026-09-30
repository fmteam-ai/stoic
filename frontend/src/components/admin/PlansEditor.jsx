import { useEffect, useMemo, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, CreditCard, RotateCcw } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

const TIER_LABEL = { starter: "Starter", trader: "Trader", professional: "Professional", elite_ai: "Elite AI" };
const DUR = [["monthly", "Monthly", 1], ["quarterly", "3 Months", 3], ["semi_annual", "6 Months", 6], ["annual", "Annual", 12]];
const fmt = (cents, sym) => `${sym}${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const toCents = (v) => Math.round(parseFloat(String(v).replace(",", ".")) * 100);

function Field({ label, children, testid }) {
    return <label className="block" data-testid={testid}>
        <span className="block text-[10px] font-mono tracking-widest text-[#71717A] mb-1">{label}</span>{children}</label>;
}

export function PlansEditor() {
    const [cfg, setCfg] = useState(null);
    const [form, setForm] = useState(null);
    const [auth, setAuth] = useState(null);
    const [saving, setSaving] = useState(false);

    const fromCfg = (c) => ({
        base: Object.fromEntries(c.tiers.map(t => [t, (c.base_cents[t] / 100).toFixed(2)])),
        discounts: { ...c.discounts }, currency: c.currency, trial_days: String(c.trial_days), trial_tier: c.trial_tier,
    });
    const load = async () => {
        try { const { data } = await api.get("/admin/integrations/plans"); setCfg(data); setForm(fromCfg(data)); }
        catch (e) { toast.error(formatApiError(e)); }
    };
    useEffect(() => { load(); }, []);

    const sym = useMemo(() => {
        const m = { usd: "$", eur: "€", gbp: "£", chf: "CHF ", aud: "A$", cad: "C$" };
        return form ? (m[form.currency] || form.currency.toUpperCase() + " ") : "$";
    }, [form]);

    const matrix = useMemo(() => {
        if (!form) return [];
        return Object.keys(TIER_LABEL).map(t => {
            const base = toCents(form.base[t]);
            return { tier: t, cells: DUR.map(([d, , months]) => {
                const disc = Number(form.discounts[d] ?? 0);
                return Number.isFinite(base) ? Math.round(base * months * (100 - disc) / 100) : NaN;
            }) };
        });
    }, [form]);

    const dirty = cfg && form && JSON.stringify(fromCfg(cfg)) !== JSON.stringify(form);

    const save = async () => {
        setSaving(true);
        try {
            const payload = {
                base_cents: Object.fromEntries(Object.entries(form.base).map(([t, v]) => [t, toCents(v)])),
                discounts: Object.fromEntries(Object.entries(form.discounts).map(([d, v]) => [d, Number(v)])),
                currency: form.currency, trial_days: Number(form.trial_days), trial_tier: form.trial_tier,
                password: auth.password, otp: auth.otp || null,
            };
            const { data } = await api.post("/admin/integrations/plans", payload);
            setCfg(data); setForm(fromCfg(data)); setAuth(null);
            toast.success("Plans updated — new prices are live on /subscription");
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setSaving(false); }
    };

    if (!cfg || !form) return <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /></div>;

    const inputCls = "h-8 bg-[#0F0F0F] border-[#1F1F1F] font-mono text-xs";
    return (
        <div data-testid="plans-editor" className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <CreditCard className="w-4 h-4 text-[#00FF41]" />
                    <h3 className="text-sm font-semibold text-[#E4E4E7]">Stripe plans — pricing · trial · currency</h3>
                </div>
                <div className="flex items-center gap-2">
                    <button data-testid="plans-reset-defaults" onClick={() => setForm({
                        base: Object.fromEntries(cfg.tiers.map(t => [t, (cfg.defaults.base_cents[t] / 100).toFixed(2)])),
                        discounts: { ...cfg.defaults.discounts }, currency: cfg.defaults.currency,
                        trial_days: String(cfg.defaults.trial_days), trial_tier: cfg.defaults.trial_tier })}
                        className="px-2.5 py-1 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:text-[#E4E4E7] flex items-center gap-1.5">
                        <RotateCcw className="w-3 h-3" /> DEFAULTS</button>
                    <button data-testid="plans-save" disabled={!dirty} onClick={() => setAuth({ password: "", otp: "" })}
                        className="px-2.5 py-1 text-[10px] font-mono tracking-widest bg-[#00FF41] text-black disabled:opacity-40 hover:bg-[#00FF41]/90">
                        SAVE & APPLY</button>
                </div>
            </div>
            {cfg.updated_at && <div className="text-[11px] font-mono text-[#52525B] mb-3">last change {cfg.updated_at.slice(0, 16).replace("T", " ")} by {cfg.updated_by}</div>}

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mb-4">
                {cfg.tiers.map(t => (
                    <Field key={t} label={`${TIER_LABEL[t]} · monthly base`} testid={`plan-base-field-${t}`}>
                        <div className="relative">
                            <span className="absolute left-2 top-1/2 -translate-y-1/2 text-xs font-mono text-[#71717A]">{sym.trim()}</span>
                            <Input data-testid={`plan-base-${t}`} value={form.base[t]} inputMode="decimal" className={`${inputCls} pl-9`}
                                onChange={(e) => setForm({ ...form, base: { ...form.base, [t]: e.target.value } })} />
                        </div>
                    </Field>
                ))}
            </div>
            <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6 mb-4">
                {DUR.filter(([d]) => d !== "monthly").map(([d, l]) => (
                    <Field key={d} label={`${l} discount %`}>
                        <Input data-testid={`plan-discount-${d}`} value={form.discounts[d]} inputMode="numeric" className={inputCls}
                            onChange={(e) => setForm({ ...form, discounts: { ...form.discounts, [d]: e.target.value } })} />
                    </Field>
                ))}
                <Field label="Currency">
                    <select data-testid="plan-currency" value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })}
                        className="h-8 w-full bg-[#0F0F0F] border border-[#1F1F1F] font-mono text-xs text-[#E4E4E7] px-2">
                        {cfg.supported_currencies.map(c => <option key={c} value={c}>{c.toUpperCase()}</option>)}
                    </select>
                </Field>
                <Field label="Free trial days (0 = off)">
                    <Input data-testid="plan-trial-days" value={form.trial_days} inputMode="numeric" className={inputCls}
                        onChange={(e) => setForm({ ...form, trial_days: e.target.value })} />
                </Field>
                <Field label="Trial tier">
                    <select data-testid="plan-trial-tier" value={form.trial_tier} onChange={(e) => setForm({ ...form, trial_tier: e.target.value })}
                        className="h-8 w-full bg-[#0F0F0F] border border-[#1F1F1F] font-mono text-xs text-[#E4E4E7] px-2">
                        {cfg.tiers.map(t => <option key={t} value={t}>{TIER_LABEL[t]}</option>)}
                    </select>
                </Field>
            </div>
            <div className="text-[11px] font-mono text-[#71717A] mb-3" data-testid="plan-trial-note">
                {Number(form.trial_days) > 0
                    ? `New sign-ups get ${form.trial_days} days of ${TIER_LABEL[form.trial_tier]} free${cfg.trial_enabled_at ? ` (active for accounts created after ${cfg.trial_enabled_at.slice(0, 10)})` : " — starts for accounts created after you save"}.`
                    : "Free trial disabled — new sign-ups start on Starter."}
            </div>

            <div className="overflow-x-auto">
                <table className="w-full text-xs font-mono" data-testid="plan-matrix">
                    <thead><tr className="text-[#71717A] text-[10px] tracking-widest">
                        <th className="text-left py-1.5">TIER</th>{DUR.map(([d, l]) => <th key={d} className="text-right py-1.5">{l.toUpperCase()}</th>)}
                    </tr></thead>
                    <tbody>{matrix.map(r => (
                        <tr key={r.tier} className="border-t border-[#1F1F1F]" data-testid={`plan-matrix-row-${r.tier}`}>
                            <td className="py-1.5 text-[#E4E4E7]">{TIER_LABEL[r.tier]}</td>
                            {r.cells.map((c, i) => <td key={i} className="py-1.5 text-right text-[#A1A1AA]">{Number.isFinite(c) ? fmt(c, sym) : "—"}</td>)}
                        </tr>
                    ))}</tbody>
                </table>
            </div>

            <Dialog open={!!auth} onOpenChange={(o) => !o && setAuth(null)}>
                <DialogContent data-testid="plans-reauth-dialog" className="bg-[#0A0A0A] border-[#1F1F1F] text-[#E4E4E7]">
                    <DialogHeader>
                        <DialogTitle className="font-mono text-sm">Apply new plan pricing</DialogTitle>
                        <DialogDescription className="text-[#71717A] text-xs">Prices go live immediately for every new checkout. Re-authenticate to confirm.</DialogDescription>
                    </DialogHeader>
                    {auth && <div className="space-y-3">
                        <Input data-testid="plans-reauth-password" type="password" placeholder="your password" value={auth.password}
                            onChange={(e) => setAuth({ ...auth, password: e.target.value })} autoComplete="current-password" />
                        <Input data-testid="plans-reauth-otp" inputMode="numeric" placeholder="authenticator code (if enabled)" value={auth.otp}
                            onChange={(e) => setAuth({ ...auth, otp: e.target.value })} autoComplete="one-time-code" />
                        <div className="flex justify-end pt-1">
                            <Button data-testid="plans-reauth-confirm" disabled={saving || !auth.password} onClick={save}
                                className="bg-[#00FF41] text-black hover:bg-[#00FF41]/90 font-mono text-xs tracking-widest">
                                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "CONFIRM & APPLY"}
                            </Button>
                        </div>
                    </div>}
                </DialogContent>
            </Dialog>
        </div>
    );
}
