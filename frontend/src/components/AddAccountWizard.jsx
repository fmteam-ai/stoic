import { useEffect, useMemo, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { QuickInstallPanel } from "@/components/QuickInstallPanel";
import { Loader2, Server, ChevronRight, ChevronLeft, CheckCircle2 } from "lucide-react";
import { VpsOfferButton, useVpsOffer } from "@/components/VpsOffer";

const STEPS = ["Broker & login", "VPS", "Review"];
// N110-9 — the account type feeds lot sizing; cent / micro-cent accounts must be selectable
const ACCOUNT_TYPES = [
    { id: "standard", label: "STANDARD", hint: "Regular lots (1 lot = 100 000 units)." },
    { id: "cent", label: "CENT", hint: "Balance shown in cents; lots sized 100× smaller." },
    { id: "microcent", label: "MICRO-CENT", hint: "Micro-cent account; smallest position sizes." },
    { id: "demo", label: "DEMO", hint: "Practice account — detected from the server name too (…-Demo)." },
];
const INPUT = "w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono text-white outline-none";
const LABEL = "font-mono text-[10px] text-[#52525B] tracking-widest block mb-1";

function Field({ id, label, children }) {
    return <div><label className={LABEL} htmlFor={id}>{label}</label>{children}</div>;
}

/** Easy-Connect: one guided form — broker → server → login → VPS → review → account created → Quick Install code. */
export function AddAccountWizard({ open, onClose, pairedHosts = [], onCreated, onAdvanced }) {
    const [step, setStep] = useState(0);
    const [presets, setPresets] = useState([]);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const [created, setCreated] = useState(null);
    const [f, setF] = useState({ mode: "live", environment: "demo", consent: false, broker: "", server: "", account_number: "", label: "", account_type: "microcent", base_currency: "USD", vps: pairedHosts[0] ? pairedHosts[0] : "new" });
    const [demoPolicy, setDemoPolicy] = useState(false);
    const set = (k, v) => setF(p => ({ ...p, [k]: v }));
    const vpsOffer = useVpsOffer();
    useEffect(() => {
        if (!open) return;
        setStep(0); setCreated(null); setErr("");
        api.get("/accounts/broker-presets").then(r => { setPresets(r.data.presets || []); setDemoPolicy(!!r.data.demo_only_policy); }).catch(() => setPresets([]));
    }, [open]);
    const preset = useMemo(() => presets.find(p => p.broker === f.broker), [presets, f.broker]);
    const servers = preset?.servers || [];
    const brokerName = f.broker === "__other" ? (f.broker_other || "") : f.broker;
    const realBlocked = f.mode === "live" && f.environment === "real" && demoPolicy;
    const canNext = step === 0
        ? (f.mode === "paper" ? !!f.account_number : !!(brokerName && f.server && f.account_number) && !realBlocked)
        : true;
    const canCreate = f.mode === "paper" || f.consent;   // A17-13 — explicit Algo-Trading consent for MT5 terminals

    const create = async () => {
        setBusy(true); setErr("");
        try {
            const label = f.label || (f.mode === "paper" ? `Paper ${f.account_number}` : `${brokerName} …${String(f.account_number).slice(-3)}`);
            const payload = { label, broker: f.mode === "paper" ? "" : brokerName, server: f.mode === "paper" ? "" : f.server,
                account_number: f.account_number, account_type: f.account_type, account_role: "STANDARD", base_currency: f.base_currency,
                mode: f.mode, initial_balance: 10000, pamm_provider: null, pamm_program_id: null, pamm_broker_program_id: null,
                declared_environment: f.mode === "paper" ? null : f.environment, algo_trading_consent: f.mode !== "paper" && f.consent };
            const { data } = await api.post("/accounts", payload);
            setCreated(data); onCreated?.(data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setBusy(false); }
    };

    return (
        <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
            <DialogContent className="bg-[#0A0A0A] border-[#1F1F1F] text-[#E4E4E7] max-w-2xl" data-testid="add-account-wizard">
                <DialogHeader>
                    <DialogTitle className="font-display tracking-tight">{created ? "Account created — connect it" : "Add a broker account"}</DialogTitle>
                    <DialogDescription className="text-[#71717A] text-xs">
                        {created ? "One code, one PowerShell line on your VPS. Nothing to type in the EA." : "Three steps. You never open MetaEditor or edit EA inputs."}
                    </DialogDescription>
                </DialogHeader>
                {!created && (
                    <div className="flex items-center gap-2 font-mono text-[10px] tracking-widest" data-testid="wizard-steps">
                        {STEPS.map((s, i) => (
                            <span key={s} className={`px-2 py-0.5 border ${i === step ? "border-[#00FF41] text-[#00FF41]" : i < step ? "border-[#00FF41]/30 text-[#00FF41]/70" : "border-[#1F1F1F] text-[#52525B]"}`} data-testid={`wizard-step-${i}`}>
                                {i + 1} · {s.toUpperCase()}
                            </span>
                        ))}
                    </div>
                )}

                {created ? (
                    <div className="space-y-3" data-testid="wizard-done">
                        <div className="flex items-center gap-2 text-sm text-[#00FF41]"><CheckCircle2 className="w-4 h-4" /> {created.label || "Account"} added{f.mode === "paper" ? " — paper account, no terminal needed." : "."}</div>
                        {f.mode !== "paper" && (
                            <>
                                {f.vps === "new" && <div className="flex items-center justify-between gap-3 text-xs text-[#A1A1AA] border border-[#FFD700]/20 px-3 py-2"><span>No VPS yet? Order it first, install MT5 there and log in — then come back for the code (valid 60 min).</span><VpsOfferButton compact testid="wizard-done-vps-offer-link" /></div>}
                                {f.vps !== "new" && <div className="text-xs text-[#A1A1AA]">Run the one-liner on <code className="text-[#FFD700] font-mono">{f.vps}</code> (the VPS you already use) with MT5 logged into <code className="font-mono">{f.account_number}</code>.</div>}
                                <QuickInstallPanel accountId={created.id} accountLabel={created.label} account={created} />
                            </>
                        )}
                        <button onClick={onClose} data-testid="wizard-close-btn" className="w-full px-3 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#52525B] text-[#A1A1AA]">DONE</button>
                    </div>
                ) : step === 0 ? (
                    <div className="space-y-3" data-testid="wizard-step-broker">
                        <div className="flex gap-2">
                            {["live", "paper"].map(m => (
                                <button key={m} type="button" onClick={() => set("mode", m)} data-testid={`wizard-mode-${m}`}
                                    className={`flex-1 py-2 text-xs font-mono tracking-widest border ${f.mode === m ? "border-[#00FF41] bg-[#00FF41]/10 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"}`}>
                                    {m === "live" ? "MT5 TERMINAL" : "PAPER SANDBOX"}
                                </button>
                            ))}
                        </div>
                        {f.mode === "live" && (
                            <Field id="wz-env" label="DEMO OR REAL MONEY?">
                                <div className="flex gap-2" data-testid="wizard-environment">
                                    {[["demo", "DEMO ACCOUNT"], ["real", "REAL MONEY"]].map(([id, lbl]) => (
                                        <button key={id} type="button" onClick={() => set("environment", id)} data-testid={`wizard-env-${id}`}
                                            className={`flex-1 py-1.5 text-xs font-mono tracking-widest border ${f.environment === id ? (id === "real" ? "border-[#FF3B30] bg-[#FF3B30]/10 text-[#FF3B30]" : "border-[#00FF41] bg-[#00FF41]/10 text-[#00FF41]") : "border-[#1F1F1F] text-[#A1A1AA]"}`}>
                                            {lbl}
                                        </button>
                                    ))}
                                </div>
                                {realBlocked && <p className="text-[11px] text-[#FF3B30] mt-1" data-testid="wizard-real-blocked">A signed DEMO-only policy is in force — real-money accounts cannot be added until the admin replaces it.</p>}
                                {!realBlocked && f.environment === "real" && <p className="text-[11px] text-[#FFD700] mt-1">Real money: the EA must attest the account as live; trading stays OFF until you enable it per bot.</p>}
                            </Field>
                        )}
                        {f.mode === "live" && (
                            <div className="grid sm:grid-cols-2 gap-3">
                                <Field id="wz-broker" label="BROKER">
                                    <select id="wz-broker" value={f.broker} onChange={e => { set("broker", e.target.value); const p = presets.find(x => x.broker === e.target.value); set("server", p?.servers?.[0] || ""); if (p?.account_types?.[0]) set("account_type", p.account_types[0]); }}
                                        className={INPUT} data-testid="wizard-broker-select">
                                        <option value="">— pick your broker —</option>
                                        {presets.map(p => <option key={p.broker} value={p.broker}>{p.broker}</option>)}
                                        <option value="__other">Other (type below)</option>
                                    </select>
                                    {f.broker === "__other" && <input className={`${INPUT} mt-1`} placeholder="Broker name" onChange={e => set("broker_other", e.target.value)} data-testid="wizard-broker-other" />}
                                </Field>
                                <Field id="wz-server" label="SERVER">
                                    {servers.length > 0
                                        ? <select id="wz-server" value={f.server} onChange={e => set("server", e.target.value)} className={INPUT} data-testid="wizard-server-select">
                                            {servers.map(s => <option key={s} value={s}>{s}</option>)}
                                          </select>
                                        : <input id="wz-server" value={f.server} onChange={e => set("server", e.target.value)} placeholder="e.g. RoboForex-ECN" className={INPUT} data-testid="wizard-server-input" />}
                                </Field>
                            </div>
                        )}
                        <div className="grid sm:grid-cols-2 gap-3">
                            <Field id="wz-login" label={f.mode === "paper" ? "SANDBOX ID" : "MT5 LOGIN"}>
                                <input id="wz-login" value={f.account_number} onChange={e => set("account_number", e.target.value.trim())} placeholder="12345678" className={INPUT} data-testid="wizard-login-input" />
                            </Field>
                            <Field id="wz-label" label="LABEL (OPTIONAL)">
                                <input id="wz-label" value={f.label} onChange={e => set("label", e.target.value)} placeholder={f.mode === "paper" ? "Paper Sandbox" : "My demo account"} className={INPUT} data-testid="wizard-label-input" />
                            </Field>
                        </div>
                        {f.mode === "live" && (
                            <Field id="wz-type" label="ACCOUNT TYPE (DRIVES LOT SIZING)">
                                <div className="flex flex-wrap gap-2" data-testid="wizard-account-type">
                                    {ACCOUNT_TYPES.filter(t => !preset?.account_types || preset.account_types.includes(t.id)).map(t => (
                                        <button key={t.id} type="button" onClick={() => set("account_type", t.id)} data-testid={`wizard-account-type-${t.id}`}
                                            className={`px-3 py-1.5 text-xs font-mono tracking-widest border ${f.account_type === t.id ? "border-[#00FF41] bg-[#00FF41]/10 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"}`}>
                                            {t.label}
                                        </button>
                                    ))}
                                </div>
                                <p className="text-[11px] text-[#52525B] mt-1">{ACCOUNT_TYPES.find(t => t.id === f.account_type)?.hint}</p>
                            </Field>
                        )}
                        <p className="text-[11px] text-[#52525B]">The trading password stays in MT5 on your VPS — STOIC never asks for it.</p>
                    </div>
                ) : step === 1 ? (
                    <div className="space-y-2" data-testid="wizard-step-vps">
                        {f.mode === "paper" ? <p className="text-sm text-[#A1A1AA]">Paper accounts run inside STOIC — no VPS or MT5 terminal needed.</p> : (
                            <>
                                {pairedHosts.map(h => (
                                    <button key={h} type="button" onClick={() => set("vps", h)} data-testid={`wizard-vps-${h}`}
                                        className={`w-full flex items-center gap-3 px-3 py-2 border text-left ${f.vps === h ? "border-[#00FF41] bg-[#00FF41]/5" : "border-[#1F1F1F]"}`}>
                                        <Server className="w-4 h-4 text-[#00FF41]" />
                                        <div><div className="text-sm text-white font-mono">{h}</div><div className="text-[11px] text-[#52525B]">VPS you already connected — you'll run the install line on this VPS, with this account logged into MT5 there (a second terminal is fine)</div></div>
                                    </button>
                                ))}
                                <button type="button" onClick={() => set("vps", "new")} data-testid="wizard-vps-new"
                                    className={`w-full flex items-center gap-3 px-3 py-2 border text-left ${f.vps === "new" ? "border-[#00FF41] bg-[#00FF41]/5" : "border-[#1F1F1F]"}`}>
                                    <Server className="w-4 h-4 text-[#FFD700]" />
                                    <div><div className="text-sm text-white">A new Windows VPS</div><div className="text-[11px] text-[#52525B]">We recommend <strong className="text-[#FFD700]">{vpsOffer.provider}</strong> — 2 vCPU / 4 GB runs up to 4 terminals, datacentres next to the brokers. Install MT5 from your broker there, log in, then paste the one-liner.</div></div>
                                </button>
                                {f.vps === "new" && <div className="pl-10" data-testid="wizard-vps-offer"><VpsOfferButton testid="wizard-vps-offer-link" /></div>}
                            </>
                        )}
                    </div>
                ) : (
                    <div className="space-y-2 text-sm" data-testid="wizard-step-review">
                        {[["Mode", f.mode === "paper" ? "Paper sandbox" : `MT5 terminal · ${f.environment === "real" ? "REAL MONEY" : "DEMO"} (trading stays OFF until you enable it)`], ...(f.mode === "live" ? [["Broker", brokerName], ["Server", f.server], ["Account type", ACCOUNT_TYPES.find(t => t.id === f.account_type)?.label || f.account_type], ["VPS", f.vps === "new" ? "new Windows VPS" : `${f.vps} (you'll run the install line there)`]] : []), ["Login", f.account_number], ["Label", f.label || "(auto)"]]
                            .map(([k, v]) => <div key={k} className="flex justify-between border-b border-[#1F1F1F] py-1.5"><span className="text-[#52525B] font-mono text-xs">{k.toUpperCase()}</span><span className="font-mono text-xs text-white">{v}</span></div>)}
                        {f.mode !== "paper" && (
                            <label className="flex items-start gap-2 border border-[#FFD700]/40 bg-[#FFD700]/5 px-3 py-2 cursor-pointer" data-testid="wizard-consent">
                                <input type="checkbox" checked={f.consent} onChange={e => set("consent", e.target.checked)} className="mt-0.5 accent-[#00FF41]" data-testid="wizard-consent-checkbox" />
                                <span className="text-[11px] text-[#E4E4E7]">I understand that the installer turns <strong>Algo Trading</strong> on in this MT5 terminal and that, once I enable a bot, STOIC will open and close positions on account <span className="font-mono">{f.account_number}</span>{f.environment === "real" ? " with real money" : ""}. Trading stays OFF until I enable it.</span>
                            </label>
                        )}
                        <p className="text-[11px] text-[#52525B] pt-1">Next: the account is created and you get a 60-minute code with the PowerShell line to run on the VPS.</p>
                    </div>
                )}

                {err && <div className="text-xs text-[#FF3B30] border border-[#FF3B30]/40 px-2 py-1.5" data-testid="wizard-error">{err}</div>}
                {!created && (
                    <div className="flex items-center justify-between pt-2">
                        <button type="button" onClick={onAdvanced} className="text-[11px] text-[#52525B] hover:text-[#A1A1AA] underline" data-testid="wizard-advanced-link">Advanced form (PAMM, balances)</button>
                        <div className="flex gap-2">
                            {step > 0 && <button type="button" onClick={() => setStep(step - 1)} data-testid="wizard-back-btn" className="px-3 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] flex items-center gap-1"><ChevronLeft className="w-3 h-3" /> BACK</button>}
                            {step < 2
                                ? <button type="button" disabled={!canNext} onClick={() => setStep(step + 1)} data-testid="wizard-next-btn" className="px-3 py-2 text-xs font-mono tracking-widest bg-[#00FF41] text-black disabled:opacity-40 flex items-center gap-1">NEXT <ChevronRight className="w-3 h-3" /></button>
                                : <button type="button" disabled={busy || !canCreate} onClick={create} data-testid="wizard-create-btn" className="px-3 py-2 text-xs font-mono tracking-widest bg-[#00FF41] text-black disabled:opacity-40 flex items-center gap-1">{busy ? <Loader2 className="w-3 h-3 animate-spin" /> : null} CREATE & CONNECT</button>}
                        </div>
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
}

export default AddAccountWizard;
