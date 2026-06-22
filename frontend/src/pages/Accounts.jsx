import { useEffect, useState, useCallback } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Plus, Trash2 as Trash, Copy, Download, RefreshCw as ArrowsClockwise, Plug, PlugZap as PlugsConnected, Info, Lock, Eye, EyeOff, KeyRound, Layers, ChevronDown } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";

const empty = { label: "", broker: "", server: "", account_number: "", account_type: "microcent", base_currency: "USD", mode: "paper", initial_balance: 10000, investor_password: "", master_password: "" };

export default function Accounts() {
    const [accounts, setAccounts] = useState([]);
    const [limits, setLimits] = useState(null);
    const [presets, setPresets] = useState([]);
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState(empty);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        try {
            const [a, l] = await Promise.all([api.get("/accounts"), api.get("/accounts/limits")]);
            setAccounts(a.data);
            setLimits(l.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    // Lazy-load broker presets when the form first opens
    useEffect(() => {
        if (showForm && presets.length === 0) {
            api.get("/accounts/broker-presets").then(r => setPresets(r.data.presets || [])).catch(() => {});
        }
    }, [showForm, presets.length]);

    // Live heartbeat updates from the EA bridge
    const { lastEvent } = useLiveStream();
    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "account_heartbeat") {
            const p = lastEvent.payload;
            setAccounts(prev => prev.map(a => a.id === p.account_id
                ? { ...a, balance: p.balance, equity: p.equity, last_heartbeat: p.last_heartbeat, status: "connected" }
                : a));
        }
    }, [lastEvent]);

    const create = async (e) => {
        e.preventDefault(); setErr(""); setMsg("");
        try {
            await api.post("/accounts", form);
            setShowForm(false); setForm(empty); setMsg("Account added.");
            await load();
        } catch (e2) { setErr(formatApiError(e2)); }
    };

    const remove = async (id) => {
        if (!window.confirm("Delete this account? Trades remain in history.")) return;
        try { await api.delete(`/accounts/${id}`); await load(); } catch (e) { setErr(formatApiError(e)); }
    };

    const rotate = async (id) => {
        try {
            const { data } = await api.post(`/accounts/${id}/rotate-token`);
            setMsg(`New bridge token: ${data.bridge_token.slice(0, 12)}…`);
            await load();
        } catch (e) { setErr(formatApiError(e)); }
    };

    const copyToken = (t) => { navigator.clipboard.writeText(t); setMsg("Bridge token copied."); };

    const isFresh = (iso) => {
        if (!iso) return false;
        return (Date.now() - new Date(iso).getTime()) < 60_000;
    };

    return (
        <AppLayout>
            <PageHeader
                title="MT5 Accounts"
                subtitle="Connect MetaTrader 5 microcent accounts to execute live trades."
                testid="accounts-header"
                action={
                    <div className="flex gap-2">
                        <a href={`${API}/ea-script`} target="_blank" rel="noopener noreferrer" download
                            data-testid="download-ea-button"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                            <Download className="w-3.5 h-3.5" /> DOWNLOAD EA
                        </a>
                        <button onClick={() => setShowForm(!showForm)} data-testid="add-account-button"
                            className="flex items-center gap-2 px-3 py-2 bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium text-xs tracking-widest transition-colors">
                            <Plus className="w-3.5 h-3.5" /> ADD ACCOUNT
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-4" data-testid="accounts-page">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}
                {msg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-4 py-2 text-xs text-[#00FF41] font-mono">{msg}</div>}

                {/* Bridge instructions */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 flex gap-4">
                    <Info className="w-5 h-5 text-[#FFB000] shrink-0 mt-0.5" />
                    <div className="text-sm text-[#A1A1AA] space-y-2">
                        <div className="font-display font-bold text-white">How the MT5 Bridge works</div>
                        <ol className="list-decimal list-inside space-y-1 text-xs leading-relaxed">
                            <li>Add your MT5 account here — you&apos;ll receive a unique <em>bridge token</em>.</li>
                            <li>Download <code className="font-mono text-[#00FF41]">EmergentTradingBridge.mq5</code> and copy it to your MT5 <em>MQL5/Experts</em> folder.</li>
                            <li>In MT5: <em>Tools → Options → Expert Advisors</em> — enable WebRequest and add this server URL.</li>
                            <li>Attach the EA to any chart and paste your bridge token in the EA inputs.</li>
                            <li>The EA polls every 5s for trades and reports execution back here.</li>
                        </ol>
                    </div>
                </div>

                {/* Broker / account-slot usage */}
                <BrokerUsageCard limits={limits} />

                {/* Create form */}
                {showForm && (
                    <form onSubmit={create} className="border border-[#00FF41]/40 bg-[#0A0A0A] p-5 grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="account-form">
                        <div className="md:col-span-2 flex gap-2 mb-2">
                            {["paper", "live"].map(m => (
                                <button type="button" key={m} onClick={() => setForm({ ...form, mode: m })}
                                    data-testid={`account-mode-${m}`}
                                    className={`flex-1 px-3 py-2 text-xs font-mono tracking-widest border transition-colors ${
                                        form.mode === m ? "border-[#00FF41] bg-[#00FF41]/10 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"
                                    }`}>
                                    {m === "paper" ? "● PAPER · zero-risk simulation" : "● LIVE · real MT5 broker"}
                                </button>
                            ))}
                        </div>
                        {form.mode === "live" && (
                            <div className="md:col-span-2">
                                <BrokerPresetPicker presets={presets} form={form} setForm={setForm} />
                            </div>
                        )}
                        {[
                            ["label", "Label", form.mode === "paper" ? "Paper Sandbox #1" : "My Microcent #1"],
                            ...(form.mode === "live" ? [
                                ["broker", "Broker", "RoboForex"],
                                ["server", "Server", "RoboForex-ECN"],
                            ] : []),
                            ["account_number", form.mode === "paper" ? "Sandbox ID" : "Account #", "12345678"],
                        ].map(([k, l, ph]) => (
                            <div key={k}>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">{l.toUpperCase()}</label>
                                <input value={form[k]} onChange={e => setForm({ ...form, [k]: e.target.value })}
                                    required data-testid={`account-${k}-input`}
                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm outline-none transition-colors" placeholder={ph} />
                            </div>
                        ))}
                        {form.mode === "paper" ? (
                            <div>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">STARTING VIRTUAL BALANCE</label>
                                <input type="number" min="100" step="100" value={form.initial_balance}
                                    onChange={e => setForm({ ...form, initial_balance: parseFloat(e.target.value) || 10000 })}
                                    data-testid="account-balance-input"
                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                            </div>
                        ) : (
                            <div>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">ACCOUNT TYPE</label>
                                <select value={form.account_type} onChange={e => setForm({ ...form, account_type: e.target.value })}
                                    data-testid="account-type-select"
                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm outline-none">
                                    <option value="microcent">Microcent</option>
                                    <option value="cent">Cent</option>
                                    <option value="standard">Standard</option>
                                    <option value="demo">Demo</option>
                                </select>
                            </div>
                        )}
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">BASE CURRENCY</label>
                            <input value={form.base_currency} onChange={e => setForm({ ...form, base_currency: e.target.value.toUpperCase() })}
                                data-testid="account-currency-input"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                        </div>

                        {form.mode === "live" && (
                            <div className="md:col-span-2 border-t border-[#1F1F1F] pt-3 mt-1 space-y-3">
                                <div className="flex items-start gap-2">
                                    <Lock className="w-3.5 h-3.5 text-[#FFD700] shrink-0 mt-0.5" />
                                    <div className="text-[10px] font-mono text-[#A1A1AA] tracking-wide leading-relaxed">
                                        <span className="text-[#FFD700]">OPTIONAL</span> · Stored encrypted (AES-256-GCM) for your reference only.
                                        The EA <em>does not need them</em> — it uses your logged-in MT5 session.
                                    </div>
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    <div>
                                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">INVESTOR PASSWORD (read-only)</label>
                                        <input type="password" autoComplete="new-password" value={form.investor_password}
                                            onChange={e => setForm({ ...form, investor_password: e.target.value })}
                                            data-testid="account-investor-pw-input"
                                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" placeholder="(optional)" />
                                    </div>
                                    <div>
                                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">MASTER PASSWORD (trading)</label>
                                        <input type="password" autoComplete="new-password" value={form.master_password}
                                            onChange={e => setForm({ ...form, master_password: e.target.value })}
                                            data-testid="account-master-pw-input"
                                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" placeholder="(optional)" />
                                    </div>
                                </div>
                            </div>
                        )}

                        <div className="md:col-span-2 flex justify-end gap-2">
                            <button type="button" onClick={() => { setShowForm(false); setForm(empty); }}
                                className="px-4 py-2 text-xs tracking-widest border border-[#1F1F1F] hover:border-[#333333] transition-colors">
                                CANCEL
                            </button>
                            <button type="submit" data-testid="account-save-button"
                                className="px-4 py-2 bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium text-xs tracking-widest transition-colors">
                                CREATE ACCOUNT
                            </button>
                        </div>
                    </form>
                )}

                {/* Accounts list */}
                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING ACCOUNTS…</div>
                ) : accounts.length === 0 ? (
                    <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="accounts-empty">
                        <Plug className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                        <div className="font-display font-bold text-lg mb-1">No accounts connected</div>
                        <div className="text-sm text-[#A1A1AA]">Add an MT5 microcent account to start live trading.</div>
                    </div>
                ) : (
                    <div className="space-y-2">
                        {accounts.map(a => {
                            const live = isFresh(a.last_heartbeat);
                            return (
                                <div key={a.id} className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid={`account-row-${a.account_number}`}>
                                    <div className="flex items-start justify-between gap-3 flex-wrap">
                                        <div className="space-y-1">
                                            <div className="flex items-center gap-3">
                                                <div className="font-display font-bold text-lg">{a.label}</div>
                                                <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${
                                                    live ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"
                                                }`}>
                                                    {live ? <><PlugsConnected className="w-3 h-3 inline mr-1" /> CONNECTED</> : "● DISCONNECTED"}
                                                </span>
                                            </div>
                                            <div className="font-mono text-xs text-[#A1A1AA]">{a.broker} · {a.server} · #{a.account_number}</div>
                                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">TYPE · {a.account_type?.toUpperCase()} · {a.base_currency}</div>
                                        </div>
                                        <div className="grid grid-cols-2 gap-3">
                                            <div className="p-3 border border-[#1F1F1F]">
                                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">BALANCE</div>
                                                <div className="font-mono text-sm">{(a.balance ?? 0).toFixed(2)}</div>
                                            </div>
                                            <div className="p-3 border border-[#1F1F1F]">
                                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">EQUITY</div>
                                                <div className="font-mono text-sm">{(a.equity ?? 0).toFixed(2)}</div>
                                            </div>
                                        </div>
                                    </div>

                                    <div className="mt-4 pt-4 border-t border-[#1F1F1F]">
                                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">BRIDGE TOKEN · paste into MT5 EA inputs</div>
                                        <div className="flex items-center gap-2 flex-wrap">
                                            <code className="font-mono text-xs px-3 py-2 bg-[#050505] border border-[#1F1F1F] flex-1 break-all" data-testid={`bridge-token-${a.account_number}`}>{a.bridge_token}</code>
                                            <button onClick={() => copyToken(a.bridge_token)} data-testid={`copy-token-${a.account_number}`}
                                                className="px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest flex items-center gap-1 transition-colors">
                                                <Copy className="w-3.5 h-3.5" /> COPY
                                            </button>
                                            <button onClick={() => rotate(a.id)} data-testid={`rotate-token-${a.account_number}`}
                                                className="px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest flex items-center gap-1 transition-colors">
                                                <ArrowsClockwise className="w-3.5 h-3.5" /> ROTATE
                                            </button>
                                            <button onClick={() => remove(a.id)} data-testid={`delete-account-${a.account_number}`}
                                                className="px-3 py-2 border border-[#FF3B30]/30 text-[#FF3B30] hover:bg-[#FF3B30]/10 text-xs font-mono tracking-widest flex items-center gap-1 transition-colors">
                                                <Trash className="w-3.5 h-3.5" /> DELETE
                                            </button>
                                        </div>
                                    </div>

                                    {a.mode !== "paper" && (
                                        <CredentialsPanel account={a} onUpdate={load} onError={(e) => setErr(e)} onMessage={(m) => setMsg(m)} />
                                    )}
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>
        </AppLayout>
    );
}

function CredentialsPanel({ account, onUpdate, onError, onMessage }) {
    const [editing, setEditing] = useState(false);
    const [investor, setInvestor] = useState("");
    const [master, setMaster] = useState("");
    const [revealed, setRevealed] = useState(null);
    const [saving, setSaving] = useState(false);

    const hasAny = account.has_investor_password || account.has_master_password;

    const reveal = async () => {
        try {
            const { data } = await api.post(`/accounts/${account.id}/credentials/reveal`);
            setRevealed(data);
            setTimeout(() => setRevealed(null), 30_000); // auto-hide after 30s
        } catch (e) { onError(formatApiError(e)); }
    };

    const save = async () => {
        setSaving(true);
        try {
            const payload = {};
            if (investor !== "") payload.investor_password = investor;
            if (master !== "") payload.master_password = master;
            if (Object.keys(payload).length === 0) {
                setEditing(false); setSaving(false); return;
            }
            await api.patch(`/accounts/${account.id}/credentials`, payload);
            onMessage("Credentials saved (encrypted).");
            setInvestor(""); setMaster(""); setEditing(false);
            onUpdate();
        } catch (e) { onError(formatApiError(e)); }
        finally { setSaving(false); }
    };

    const clear = async (which) => {
        if (!window.confirm(`Remove stored ${which} password?`)) return;
        try {
            const payload = which === "investor" ? { investor_password: "" } : { master_password: "" };
            await api.patch(`/accounts/${account.id}/credentials`, payload);
            onMessage("Credential cleared.");
            setRevealed(null);
            onUpdate();
        } catch (e) { onError(formatApiError(e)); }
    };

    return (
        <div className="mt-4 pt-4 border-t border-[#1F1F1F]" data-testid={`credentials-panel-${account.account_number}`}>
            <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                    <Lock className="w-3.5 h-3.5 text-[#FFD700]" />
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">BROKER PASSWORDS · AES-256-GCM at rest</span>
                </div>
                {!editing && (
                    <button onClick={() => setEditing(true)} data-testid={`credentials-edit-${account.account_number}`}
                        className="px-3 py-1 border border-[#1F1F1F] hover:border-[#FFD700] text-[10px] font-mono tracking-widest flex items-center gap-1 transition-colors">
                        <KeyRound className="w-3 h-3" /> {hasAny ? "UPDATE" : "ADD"}
                    </button>
                )}
            </div>

            {!editing && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {["investor", "master"].map((kind) => {
                        const stored = kind === "investor" ? account.has_investor_password : account.has_master_password;
                        const value = revealed ? (kind === "investor" ? revealed.investor_password : revealed.master_password) : null;
                        return (
                            <div key={kind} className="p-2 bg-[#050505] border border-[#1F1F1F] flex items-center justify-between gap-2">
                                <div className="min-w-0 flex-1">
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">{kind.toUpperCase()} PASSWORD</div>
                                    <div className="font-mono text-xs truncate" data-testid={`credentials-${kind}-${account.account_number}`}>
                                        {stored ? (value ?? "••••••••") : <span className="text-[#52525B]">— not stored —</span>}
                                    </div>
                                </div>
                                {stored && (
                                    <div className="flex items-center gap-1">
                                        <button onClick={reveal} data-testid={`reveal-${kind}-${account.account_number}`}
                                            className="p-1.5 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]" title={value ? "Hide" : "Reveal (auto-hides in 30s)"}>
                                            {value ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                                        </button>
                                        <button onClick={() => clear(kind)} data-testid={`clear-${kind}-${account.account_number}`}
                                            className="p-1.5 border border-[#FF3B30]/30 text-[#FF3B30] hover:bg-[#FF3B30]/10" title="Remove stored password">
                                            <Trash className="w-3 h-3" />
                                        </button>
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            {editing && (
                <div className="space-y-2 bg-[#050505] border border-[#FFD700]/30 p-3">
                    <div className="text-[10px] text-[#A1A1AA] font-mono">
                        Leave a field blank to keep the existing value. These are never sent to MT5 — they&apos;re stored encrypted for your reference only.
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                        <input type="password" autoComplete="new-password" value={investor}
                            onChange={e => setInvestor(e.target.value)}
                            data-testid={`edit-investor-pw-${account.account_number}`}
                            placeholder={account.has_investor_password ? "Investor (already stored)" : "Investor password (read-only)"}
                            className="bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                        <input type="password" autoComplete="new-password" value={master}
                            onChange={e => setMaster(e.target.value)}
                            data-testid={`edit-master-pw-${account.account_number}`}
                            placeholder={account.has_master_password ? "Master (already stored)" : "Master password (trading)"}
                            className="bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div className="flex justify-end gap-2">
                        <button onClick={() => { setEditing(false); setInvestor(""); setMaster(""); }}
                            className="px-3 py-1.5 border border-[#1F1F1F] text-[10px] font-mono tracking-widest">CANCEL</button>
                        <button onClick={save} disabled={saving}
                            data-testid={`credentials-save-${account.account_number}`}
                            className="px-3 py-1.5 bg-[#FFD700] text-black font-bold text-[10px] tracking-widest disabled:opacity-50">
                            {saving ? "SAVING…" : "SAVE ENCRYPTED"}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

function BrokerUsageCard({ limits }) {
    if (!limits) return null;
    const { max_brokers, max_accounts_per_broker, brokers_used, breakdown, total_live_accounts } = limits;
    const brokerCapReached = brokers_used >= max_brokers;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="broker-usage-card">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <Layers className="w-4 h-4 text-[#00FF41]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">BROKER USAGE</div>
                        <div className="font-display font-bold text-base tracking-tight">
                            {brokers_used} / {max_brokers} brokers · {total_live_accounts} live account{total_live_accounts === 1 ? "" : "s"}
                        </div>
                    </div>
                </div>
                <div className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${
                    brokerCapReached ? "border-[#FFD700]/40 text-[#FFD700] bg-[#FFD700]/10" : "border-[#1F1F1F] text-[#A1A1AA]"
                }`} data-testid="broker-usage-status">
                    {brokerCapReached ? "● BROKER CAP REACHED" : `○ ${max_brokers - brokers_used} BROKER${(max_brokers - brokers_used) === 1 ? "" : "S"} FREE`}
                </div>
            </div>
            <div className="p-5">
                {breakdown.length === 0 ? (
                    <div className="text-xs text-[#52525B] font-mono tracking-widest">
                        NO LIVE BROKERS CONNECTED · ADD YOUR FIRST MT5 ACCOUNT TO BEGIN
                    </div>
                ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2" data-testid="broker-usage-breakdown">
                        {breakdown.map(b => {
                            const used = b.count;
                            const pct = Math.min(100, (used / max_accounts_per_broker) * 100);
                            const full = used >= max_accounts_per_broker;
                            return (
                                <div key={b.broker} className="p-3 border border-[#1F1F1F] bg-[#050505]"
                                    data-testid={`broker-slot-${b.broker.toLowerCase().replace(/\s+/g, "-")}`}>
                                    <div className="flex items-center justify-between mb-1.5">
                                        <div className="font-display font-bold text-sm truncate">{b.broker}</div>
                                        <div className={`font-mono text-[10px] tracking-widest ${full ? "text-[#FFD700]" : "text-[#A1A1AA]"}`}>
                                            {used}/{max_accounts_per_broker}
                                        </div>
                                    </div>
                                    <div className="h-1 bg-[#0A0A0A] border border-[#1F1F1F]">
                                        <div className={`h-full ${full ? "bg-[#FFD700]" : "bg-[#00FF41]"}`} style={{ width: `${pct}%` }} />
                                    </div>
                                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1.5">
                                        {full ? "AT CAP" : `${b.remaining_slots} SLOT${b.remaining_slots === 1 ? "" : "S"} FREE`}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}
                <div className="mt-3 pt-3 border-t border-[#1F1F1F] flex items-start gap-2">
                    <Info className="w-3.5 h-3.5 text-[#52525B] shrink-0 mt-0.5" />
                    <div className="font-mono text-[10px] text-[#52525B] tracking-wide leading-relaxed">
                        UP TO {max_brokers} DIFFERENT BROKERS · {max_accounts_per_broker} LIVE ACCOUNTS PER BROKER.
                        PAPER SANDBOX ACCOUNTS DO NOT COUNT TOWARDS THESE LIMITS.
                        NEED MORE? CONTACT SUPPORT TO EXTEND.
                    </div>
                </div>
            </div>
        </div>
    );
}

function BrokerPresetPicker({ presets, form, setForm }) {
    const [open, setOpen] = useState(false);
    if (!presets || presets.length === 0) return null;

    const pickBroker = (p) => {
        setForm({
            ...form,
            broker: p.broker,
            server: p.servers[0] || "",
            account_type: p.account_types?.[0] || form.account_type,
        });
        setOpen(false);
    };

    return (
        <div className="mb-2" data-testid="broker-preset-picker">
            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">QUICK PICK · COMMON BROKERS</label>
            <button type="button" onClick={() => setOpen(!open)}
                data-testid="broker-preset-toggle"
                className="w-full bg-[#050505] border border-[#1F1F1F] hover:border-[#00FF41]/40 px-3 py-2 text-sm font-mono flex items-center justify-between transition-colors">
                <span className={form.broker ? "text-white" : "text-[#52525B]"}>
                    {form.broker ? `▸ ${form.broker} · ${form.server || "(no server)"}` : "Pick a broker preset, or fill the fields below manually"}
                </span>
                <ChevronDown className={`w-3.5 h-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
            </button>
            {open && (
                <div className="mt-2 border border-[#1F1F1F] bg-[#050505] max-h-72 overflow-y-auto" data-testid="broker-preset-list">
                    {presets.map(p => (
                        <button type="button" key={p.broker} onClick={() => pickBroker(p)}
                            data-testid={`broker-preset-${p.broker.toLowerCase().replace(/\s+/g, "-")}`}
                            className="w-full text-left px-3 py-2 border-b border-[#1F1F1F] last:border-b-0 hover:bg-[#0A0A0A] transition-colors">
                            <div className="font-display font-bold text-sm">{p.broker}</div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-wide mt-0.5">
                                {p.servers.join(" · ")}
                            </div>
                        </button>
                    ))}
                    <div className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-wide bg-[#0A0A0A] border-t border-[#1F1F1F]">
                        BROKER NOT LISTED? CLOSE THIS MENU AND TYPE IT MANUALLY BELOW.
                    </div>
                </div>
            )}
        </div>
    );
}
