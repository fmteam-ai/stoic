import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Bitcoin, Plus, Trash2, RefreshCw, ShieldCheck, ExternalLink, AlertTriangle, X, Zap } from "lucide-react";
import { toast } from "sonner";

const emptyForm = { label: "", api_key: "", api_secret: "", testnet: true, initial_balance: 10000 };

export default function Crypto() {
    const [accounts, setAccounts] = useState([]);
    const [status, setStatus] = useState({ live_enabled: false, default_testnet: true });
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState(emptyForm);
    const [submitting, setSubmitting] = useState(false);
    const [err, setErr] = useState("");
    const [loading, setLoading] = useState(true);
    const [activeAccount, setActiveAccount] = useState(null);

    const load = useCallback(async () => {
        try {
            const [a, s] = await Promise.all([
                api.get("/crypto/accounts"),
                api.get("/crypto/status"),
            ]);
            setAccounts(a.data || []);
            setStatus(s.data || status);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    const submit = async (e) => {
        e.preventDefault();
        setErr(""); setSubmitting(true);
        try {
            await api.post("/crypto/accounts", form);
            setShowForm(false); setForm(emptyForm);
            toast.success("Binance account verified & linked.");
            await load();
        } catch (e2) { setErr(formatApiError(e2)); }
        finally { setSubmitting(false); }
    };

    const remove = async (id) => {
        if (!confirm("Detach this Binance account? Stored API keys will be permanently erased.")) return;
        try {
            await api.delete(`/crypto/accounts/${id}`);
            toast.success("Account detached.");
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const verify = async (id) => {
        try {
            const { data } = await api.post(`/crypto/accounts/${id}/verify`);
            if (data.ok) toast.success(`Keys OK · USDT total ${data.usdt_total.toFixed(2)} · ${data.testnet ? "TESTNET" : "LIVE"}`);
            else toast.error(`Verify failed: ${data.message}`);
        } catch (e) { toast.error(formatApiError(e)); }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Crypto · Binance Spot"
                subtitle="Live BTC/ETH execution via CCXT. Testnet by default — flip to live only after explicit verification."
                action={
                    <button onClick={() => setShowForm(true)} data-testid="add-crypto-account-btn"
                        className="bg-[#FFD700] text-black px-4 py-2 font-mono text-xs tracking-widest hover:bg-[#FFB000] flex items-center gap-2">
                        <Plus className="w-4 h-4" /> ADD BINANCE ACCOUNT
                    </button>
                }
            />

            {/* Master live-toggle banner */}
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 mb-4 flex items-start gap-3" data-testid="crypto-master-status">
                <ShieldCheck className={`w-5 h-5 mt-0.5 shrink-0 ${status.live_enabled ? "text-[#FF3B30]" : "text-[#00FF41]"}`} />
                <div className="flex-1">
                    <div className="font-mono text-[10px] tracking-widest text-[#52525B]">SERVER-SIDE LIVE EXECUTION</div>
                    <div className="font-display font-bold text-sm">
                        {status.live_enabled ? "LIVE ENABLED · real-money trades allowed" : "TESTNET ONLY · live trades disabled by env"}
                    </div>
                    <div className="text-xs text-[#A1A1AA] mt-1">
                        Set <code className="font-mono text-[#FFD700]">BINANCE_LIVE_ENABLED=true</code> in backend/.env to unlock live. Defaults to false.
                    </div>
                </div>
            </div>

            {err && (
                <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 p-3 mb-4 text-xs text-[#FF3B30] font-mono" data-testid="crypto-error">{err}</div>
            )}

            {/* Accounts list */}
            {loading ? (
                <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="crypto-loading">LOADING…</div>
            ) : accounts.length === 0 ? (
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-10 text-center" data-testid="crypto-empty">
                    <Bitcoin className="w-12 h-12 text-[#FFD700] mx-auto mb-3 opacity-50" />
                    <div className="font-display font-bold text-lg mb-1">No Binance accounts yet</div>
                    <div className="text-sm text-[#A1A1AA] max-w-md mx-auto">
                        Add your testnet API key+secret from <a href="https://testnet.binance.vision/" target="_blank" rel="noopener noreferrer"
                            className="text-[#FFD700] underline">testnet.binance.vision</a> to enable real-time BTC/ETH execution alongside MT5.
                    </div>
                </div>
            ) : (
                <div className="space-y-3" data-testid="crypto-accounts-list">
                    {accounts.map(a => (
                        <div key={a.id} className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid={`crypto-account-${a.id}`}>
                            <div className="flex items-start gap-4 flex-wrap">
                                <Bitcoin className="w-8 h-8 text-[#FFD700] mt-1 shrink-0" />
                                <div className="flex-1 min-w-0">
                                    <div className="font-display font-bold text-base truncate">{a.label}</div>
                                    <div className="flex items-center gap-2 flex-wrap mt-1">
                                        <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${a.testnet ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}>
                                            {a.testnet ? "TESTNET" : "LIVE"}
                                        </span>
                                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{a.broker || "BINANCE_SPOT"}</span>
                                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">KEY: {a.api_key_masked || "•••• ????"}</span>
                                    </div>
                                </div>
                                <div className="flex items-center gap-2 flex-wrap">
                                    <button onClick={() => verify(a.id)} data-testid={`crypto-verify-${a.id}`}
                                        className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white hover:border-[#FFD700]/40 font-mono text-[10px] tracking-widest flex items-center gap-1">
                                        <ShieldCheck className="w-3 h-3" /> VERIFY
                                    </button>
                                    <button onClick={() => setActiveAccount(a)} data-testid={`crypto-inspect-${a.id}`}
                                        className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white hover:border-[#0099FF]/40 font-mono text-[10px] tracking-widest flex items-center gap-1">
                                        <Zap className="w-3 h-3" /> INSPECT
                                    </button>
                                    <button onClick={() => remove(a.id)} data-testid={`crypto-remove-${a.id}`}
                                        className="px-3 py-1.5 border border-[#1F1F1F] text-[#FF3B30] hover:bg-[#FF3B30]/10 font-mono text-[10px] tracking-widest flex items-center gap-1">
                                        <Trash2 className="w-3 h-3" /> DETACH
                                    </button>
                                </div>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {showForm && (
                <AddCryptoModal form={form} setForm={setForm} onSubmit={submit}
                    onClose={() => setShowForm(false)} submitting={submitting} />
            )}

            {activeAccount && (
                <InspectAccountModal account={activeAccount} onClose={() => setActiveAccount(null)} />
            )}
        </AppLayout>
    );
}


function AddCryptoModal({ form, setForm, onSubmit, onClose, submitting }) {
    const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="add-crypto-modal" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-lg w-full max-h-[90vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                    <Bitcoin className="w-5 h-5 text-[#FFD700]" />
                    <div className="flex-1">
                        <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">ADD BINANCE ACCOUNT</div>
                        <div className="font-display font-bold text-lg">Connect via CCXT</div>
                    </div>
                    <button onClick={onClose} data-testid="add-crypto-close"
                        className="p-1.5 text-[#52525B] hover:text-white"><X className="w-4 h-4" /></button>
                </div>

                <div className="p-5 border-b border-[#1F1F1F] bg-[#FFD700]/5 text-xs text-[#A1A1AA] space-y-1">
                    <div className="flex items-start gap-2">
                        <AlertTriangle className="w-4 h-4 text-[#FFD700] shrink-0 mt-0.5" />
                        <div>
                            <strong className="text-[#FFD700]">Testnet first.</strong> Generate keys at{" "}
                            <a href="https://testnet.binance.vision/" target="_blank" rel="noopener noreferrer"
                                className="text-[#FFD700] underline inline-flex items-center gap-1">
                                testnet.binance.vision <ExternalLink className="w-3 h-3" />
                            </a>. Required permissions: <code className="text-white">Enable Spot Trading</code>.
                            <span className="text-[#FF3B30]"> Never grant Withdrawals.</span> Keys are AES-256-GCM encrypted before storage.
                        </div>
                    </div>
                </div>

                <form onSubmit={onSubmit} className="p-5 space-y-4">
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">LABEL</label>
                        <input type="text" required value={form.label}
                            onChange={(e) => set("label", e.target.value)} data-testid="form-label"
                            placeholder="My Testnet Account"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">API KEY</label>
                        <input type="text" required value={form.api_key}
                            onChange={(e) => set("api_key", e.target.value)} data-testid="form-api-key"
                            placeholder="paste 64-char API key"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">API SECRET</label>
                        <input type="password" required value={form.api_secret}
                            onChange={(e) => set("api_secret", e.target.value)} data-testid="form-api-secret"
                            placeholder="paste 64-char secret"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <label className="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" checked={form.testnet}
                            onChange={(e) => set("testnet", e.target.checked)} data-testid="form-testnet"
                            className="accent-[#FFD700]" />
                        <span className="font-mono text-xs">Testnet (recommended)</span>
                    </label>

                    <div className="pt-2 flex items-center justify-end gap-2">
                        <button type="button" onClick={onClose} data-testid="form-cancel"
                            className="px-4 py-2 font-mono text-xs tracking-widest text-[#A1A1AA] hover:text-white">CANCEL</button>
                        <button type="submit" disabled={submitting} data-testid="form-submit"
                            className="bg-[#FFD700] text-black px-5 py-2 font-mono text-xs tracking-widest hover:bg-[#FFB000] disabled:opacity-50 flex items-center gap-2">
                            {submitting ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Plus className="w-3 h-3" />}
                            {submitting ? "VERIFYING…" : "LINK ACCOUNT"}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
}


function InspectAccountModal({ account, onClose }) {
    const [balance, setBalance] = useState(null);
    const [ticker, setTicker] = useState(null);
    const [err, setErr] = useState("");
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancelled = false;
        const run = async () => {
            try {
                const [b, t] = await Promise.all([
                    api.get(`/crypto/accounts/${account.id}/balance`),
                    api.get(`/crypto/accounts/${account.id}/ticker?symbol=BTC/USDT`),
                ]);
                if (!cancelled) { setBalance(b.data); setTicker(t.data); }
            } catch (e) { if (!cancelled) setErr(formatApiError(e)); }
            finally { if (!cancelled) setLoading(false); }
        };
        run();
        return () => { cancelled = true; };
    }, [account.id]);

    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="inspect-modal" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#0099FF]/40 max-w-xl w-full max-h-[85vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                    <Zap className="w-5 h-5 text-[#0099FF]" />
                    <div className="flex-1 min-w-0">
                        <div className="font-mono text-[10px] text-[#0099FF] tracking-widest">INSPECT · LIVE BINANCE STATE</div>
                        <div className="font-display font-bold text-lg truncate">{account.label}</div>
                    </div>
                    <button onClick={onClose} data-testid="inspect-close"
                        className="p-1.5 text-[#52525B] hover:text-white"><X className="w-4 h-4" /></button>
                </div>

                {err && (
                    <div className="border-b border-[#FF3B30]/30 bg-[#FF3B30]/10 px-5 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>
                )}

                {loading ? (
                    <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                ) : (
                    <div className="p-5 space-y-4">
                        {ticker && (
                            <div data-testid="inspect-ticker">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">BTC/USDT · LIVE TICKER</div>
                                <div className="grid grid-cols-3 gap-2 font-mono text-xs">
                                    <Stat label="BID" v={ticker.bid?.toFixed?.(2)} />
                                    <Stat label="ASK" v={ticker.ask?.toFixed?.(2)} />
                                    <Stat label="LAST" v={ticker.last?.toFixed?.(2)} hi />
                                </div>
                            </div>
                        )}
                        {balance && (
                            <div data-testid="inspect-balance">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">
                                    BALANCE · {balance.testnet ? "TESTNET" : "LIVE"}
                                </div>
                                <div className="border border-[#1F1F1F] bg-[#050505]">
                                    {(balance.assets || []).length === 0 ? (
                                        <div className="px-3 py-2 text-xs text-[#52525B] font-mono">No assets</div>
                                    ) : (
                                        balance.assets.map(a => (
                                            <div key={a.asset} className="flex items-center justify-between px-3 py-2 border-b border-[#1F1F1F] last:border-b-0">
                                                <span className="font-mono text-sm text-white">{a.asset}</span>
                                                <div className="flex gap-3 font-mono text-xs">
                                                    <span className="text-[#52525B]">free <span className="text-white">{a.free.toFixed(6)}</span></span>
                                                    <span className="text-[#52525B]">used <span className="text-white">{a.used.toFixed(6)}</span></span>
                                                    <span className="text-[#52525B]">total <span className="text-[#FFD700]">{a.total.toFixed(6)}</span></span>
                                                </div>
                                            </div>
                                        ))
                                    )}
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}

function Stat({ label, v, hi }) {
    return (
        <div className={`border border-[#1F1F1F] bg-[#050505] p-3 ${hi ? "border-[#FFD700]/30" : ""}`}>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{label}</div>
            <div className={`font-mono text-base ${hi ? "text-[#FFD700]" : "text-white"}`}>{v ?? "—"}</div>
        </div>
    );
}
