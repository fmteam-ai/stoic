import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { CryptoLiveBadge } from "@/components/CryptoLiveBadge";
import { Bitcoin, Plus, Trash2, RefreshCw, ShieldCheck, AlertTriangle, X, Zap } from "lucide-react";
import { toast } from "sonner";

const emptyForm = {
    label: "",
    api_key: "",
    api_secret: "",
    api_passphrase: "",
    testnet: true,
    initial_balance: 10000,
    exchange_id: "binance",
};

export default function Crypto() {
    const [accounts, setAccounts] = useState([]);
    const [status, setStatus] = useState({ live_enabled: false, default_testnet: true });
    const [exchanges, setExchanges] = useState([]);
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState(emptyForm);
    const [submitting, setSubmitting] = useState(false);
    const [err, setErr] = useState("");
    const [formErr, setFormErr] = useState("");
    const [loading, setLoading] = useState(true);
    const [activeAccount, setActiveAccount] = useState(null);

    const load = useCallback(async () => {
        try {
            const [a, s, x] = await Promise.all([
                api.get("/crypto/accounts"),
                api.get("/crypto/status"),
                api.get("/crypto/exchanges"),
            ]);
            setAccounts(a.data || []);
            setStatus(s.data || status);
            setExchanges(x.data?.exchanges || []);
            // Adopt the backend's smart default (first reachable exchange)
            // so the modal doesn't preselect a known-blocked option.
            const smartDefault = x.data?.default;
            if (smartDefault) {
                setForm(f => f.exchange_id === "binance" ? { ...f, exchange_id: smartDefault } : f);
            }
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => { load(); }, [load]);

    const submit = async (e) => {
        e.preventDefault();
        setErr(""); setFormErr(""); setSubmitting(true);
        try {
            // Drop empty passphrase so backend default-None applies.
            const payload = { ...form };
            if (!payload.api_passphrase) delete payload.api_passphrase;
            await api.post("/crypto/accounts", payload);
            setShowForm(false); setForm(emptyForm);
            toast.success("Account verified & linked.");
            await load();
        } catch (e2) {
            const msg = formatApiError(e2);
            setFormErr(msg);          // inline error inside the open modal
            toast.error(msg);         // and a visible toast
        }
        finally { setSubmitting(false); }
    };

    const remove = async (id) => {
        if (!confirm("Detach this crypto account? Stored API keys will be permanently erased.")) return;
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
                title="Crypto · Spot Trading"
                subtitle="Live BTC/ETH execution via CCXT. Binance · Binance.US · Kraken · OKX · KuCoin. Testnet first — flip to live only after verification."
                action={
                    <div className="flex items-center gap-3">
                        <CryptoLiveBadge />
                        <button onClick={() => setShowForm(true)} data-testid="add-crypto-account-btn"
                            className="bg-[#FFD700] text-black px-4 py-2 font-mono text-xs tracking-widest hover:bg-[#FFB000] flex items-center gap-2">
                            <Plus className="w-4 h-4" /> ADD CRYPTO ACCOUNT
                        </button>
                    </div>
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
                        Live needs BOTH <code className="font-mono text-[#FFD700]">CRYPTO_LIVE_TRADING_ENABLED=true</code> and <code className="font-mono text-[#FFD700]">BINANCE_LIVE_ENABLED=true</code> in backend/.env. Both default to off. Testnet accounts are refused on exchanges without a sandbox (Kraken, Binance.US).
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
                    <div className="font-display font-bold text-lg mb-1">No crypto accounts yet</div>
                    <div className="text-sm text-[#A1A1AA] max-w-md mx-auto">
                        Connect any supported exchange (Binance · Binance.US · Kraken · OKX · KuCoin)
                        to enable real-time BTC/ETH execution alongside MT5.
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
                                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{a.exchange_label || a.broker || "BINANCE_SPOT"}</span>
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
                    exchanges={exchanges}
                    onClose={() => { setShowForm(false); setFormErr(""); }}
                    submitting={submitting} formErr={formErr} />
            )}

            {activeAccount && (
                <InspectAccountModal account={activeAccount} onClose={() => setActiveAccount(null)} />
            )}
        </AppLayout>
    );
}


function AddCryptoModal({ form, setForm, onSubmit, onClose, submitting, formErr, exchanges }) {
    const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
    const selected = exchanges.find(e => e.id === form.exchange_id) || {};
    const needsPassphrase = !!selected.requires_passphrase;
    const supportsSandbox = selected.supports_sandbox !== false;
    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="add-crypto-modal" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-lg w-full max-h-[90vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                    <Bitcoin className="w-5 h-5 text-[#FFD700]" />
                    <div className="flex-1">
                        <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">ADD CRYPTO ACCOUNT</div>
                        <div className="font-display font-bold text-lg">Connect via CCXT</div>
                    </div>
                    <button onClick={onClose} data-testid="add-crypto-close"
                        className="p-1.5 text-[#52525B] hover:text-white"><X className="w-4 h-4" /></button>
                </div>

                <div className="p-5 border-b border-[#1F1F1F] bg-[#FFD700]/5 text-xs text-[#A1A1AA] space-y-1">
                    <div className="flex items-start gap-2">
                        <AlertTriangle className="w-4 h-4 text-[#FFD700] shrink-0 mt-0.5" />
                        <div>
                            <strong className="text-[#FFD700]">Testnet first.</strong>{" "}
                            Required permissions: <code className="text-white">Read</code> + <code className="text-white">Spot Trading</code>.
                            <span className="text-[#FF3B30]"> Never grant Withdrawals.</span> Keys are AES-256-GCM encrypted before storage.
                            {!supportsSandbox && form.exchange_id && (
                                <div className="mt-1.5 text-[#FFB000]">
                                    ⚠ {selected.label} has no public sandbox. Testnet flag here keeps the route-layer kill-switch on
                                    (no real orders without <code>BINANCE_LIVE_ENABLED=true</code>) but API calls hit live endpoints.
                                </div>
                            )}
                        </div>
                    </div>
                </div>

                <form onSubmit={onSubmit} className="p-5 space-y-4">
                    {formErr && (
                        <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono flex items-start gap-2"
                            data-testid="add-crypto-form-error">
                            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                            <span className="break-words">{formErr}</span>
                        </div>
                    )}
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5 flex items-center justify-between">
                            <span>EXCHANGE</span>
                            <span className="font-mono text-[9px] text-[#52525B] normal-case tracking-normal">
                                ● reachable from this server · ○ blocked
                            </span>
                        </label>
                        <select required value={form.exchange_id}
                            onChange={(e) => set("exchange_id", e.target.value)} data-testid="form-exchange"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none">
                            {exchanges.map(x => {
                                // Plain-text reachability marker that survives <option> styling
                                // restrictions (HTML <option> can't render coloured spans).
                                const reachMark = x.reachable === false ? "○ "
                                                : x.reachable === true ? "● " : "  ";
                                const reachSuffix = x.reachable === false ?
                                    ` · ${x.reach_error || "unreachable"}` : "";
                                return (
                                    <option key={x.id} value={x.id} disabled={x.reachable === false}>
                                        {reachMark}{x.label}
                                        {x.requires_passphrase ? " (passphrase required)" : ""}
                                        {!x.supports_sandbox ? " · live only" : ""}
                                        {reachSuffix}
                                    </option>
                                );
                            })}
                        </select>
                        {selected.id && selected.reachable === false && (
                            <div className="mt-1.5 text-[10px] text-[#FF3B30] font-mono flex items-center gap-1.5"
                                 data-testid="exchange-unreachable-warning">
                                <AlertTriangle className="w-3 h-3 shrink-0" />
                                Cannot reach {selected.label} from this server ({selected.reach_error || "blocked"}).
                                Pick a different exchange above.
                            </div>
                        )}
                        {selected.id && selected.reachable === true && (
                            <div className="mt-1.5 text-[10px] text-[#00FF41] font-mono flex items-center gap-1.5"
                                 data-testid="exchange-reachable-ok">
                                <ShieldCheck className="w-3 h-3 shrink-0" />
                                {selected.label} API reachable from this server.
                            </div>
                        )}
                    </div>
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">LABEL</label>
                        <input type="text" required value={form.label}
                            onChange={(e) => set("label", e.target.value)} data-testid="form-label"
                            placeholder={`My ${selected.label || "Crypto"} Account`}
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">API KEY</label>
                        <input type="text" required value={form.api_key}
                            onChange={(e) => set("api_key", e.target.value)} data-testid="form-api-key"
                            placeholder="paste API key"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">API SECRET</label>
                        <input type="password" required value={form.api_secret}
                            onChange={(e) => set("api_secret", e.target.value)} data-testid="form-api-secret"
                            placeholder="paste API secret"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    {needsPassphrase && (
                        <div data-testid="form-passphrase-wrap">
                            <label className="font-mono text-[10px] tracking-widest text-[#52525B] block mb-1.5">
                                API PASSPHRASE <span className="text-[#FFD700]">(required for {selected.label})</span>
                            </label>
                            <input type="password" required value={form.api_passphrase}
                                onChange={(e) => set("api_passphrase", e.target.value)} data-testid="form-api-passphrase"
                                placeholder="the passphrase you chose when generating the API key"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                        </div>
                    )}
                    <label className="flex items-center gap-2 cursor-pointer">
                        <input type="checkbox" checked={form.testnet}
                            onChange={(e) => set("testnet", e.target.checked)} data-testid="form-testnet"
                            className="accent-[#FFD700]" />
                        <span className="font-mono text-xs">
                            Testnet {supportsSandbox ? "(recommended)" : "(safe-mode flag — no sandbox available for this exchange)"}
                        </span>
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
                const tickerSymbol = (account.base_currency || "USDT") === "USD" ? "BTC/USD" : "BTC/USDT";
                const [b, t] = await Promise.all([
                    api.get(`/crypto/accounts/${account.id}/balance`),
                    api.get(`/crypto/accounts/${account.id}/ticker?symbol=${encodeURIComponent(tickerSymbol)}`),
                ]);
                if (!cancelled) { setBalance(b.data); setTicker(t.data); }
            } catch (e) { if (!cancelled) setErr(formatApiError(e)); }
            finally { if (!cancelled) setLoading(false); }
        };
        run();
        return () => { cancelled = true; };
    }, [account.id, account.base_currency]);

    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="inspect-modal" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#0099FF]/40 max-w-xl w-full max-h-[85vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                    <Zap className="w-5 h-5 text-[#0099FF]" />
                    <div className="flex-1 min-w-0">
                        <div className="font-mono text-[10px] text-[#0099FF] tracking-widest">INSPECT · LIVE EXCHANGE STATE</div>
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
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">
                                    {ticker.symbol || "BTC/USDT"} · LIVE TICKER
                                </div>
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
