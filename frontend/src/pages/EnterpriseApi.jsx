import { useEffect, useState, useCallback } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { KeyRound, Plus, Copy, Trash2, AlertTriangle, Terminal, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

const SCOPES = [
    { id: "read:accounts", label: "read:accounts", desc: "Account list, balances, equity, connection state" },
    { id: "read:trades", label: "read:trades", desc: "Trade history with filters & pagination" },
    { id: "read:portfolio", label: "read:portfolio", desc: "Aggregate portfolio snapshot across accounts" },
];

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

function CreateKeyForm({ onCreated }) {
    const [name, setName] = useState("");
    const [scopes, setScopes] = useState(["read:accounts", "read:trades", "read:portfolio"]);
    const [rate, setRate] = useState(120);
    const [busy, setBusy] = useState(false);

    const toggle = (s) => setScopes(prev => prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s]);

    const submit = async (e) => {
        e.preventDefault();
        if (scopes.length === 0) { toast.error("Select at least one scope"); return; }
        setBusy(true);
        try {
            const { data } = await api.post("/api-keys", { name, scopes, rate_limit_per_minute: rate });
            onCreated(data);
            setName("");
        } catch (err) { toast.error(formatApiError(err)); }
        finally { setBusy(false); }
    };

    return (
        <form onSubmit={submit} className="border border-[#00FF41]/40 bg-[#0A0A0A] p-5 space-y-4" data-testid="create-key-form">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">KEY NAME</label>
                    <input value={name} onChange={e => setName(e.target.value)} required maxLength={100}
                        data-testid="key-name-input" placeholder="Trading desk integration"
                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm outline-none" />
                </div>
                <div>
                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">RATE LIMIT · REQ/MIN</label>
                    <input type="number" min="1" max="10000" value={rate}
                        onChange={e => setRate(parseInt(e.target.value, 10) || 120)}
                        data-testid="key-rate-input"
                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                </div>
            </div>
            <div>
                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">SCOPES · least privilege recommended</label>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                    {SCOPES.map(s => (
                        <label key={s.id} className={`flex items-start gap-2 p-3 border cursor-pointer transition-colors ${
                            scopes.includes(s.id) ? "border-[#00FF41]/40 bg-[#00FF41]/5" : "border-[#1F1F1F] hover:border-[#333333]"
                        }`}>
                            <input type="checkbox" checked={scopes.includes(s.id)} onChange={() => toggle(s.id)}
                                data-testid={`scope-${s.id.replace(":", "-")}`} className="accent-[#00FF41] mt-0.5" />
                            <span>
                                <span className="font-mono text-xs text-[#E4E4E7] block">{s.label}</span>
                                <span className="text-[10px] text-[#52525B] leading-tight block mt-0.5">{s.desc}</span>
                            </span>
                        </label>
                    ))}
                </div>
            </div>
            <div className="flex justify-end">
                <button type="submit" disabled={busy} data-testid="create-key-submit"
                    className="px-4 py-2 bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium text-xs tracking-widest disabled:opacity-50 flex items-center gap-1.5">
                    <Plus className="w-3.5 h-3.5" /> {busy ? "GENERATING…" : "GENERATE KEY"}
                </button>
            </div>
        </form>
    );
}

function KeyRevealModal({ apiKey, onClose }) {
    const copy = async () => {
        try { await navigator.clipboard.writeText(apiKey); toast.success("API key copied to clipboard"); }
        catch (e) { toast.error("Copy failed — select and copy manually", { description: e?.message }); }
    };
    return (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="key-reveal-modal">
            <div className="bg-[#0A0A0A] border border-[#FFD700]/50 max-w-lg w-full p-6 space-y-4">
                <div className="flex items-center gap-2">
                    <KeyRound className="w-5 h-5 text-[#FFD700]" />
                    <h2 className="font-display font-bold text-lg tracking-tight">Your new API key</h2>
                </div>
                <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 flex items-start gap-2">
                    <AlertTriangle className="w-4 h-4 text-[#FF3B30] shrink-0 mt-0.5" />
                    <p className="text-xs text-[#FF3B30] font-mono leading-relaxed">
                        SHOWN ONCE — we store only a hash. Copy it into your secrets manager now.
                        If lost, revoke and generate a new key.
                    </p>
                </div>
                <code className="block font-mono text-xs px-3 py-3 bg-[#050505] border border-[#1F1F1F] break-all select-all"
                    data-testid="revealed-api-key">{apiKey}</code>
                <div className="flex justify-end gap-2">
                    <button onClick={copy} data-testid="copy-api-key"
                        className="px-4 py-2 bg-[#FFD700] hover:bg-[#FFE033] text-black font-bold text-xs tracking-widest flex items-center gap-1.5">
                        <Copy className="w-3.5 h-3.5" /> COPY KEY
                    </button>
                    <button onClick={onClose} data-testid="close-reveal-modal"
                        className="px-4 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest">
                        I&apos;VE SAVED IT
                    </button>
                </div>
            </div>
        </div>
    );
}

export default function EnterpriseApi() {
    const [keys, setKeys] = useState([]);
    const [loading, setLoading] = useState(true);
    const [showForm, setShowForm] = useState(false);
    const [revealed, setRevealed] = useState(null);

    const load = useCallback(async () => {
        try { const { data } = await api.get("/api-keys"); setKeys(data); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const revoke = async (k) => {
        if (!window.confirm(`Revoke "${k.name}" (${k.key_prefix}…)?\n\nAny system using this key loses access IMMEDIATELY. This cannot be undone.`)) return;
        try {
            await api.post(`/api-keys/${k.id}/revoke`);
            toast.success("Key revoked");
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const onCreated = (data) => {
        setRevealed(data.api_key);
        setShowForm(false);
        load();
    };

    const active = keys.filter(k => !k.revoked_at);

    return (
        <AppLayout>
            <PageHeader
                title="Enterprise API"
                subtitle="Programmatic read access to accounts, trades and portfolio — for institutional integrations."
                testid="enterprise-api-header"
                action={
                    <button onClick={() => setShowForm(v => !v)} data-testid="new-key-button"
                        className="flex items-center gap-2 px-3 py-2 bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium text-xs tracking-widest transition-colors">
                        <Plus className="w-3.5 h-3.5" /> NEW API KEY
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-4" data-testid="enterprise-api-page">
                {showForm && <CreateKeyForm onCreated={onCreated} />}

                {/* Keys table */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="api-keys-table">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <ShieldCheck className="w-4 h-4 text-[#00FF41]" />
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                            API KEYS · {active.length}/10 ACTIVE · SHA-256 HASHED AT REST
                        </div>
                    </div>
                    {loading ? (
                        <div className="p-5 font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                    ) : keys.length === 0 ? (
                        <div className="p-12 text-center" data-testid="api-keys-empty">
                            <KeyRound className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                            <div className="font-display font-bold text-lg mb-1">No API keys yet</div>
                            <div className="text-sm text-[#A1A1AA]">Generate a key to start pulling your data programmatically.</div>
                        </div>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="w-full min-w-[720px]">
                                <thead>
                                    <tr className="border-b border-[#1F1F1F]">
                                        {["NAME", "KEY", "SCOPES", "RATE", "LAST USED", "REQUESTS", "STATUS", ""].map(h => (
                                            <th key={h} className="text-left px-4 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">{h}</th>
                                        ))}
                                    </tr>
                                </thead>
                                <tbody>
                                    {keys.map(k => (
                                        <tr key={k.id} className={`border-b border-[#1F1F1F] last:border-b-0 ${k.revoked_at ? "opacity-40" : ""}`}
                                            data-testid={`key-row-${k.key_prefix}`}>
                                            <td className="px-4 py-3 font-display font-bold text-sm">{k.name}</td>
                                            <td className="px-4 py-3 font-mono text-xs text-[#A1A1AA]">{k.key_prefix}…</td>
                                            <td className="px-4 py-3">
                                                <div className="flex flex-wrap gap-1">
                                                    {k.scopes.map(s => (
                                                        <span key={s} className="font-mono text-[9px] tracking-wide px-1.5 py-0.5 border border-[#00BFFF]/40 text-[#00BFFF]">{s}</span>
                                                    ))}
                                                </div>
                                            </td>
                                            <td className="px-4 py-3 font-mono text-xs tabular-nums">{k.rate_limit_per_minute}/m</td>
                                            <td className="px-4 py-3 font-mono text-[10px] text-[#A1A1AA]">{fmtDate(k.last_used_at)}</td>
                                            <td className="px-4 py-3 font-mono text-xs tabular-nums">{k.total_requests}</td>
                                            <td className="px-4 py-3">
                                                <span className={`font-mono text-[9px] tracking-widest px-1.5 py-0.5 border ${
                                                    k.revoked_at ? "border-[#FF3B30]/40 text-[#FF3B30]" : "border-[#00FF41]/40 text-[#00FF41]"
                                                }`}>
                                                    {k.revoked_at ? "REVOKED" : "ACTIVE"}
                                                </span>
                                            </td>
                                            <td className="px-4 py-3">
                                                {!k.revoked_at && (
                                                    <button onClick={() => revoke(k)} data-testid={`revoke-key-${k.key_prefix}`}
                                                        title="Revoke this key immediately"
                                                        className="p-1.5 border border-[#FF3B30]/30 text-[#FF3B30] hover:bg-[#FF3B30]/10 transition-colors">
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    </button>
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>

                {/* Quick-start docs */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="api-docs-card">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <Terminal className="w-4 h-4 text-[#00BFFF]" />
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">QUICK START · REST v1 · X-API-Key HEADER</div>
                    </div>
                    <div className="p-5 space-y-3">
                        {[
                            ["Verify key & scopes", `curl -H "X-API-Key: stoic_live_..." ${API}/v1/me`],
                            ["List accounts", `curl -H "X-API-Key: stoic_live_..." ${API}/v1/accounts`],
                            ["Closed trades (paginated)", `curl -H "X-API-Key: stoic_live_..." "${API}/v1/trades?status=closed&limit=50&offset=0"`],
                            ["Portfolio snapshot", `curl -H "X-API-Key: stoic_live_..." ${API}/v1/portfolio`],
                        ].map(([label, cmd]) => (
                            <div key={label}>
                                <div className="font-mono text-[10px] text-[#A1A1AA] tracking-widest mb-1">{label.toUpperCase()}</div>
                                <code className="block font-mono text-[11px] px-3 py-2 bg-[#050505] border border-[#1F1F1F] text-[#00FF41] break-all select-all">{cmd}</code>
                            </div>
                        ))}
                        <p className="font-mono text-[10px] text-[#52525B] tracking-widest leading-relaxed pt-1">
                            READ-ONLY · 401 = BAD/REVOKED KEY · 403 = MISSING SCOPE · 429 = RATE LIMITED ·
                            TRADES FILTERS: status, symbol, account_id, from_date, to_date (ISO), limit ≤ 500, offset
                        </p>
                    </div>
                </div>
            </div>

            {revealed && <KeyRevealModal apiKey={revealed} onClose={() => setRevealed(null)} />}
        </AppLayout>
    );
}
