import { useEffect, useMemo, useState } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { Dialog, DialogContent, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { VpsOfferButton, useVpsOffer } from "@/components/VpsOffer";
import { toast } from "sonner";
import { Loader2, CheckCircle2, Circle, Copy, Terminal, ClipboardPaste, CornerDownLeft, Download, ChevronDown } from "lucide-react";

const STEPS = ["Broker", "Connect", "Activate", "Start"];
const copy = async (t, l) => { try { await navigator.clipboard.writeText(t); toast.success(`${l} copied`); } catch { toast.error("Clipboard blocked — select & copy"); } };

function TopBar({ screen }) {
    return (
        <ol className="flex items-center gap-1 font-mono text-[10px] tracking-widest" data-testid="wizard-topbar">
            {STEPS.map((s, i) => {
                const n = i + 1, st = n < screen ? "done" : n === screen ? "now" : "todo";
                return (<li key={s} className="flex items-center gap-1">
                    <span className={`px-1.5 py-0.5 border ${st === "done" ? "border-[#00FF41]/50 text-[#00FF41]" : st === "now" ? "border-[#FFD700] text-[#FFD700]" : "border-[#1F1F1F] text-[#52525B]"}`} data-testid={`wizard-step-${n}`} data-state={st}>{n} {s.toUpperCase()}</span>
                    {i < 3 && <span className="text-[#3F3F46]">→</span>}
                </li>);
            })}
        </ol>
    );
}

function ErrorLine({ error, onAction, busy }) {
    if (!error) return null;
    return (
        <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 p-3 flex flex-wrap items-center gap-2" data-testid="wizard-error" data-code={error.code}>
            <span className="text-sm text-white flex-1 min-w-[12rem]" data-testid="wizard-error-text">{error.text}</span>
            {error.buttons.map((b) => (
                <button key={b.action} onClick={() => onAction(b)} disabled={busy} data-testid={`wizard-error-btn-${b.action}`}
                    className={`px-3 py-1.5 text-xs font-mono tracking-widest disabled:opacity-50 ${b.action === "wait" ? "border border-[#1F1F1F] text-[#A1A1AA] hover:text-white" : "bg-[#FFD700] text-black hover:bg-[#E6C200]"}`}>{b.label.toUpperCase()}</button>
            ))}
        </div>
    );
}

function Tick({ done, status, label, testid }) {
    const Icon = done ? CheckCircle2 : status === "blocked" ? Circle : Circle;
    return (<li className="flex items-center gap-2 text-sm" data-testid={testid} data-done={done ? "1" : "0"}>
        <Icon className={`w-4 h-4 ${done ? "text-[#00FF41]" : status === "blocked" || status === "warn" ? "text-[#FF3B30]" : "text-[#52525B]"}`} />
        <span className={done ? "text-white" : "text-[#A1A1AA]"}>{label}</span>
        {!done && status === "waiting" && <Loader2 className="w-3 h-3 animate-spin text-[#52525B]" />}
    </li>);
}

// ── Screen 1 ──────────────────────────────────────────────────────────────────────────────────────────
function BrokerScreen({ onCreated }) {
    const [presets, setPresets] = useState([]);
    const [demoOnly, setDemoOnly] = useState(false);
    const [q, setQ] = useState("");
    const [f, setF] = useState({ broker: "", other: "", login: "", env: "demo", label: "" });
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    useEffect(() => { api.get("/accounts/broker-presets").then((r) => { setPresets(r.data.presets || []); setDemoOnly(!!r.data.demo_only_policy); }).catch(() => {}); }, []);
    const list = useMemo(() => presets.filter((p) => !q || p.broker.toLowerCase().includes(q.toLowerCase())), [presets, q]);
    const brokerName = f.broker === "__other" ? f.other.trim() : f.broker;
    const ok = brokerName && /^\d{3,12}$/.test(f.login);
    const create = async () => {
        setBusy(true); setErr(null);
        try {
            const label = f.label.trim() || `${brokerName} …${f.login.slice(-3)}`;
            const { data } = await api.post("/accounts", { label, broker: brokerName, server: "", account_number: f.login, account_type: f.env === "demo" ? "demo" : "standard", account_role: "STANDARD",
                base_currency: "USD", mode: "live", initial_balance: 10000, pamm_provider: null, pamm_program_id: null, pamm_broker_program_id: null,
                declared_environment: f.env, algo_trading_consent: true });
            await api.put("/bot/config", { risk_level: "medium" }, { params: { account_id: data.id } }).catch(() => {});   // bound bot, Balanced
            onCreated(data);
        } catch (e) { setErr(formatApiError(e)); } finally { setBusy(false); }
    };
    return (
        <div className="space-y-4" data-testid="wizard-screen-broker">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your broker…" data-testid="wizard-broker-search"
                className="w-full bg-black border border-[#1F1F1F] px-3 py-2 text-sm text-white focus:border-[#FFD700] outline-none" />
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 max-h-44 overflow-y-auto pr-1" data-testid="wizard-broker-list">
                {list.map((p) => (
                    <button key={p.broker} onClick={() => setF({ ...f, broker: p.broker })} data-testid={`wizard-broker-${p.broker.replace(/\W+/g, "-").toLowerCase()}`}
                        className={`flex items-center gap-2 border px-2 py-2 text-left text-xs ${f.broker === p.broker ? "border-[#FFD700] bg-[#FFD700]/10 text-white" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#3F3F46]"}`}>
                        <span className="w-6 h-6 shrink-0 grid place-items-center bg-[#18181B] text-[10px] font-bold text-[#FFD700]">{p.broker.slice(0, 2).toUpperCase()}</span>
                        <span className="truncate">{p.broker}</span>
                    </button>
                ))}
                <button onClick={() => setF({ ...f, broker: "__other" })} data-testid="wizard-broker-other"
                    className={`border px-2 py-2 text-left text-xs ${f.broker === "__other" ? "border-[#FFD700] bg-[#FFD700]/10 text-white" : "border-dashed border-[#3F3F46] text-[#A1A1AA]"}`}>Other MT5 broker</button>
            </div>
            {f.broker === "__other" && <input value={f.other} onChange={(e) => setF({ ...f, other: e.target.value })} placeholder="Broker name" data-testid="wizard-broker-other-name"
                className="w-full bg-black border border-[#1F1F1F] px-3 py-2 text-sm text-white focus:border-[#FFD700] outline-none" />}
            <div className="grid sm:grid-cols-3 gap-3">
                <label className="block text-xs text-[#A1A1AA]">MT5 login number
                    <input value={f.login} onChange={(e) => setF({ ...f, login: e.target.value.replace(/\D/g, "") })} inputMode="numeric" placeholder="e.g. 1083440" data-testid="wizard-login"
                        className="mt-1 w-full bg-black border border-[#1F1F1F] px-3 py-2 text-sm text-white font-mono focus:border-[#FFD700] outline-none" /></label>
                <div className="text-xs text-[#A1A1AA]">Account type — <span className="text-white">is this MT5 login a demo or a real-money account?</span>
                    <div className="mt-1 grid grid-cols-2 border border-[#1F1F1F]" data-testid="wizard-env-toggle">
                        {[["demo", "DEMO", "practice money"], ["real", "LIVE", "real money"]].map(([v, title, sub]) => (
                            <button key={v} onClick={() => setF({ ...f, env: v })} disabled={v === "real" && demoOnly} data-testid={`wizard-env-${v}`}
                                className={`py-2 text-xs font-mono tracking-widest disabled:opacity-40 ${f.env === v ? "bg-[#FFD700] text-black" : "text-[#A1A1AA]"}`}>
                                {title}<span className={`block text-[10px] tracking-normal ${f.env === v ? "text-black/70" : "text-[#71717A]"}`}>{sub}</span></button>
                        ))}
                    </div>
                    <div className="mt-1 text-[10px] text-[#71717A]" data-testid="wizard-env-hint">The broker's own trade mode is checked on the first heartbeat; a wrong choice is corrected automatically for demo accounts.</div>
                    {demoOnly && <div className="mt-1 text-[10px] text-[#71717A]">Demo-only period — real accounts after the operator opens it.</div>}
                </div>
                <label className="block text-xs text-[#A1A1AA]">Name (optional)
                    <input value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="My demo" data-testid="wizard-label"
                        className="mt-1 w-full bg-black border border-[#1F1F1F] px-3 py-2 text-sm text-white focus:border-[#FFD700] outline-none" /></label>
            </div>
            <div className="text-[11px] text-[#52525B]">No server to type — STOIC takes it from your terminal's first heartbeat.</div>
            {err && <div className="text-xs text-[#FF3B30]" data-testid="wizard-create-error">{err}</div>}
            <button onClick={create} disabled={!ok || busy} data-testid="wizard-next-btn"
                className="w-full bg-[#FFD700] hover:bg-[#E6C200] disabled:opacity-40 text-black font-bold py-3 text-sm font-mono tracking-widest flex items-center justify-center gap-2">
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null} NEXT</button>
        </div>
    );
}

// ── Screen 2 ──────────────────────────────────────────────────────────────────────────────────────────
function ConnectScreen({ account, state, refresh }) {
    const [line, setLine] = useState(null);
    const [mode, setMode] = useState("own");
    const [busy, setBusy] = useState(false);
    const offer = useVpsOffer();
    const generate = (repair = false) => api.post("/setup/pairing-token", { account_id: account.id, repair }).then((r) => setLine({ ...r.data, repair })).catch((e) => toast.error(formatApiError(e)));
    useEffect(() => { generate(false); }, [account.id]); // eslint-disable-line react-hooks/exhaustive-deps
    const onAction = async (b) => {
        setBusy(true);
        try {
            if (b.action === "repair") { await generate(true); toast.success("Repair line ready — same three steps"); }
            else if (b.action === "check_again") await refresh();
            else if (b.action === "use_reported_login") { await api.post(`/setup/wizard/${account.id}/use-reported-login`); toast.success(`Using #${b.login}`); await refresh(); }
            else if (b.action === "switch_real") { await api.patch(`/accounts/${account.id}`, { declared_environment: "real" }).catch(() => {}); await refresh(); }
            else await refresh();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };
    const cmdUrl = line?.cmd_download_path ? `${API.replace(/\/api$/, "")}${line.cmd_download_path}` : null;
    return (
        <div className="space-y-4" data-testid="wizard-screen-connect">
            <div className="grid grid-cols-2 border border-[#1F1F1F]" data-testid="wizard-connect-mode">
                <button onClick={() => setMode("own")} data-testid="wizard-mode-own" className={`py-2 text-xs font-mono tracking-widest ${mode === "own" ? "bg-[#18181B] text-white" : "text-[#A1A1AA]"}`}>MY OWN WINDOWS VPS / PC</button>
                <button onClick={() => setMode("stoic")} data-testid="wizard-mode-stoic" className={`py-2 text-xs font-mono tracking-widest ${mode === "stoic" ? "bg-[#18181B] text-white" : "text-[#A1A1AA]"}`}>STOIC VPS</button>
            </div>
            {mode === "stoic" ? (
                <div className="text-sm text-[#A1A1AA] space-y-2" data-testid="wizard-stoic-vps">Get a Windows VPS with MT5 pre-installed, then come back here and use the one-line installer on it.<div><VpsOfferButton testid="wizard-vps-offer" /></div><div className="text-[10px] text-[#52525B]">{offer.provider}</div></div>
            ) : (
                <div className="space-y-3">
                    <div className="flex items-start gap-2">
                        <code className="flex-1 text-[10px] font-mono text-[#FFD700] bg-black border border-[#1F1F1F] p-2 break-all select-all min-h-[2.5rem]" data-testid="wizard-command">{line?.install_command || "generating…"}</code>
                        <button onClick={() => line && copy(line.install_command, line.repair ? "Repair line" : "Installer line")} data-testid="wizard-copy-btn"
                            className="px-3 py-2 bg-[#FFD700] text-black font-mono text-xs tracking-widest hover:bg-[#E6C200] flex items-center gap-1"><Copy className="w-3.5 h-3.5" /> COPY</button>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-center" data-testid="wizard-mini-guide">
                        {[[Terminal, "Open PowerShell as Administrator"], [ClipboardPaste, "Paste the line"], [CornerDownLeft, "Press Enter"]].map(([I, t], i) => (
                            <div key={t} className="border border-[#1F1F1F] p-2"><I className="w-5 h-5 mx-auto text-[#FFD700]" /><div className="mt-1 text-[10px] text-[#A1A1AA]">{i + 1}. {t}</div></div>
                        ))}
                    </div>
                    {cmdUrl && <a href={cmdUrl} data-testid="wizard-cmd-download" className="text-[11px] text-[#71717A] underline flex items-center gap-1"><Download className="w-3 h-3" /> prefer a file? download STOIC-Setup.cmd and double-click it on the VPS</a>}
                </div>
            )}
            <ul className="space-y-1.5 border-t border-[#1F1F1F] pt-3" data-testid="wizard-checklist">
                {state.connect.checklist.map((c) => <Tick key={c.id} done={c.done} status={c.status} label={c.label} testid={`wizard-check-${c.id}`} />)}
            </ul>
            <ErrorLine error={state.connect.error} onAction={onAction} busy={busy} />
            {!state.connect.error && !state.connect.done && <div className="text-[11px] text-[#52525B] flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> listening for your terminal — this page updates by itself</div>}
        </div>
    );
}

// ── Screen 3 ──────────────────────────────────────────────────────────────────────────────────────────
function ActivateScreen({ account, state, refresh }) {
    const [busy, setBusy] = useState(false);
    const a = state.activate;
    const onAction = async (b) => {
        setBusy(true);
        try {
            if (b.action === "disable_account") { await api.patch(`/accounts/${b.account_id}`, { trading_enabled: false }); toast.success(`${b.account_label || "Account"} disabled`); }
            else if (b.action === "lower_risk") await api.put("/bot/config", { risk_level: "low" }, { params: { account_id: account.id } });
            await refresh();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };
    return (
        <div className="space-y-4" data-testid="wizard-screen-activate">
            <ul className="space-y-1.5" data-testid="wizard-activate-ticks">{a.ticks.map((t) => <Tick key={t.id} done={t.done} status={t.done ? "done" : "waiting"} label={t.label} testid={`wizard-tick-${t.id}`} />)}</ul>
            {a.waiting_operator && <div className="border border-[#FFB000]/40 bg-[#FFB000]/10 p-3 text-sm text-white flex items-center gap-2" data-testid="wizard-waiting-operator"><Loader2 className="w-4 h-4 animate-spin text-[#FFB000]" /> Waiting for operator approval — the operator has been alerted; this screen continues by itself.</div>}
            <ErrorLine error={a.error} onAction={onAction} busy={busy} />
            {!a.error && !a.waiting_operator && !a.done && <div className="text-[11px] text-[#52525B]">usually under 10 seconds…</div>}
        </div>
    );
}

// ── Screen 4 ──────────────────────────────────────────────────────────────────────────────────────────
function StartScreen({ account, state, refresh, onDone }) {
    const s = state.start;
    const [risk, setRisk] = useState(s.risk_level);
    const [adv, setAdv] = useState(false);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const start = async () => {
        setBusy(true); setErr(null);
        try {
            const card = s.risk_cards.find((c) => c.id === risk);
            await api.put("/bot/config", { risk_level: card?.apply_level || risk }, { params: { account_id: account.id } });
            await api.patch(`/accounts/${account.id}`, { trading_enabled: true }).catch(() => {});
            await api.post("/bot/start", null, { params: { account_id: account.id } });
            await refresh(); onDone();
        } catch (e) {
            const d = e?.response?.data?.detail;
            const blockers = d?.blockers || d?.failed || [];
            setErr({ code: "not_ready", text: (Array.isArray(blockers) && blockers[0] && (blockers[0].message || blockers[0].label || blockers[0])) || d?.message || formatApiError(e), buttons: [{ action: "wait", label: "Check again" }] });
        } finally { setBusy(false); }
    };
    return (
        <div className="space-y-4" data-testid="wizard-screen-start">
            <div className="grid sm:grid-cols-3 gap-2" data-testid="wizard-risk-cards">
                {s.risk_cards.map((c) => (
                    <button key={c.id} onClick={() => setRisk(c.id)} data-testid={`wizard-risk-${c.id}`}
                        className={`border p-3 text-left ${risk === c.id ? "border-[#FFD700] bg-[#FFD700]/10" : "border-[#1F1F1F] hover:border-[#3F3F46]"}`}>
                        <div className="text-sm text-white font-bold">{c.title}</div>
                        <div className="text-[11px] text-[#A1A1AA] mt-1">{c.meaning}</div>
                    </button>
                ))}
            </div>
            <div className="text-xs text-[#A1A1AA]">Symbols: <span className="text-white font-mono" data-testid="wizard-symbols">{s.symbols.join(", ")}</span>{s.suffix && <span className="text-[#52525B]"> · suffix {s.suffix} detected</span>}
                <button onClick={() => setAdv(!adv)} className="ml-2 underline text-[#71717A]" data-testid="wizard-advanced-toggle">Advanced <ChevronDown className={`inline w-3 h-3 ${adv ? "rotate-180" : ""}`} /></button></div>
            {adv && <div className="text-[11px] text-[#71717A] border border-[#1F1F1F] p-2" data-testid="wizard-advanced">Symbols, breakers and filters are on the Bot page after you start. The defaults are tuned for XAUUSD and BTCUSD.</div>}
            <ErrorLine error={err} onAction={() => { setErr(null); refresh(); }} busy={busy} />
            <button onClick={start} disabled={busy} data-testid="wizard-start-btn"
                className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-bold py-3 text-sm font-mono tracking-widest flex items-center justify-center gap-2">
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : null} START BOT</button>
        </div>
    );
}

function DoneScreen({ account, state, onClose }) {
    return (
        <div className="space-y-4 text-center py-4" data-testid="wizard-screen-done">
            <CheckCircle2 className="w-10 h-10 mx-auto text-[#00FF41]" />
            <div className="text-lg text-white font-bold">Trading — the bot will open trades when its signals fire</div>
            <div className="text-xs text-[#A1A1AA]">{account.label} · {state.environment}</div>
            <div className="flex flex-wrap justify-center gap-2 text-xs font-mono tracking-widest">
                <a href="/dashboard" data-testid="wizard-done-dashboard" className="px-3 py-2 bg-[#FFD700] text-black">DASHBOARD</a>
                {state.environment === "DEMO" && <a href="/bot" data-testid="wizard-done-test-trade" className="px-3 py-2 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white">TEST TRADE (DEMO)</a>}
                <a href="/settings" data-testid="wizard-done-telegram" className="px-3 py-2 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white">TELEGRAM ALERTS</a>
                <button onClick={onClose} data-testid="wizard-done-close" className="px-3 py-2 text-[#71717A] hover:text-white">CLOSE</button>
            </div>
        </div>
    );
}

/** Easy Start Wizard — Broker → Connect → Activate → Start. `account` = resume on an existing account (Continue setup). */
export function EasyStartWizard({ open, onClose, account: initial = null, onChanged }) {
    const [account, setAccount] = useState(initial);
    const [state, setState] = useState(null);
    const [screen, setScreen] = useState(initial ? null : 1);
    useEffect(() => { setAccount(initial); setState(null); setScreen(initial ? null : 1); }, [initial, open]);
    const refresh = async () => {
        if (!account) return;
        try {
            const { data } = await api.get(`/setup/wizard/${account.id}`);
            setState(data);
            setScreen((cur) => (cur === 5 ? 5 : data.screen));   // auto-advance (and resume) from the server-derived screen
        } catch (e) { if (e?.response?.status === 404) onClose(); }
    };
    useEffect(() => {
        if (!open || !account) return undefined;
        refresh();
        const id = setInterval(refresh, 2000);
        return () => clearInterval(id);
    }, [open, account?.id]); // eslint-disable-line react-hooks/exhaustive-deps
    const title = screen === 5 ? "Done" : screen ? `${screen}. ${STEPS[screen - 1]}` : "Continue setup";
    return (
        <Dialog open={open} onOpenChange={(o) => { if (!o) { onClose(); onChanged && onChanged(); } }}>
            <DialogContent className="bg-[#0A0A0A] border-[#1F1F1F] text-[#E4E4E7] max-w-2xl" data-testid="easy-start-wizard" data-screen={screen || ""}>
                <TopBar screen={screen || 1} />
                <DialogTitle className="font-display text-base mt-1" data-testid="wizard-title">{title}</DialogTitle>
                <DialogDescription className="sr-only">Easy Start Wizard</DialogDescription>
                {screen === 1 && <BrokerScreen onCreated={(a) => { setAccount(a); setScreen(2); onChanged && onChanged(); }} />}
                {screen === 2 && state && <ConnectScreen account={account} state={state} refresh={refresh} />}
                {screen === 3 && state && <ActivateScreen account={account} state={state} refresh={refresh} />}
                {screen === 4 && state && <StartScreen account={account} state={state} refresh={refresh} onDone={() => { setScreen(5); onChanged && onChanged(); }} />}
                {screen === 5 && state && <DoneScreen account={account} state={state} onClose={onClose} />}
                {screen !== 1 && !state && <div className="text-xs text-[#71717A] font-mono flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> loading…</div>}
            </DialogContent>
        </Dialog>
    );
}
