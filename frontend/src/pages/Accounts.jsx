import { useEffect, useState, useCallback } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Plus, Trash2 as Trash, Copy, Download, RefreshCw as ArrowsClockwise, Plug, PlugZap as PlugsConnected, Info } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";

const empty = { label: "", broker: "", server: "", account_number: "", account_type: "microcent", base_currency: "USD" };

export default function Accounts() {
    const [accounts, setAccounts] = useState([]);
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState(empty);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/accounts");
            setAccounts(data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

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

                {/* Create form */}
                {showForm && (
                    <form onSubmit={create} className="border border-[#00FF41]/40 bg-[#0A0A0A] p-5 grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="account-form">
                        {[
                            ["label", "Label", "My Microcent #1"],
                            ["broker", "Broker", "RoboForex"],
                            ["server", "Server", "RoboForex-ECN"],
                            ["account_number", "Account #", "12345678"],
                        ].map(([k, l, ph]) => (
                            <div key={k}>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">{l.toUpperCase()}</label>
                                <input value={form[k]} onChange={e => setForm({ ...form, [k]: e.target.value })}
                                    required data-testid={`account-${k}-input`}
                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm outline-none transition-colors" placeholder={ph} />
                            </div>
                        ))}
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
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">BASE CURRENCY</label>
                            <input value={form.base_currency} onChange={e => setForm({ ...form, base_currency: e.target.value.toUpperCase() })}
                                data-testid="account-currency-input"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                        </div>
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
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
