import { useEffect, useState, useCallback } from "react";
import { useSearchParams, Link } from "react-router-dom";
import api, { formatApiError, API } from "@/lib/api";
import { AccountCertification } from "@/components/AccountCertification";
import { QuickInstallPanel } from "@/components/QuickInstallPanel";
import { TrustedTerminals } from "@/components/TrustedTerminals";
import PartnerBrokerCard from "@/components/PartnerBrokerCard";
import MultiAccountOverview from "@/components/MultiAccountOverview";

// Bump together with backend `LATEST_EA` in bot_routes.py / diagnostic_routes.py.
// Used in the download URL so the filename changes per release (e.g.
// `EmergentTradingBridge_v1.35.mq5`) — defeats aggressive browser caching
// of the prior .mq5, which otherwise re-downloads stale source.
const LATEST_EA_VERSION = "1.56";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Plus, Trash2 as Trash, Copy, Download, RefreshCw as ArrowsClockwise, Plug, PlugZap as PlugsConnected, Info, Lock, Eye, EyeOff, KeyRound, Layers, ChevronDown, CheckCircle2, AlertTriangle, ExternalLink, Folder, Terminal, Wand2, Save, X, Zap as Lightning } from "lucide-react";
const Warning = AlertTriangle;
import { useLiveStream } from "@/lib/useLiveStream";
import { toast } from "sonner";

/** iter-76 · Symbol-suffix row.
 *  Shows the per-account suffix in 3 possible states:
 *    1. Manually set by user — yellow badge "MANUAL"
 *    2. Auto-detected by EA v1.34+ — cyan badge "AUTO-DETECTED"
 *    3. Neither (bare base name) — grey badge "BARE"
 *  Includes an inline edit input so the user can override. */
function SymbolSuffixRow({ account, onSet }) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(account.symbol_suffix || "");
    const userSuffix = (account.symbol_suffix || "").trim();
    const autoSuffix = (account.auto_detected_symbol_suffix || "").trim();
    const autoConf = account.auto_detected_suffix_confidence;
    const autoBases = account.auto_detected_suffix_bases || [];

    const active = userSuffix || autoSuffix;
    let source = "BARE";
    let color = "#52525B";
    if (userSuffix) { source = "MANUAL"; color = "#FFD700"; }
    else if (autoSuffix) { source = "AUTO-DETECTED"; color = "#10F2C5"; }

    return (
        <div className="font-mono text-[10px] tracking-widest" data-testid={`symbol-suffix-row-${account.account_number}`}>
            <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[#52525B]">SYMBOL SUFFIX</span>
                <span className="text-[#A1A1AA]">
                    XAUUSD<span style={{ color }} className="font-bold">{active || "(none)"}</span>
                </span>
                <span className="border px-1.5 py-0.5" style={{ color, borderColor: `${color}55` }}>
                    {source}
                </span>
                {autoSuffix && !userSuffix && (
                    <span className="text-[#52525B]">
                        from {autoBases.length} symbol{autoBases.length !== 1 ? "s" : ""} · {Math.round((autoConf || 0) * 100)}% confidence
                    </span>
                )}
                {!editing && (
                    <button
                        onClick={() => { setEditing(true); setDraft(userSuffix); }}
                        data-testid={`edit-suffix-${account.account_number}`}
                        className="ml-auto px-2 py-1 border border-[#1F1F1F] hover:border-[#FFD700]/40 hover:text-[#FFD700] text-[#A1A1AA] transition-colors flex items-center gap-1">
                        <Wand2 className="w-3 h-3" /> {userSuffix ? "OVERRIDE" : "SET MANUAL"}
                    </button>
                )}
            </div>
            {editing && (
                <div className="flex items-center gap-2 mt-2">
                    <input
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        placeholder=".fx / .c / .raw / (leave empty = use auto)"
                        data-testid={`suffix-input-${account.account_number}`}
                        className="flex-1 px-3 py-1.5 bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700]/50 outline-none text-xs font-mono text-[#E4E4E7]" />
                    <button
                        onClick={async () => { await onSet(draft.trim()); setEditing(false); }}
                        data-testid={`save-suffix-${account.account_number}`}
                        className="px-3 py-1.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 text-[10px] tracking-widest flex items-center gap-1">
                        <Save className="w-3 h-3" /> SAVE
                    </button>
                    <button
                        onClick={() => setEditing(false)}
                        className="px-3 py-1.5 border border-[#1F1F1F] hover:border-[#333333] text-[#52525B] text-[10px] tracking-widest flex items-center gap-1">
                        <X className="w-3 h-3" /> CANCEL
                    </button>
                </div>
            )}
        </div>
    );
}

const empty = { label: "", broker: "", server: "", account_number: "", account_type: "microcent", account_role: "STANDARD", pamm_provider: "", pamm_program_id: "", pamm_broker_program_id: "", base_currency: "USD", mode: "paper", initial_balance: 10000, investor_password: "", master_password: "" };

export default function Accounts() {
    const [accounts, setAccounts] = useState([]);
    const [limits, setLimits] = useState(null);
    const [presets, setPresets] = useState([]);
    const [showForm, setShowForm] = useState(false);
    const [form, setForm] = useState(empty);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");
    const [loading, setLoading] = useState(true);
    const [certMap, setCertMap] = useState({});
    const [searchParams] = useSearchParams();
    const focusId = searchParams.get("focus");

    const load = useCallback(async () => {
        try {
            const [a, l, c] = await Promise.all([
                api.get("/accounts"), api.get("/accounts/limits"),
                api.get("/accounts/certification").catch(() => ({ data: { items: [] } }))]);
            const cm = {};
            (c.data?.items || []).forEach(i => { cm[i.account_id] = i; });
            setCertMap(cm);
            setAccounts(a.data);
            setLimits(l.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    // Fix-It Shortcuts: ?focus=<accountId> scrolls to and highlights the card
    useEffect(() => {
        if (!focusId || loading) return;
        const el = document.getElementById(`account-${focusId}`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, [focusId, loading]);

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
                ? {
                    ...a,
                    // balance/equity can legitimately be null when the EA is
                    // attached to the wrong MT5 terminal (mismatch detection).
                    // Preserve null so the UI shows "—" instead of stale data.
                    balance: p.balance ?? null,
                    equity: p.equity ?? null,
                    last_heartbeat: p.last_heartbeat,
                    status: p.status || (p.broker_account_mismatch ? "disconnected" : "connected"),
                    broker_account_mismatch: p.broker_account_mismatch ?? a.broker_account_mismatch,
                    broker_account_mismatch_reason: p.broker_account_mismatch_reason ?? a.broker_account_mismatch_reason,
                    broker_account_id_reported: p.broker_account_id_reported ?? a.broker_account_id_reported,
                }
                : a));
        }
        if (lastEvent.type === "broker_sync_complete") {
            const p = lastEvent.payload;
            toast.success("Broker sync complete", {
                description: `${p.deals_pushed} broker deal${p.deals_pushed === 1 ? "" : "s"} re-scanned · ${p.trades_repaired} trade record${p.trades_repaired === 1 ? "" : "s"} repaired with exact figures.`,
            });
            setAccounts(prev => prev.map(a => a.id === p.account_id
                ? { ...a, last_full_sync_at: p.completed_at, pending_history_sync: null }
                : a));
        }
    }, [lastEvent]);

    const create = async (e) => {
        e.preventDefault(); setErr(""); setMsg("");
        try {
            const payload = { ...form };
            for (const k of ["pamm_provider", "pamm_program_id", "pamm_broker_program_id"]) {
                if (!payload[k] || payload.account_role === "STANDARD") payload[k] = null;
            }
            await api.post("/accounts", payload);
            setShowForm(false); setForm(empty); setMsg("Account added.");
            await load();
        } catch (e2) { setErr(formatApiError(e2)); }
    };

    const remove = async (id) => {
        if (!window.confirm("Delete this account? Trades remain in history.")) return;
        try { await api.delete(`/accounts/${id}`); await load(); } catch (e) { setErr(formatApiError(e)); }
    };

    const rotate = async (id) => {
        if (!window.confirm("Rotate the bridge token? The old token keeps working for 15 minutes so a live EA can switch over.")) return;
        try {
            const { data } = await api.post(`/accounts/${id}/rotate-token`);
            setRevealedTokens(prev => ({ ...prev, [id]: data.bridge_token }));
            setMsg("New bridge token generated — it is shown ONCE below. Copy it into the EA now; it will be masked afterwards.");
        } catch (e) { setErr(formatApiError(e)); }
    };

    const revokeToken = async (id) => {
        if (!window.confirm("Revoke the bridge token? The EA stops authenticating IMMEDIATELY until you rotate a new one.")) return;
        try {
            await api.post(`/accounts/${id}/bridge-token/revoke`);
            setRevealedTokens(prev => { const n = { ...prev }; delete n[id]; return n; });
            setMsg("Bridge token revoked — rotate to issue a new one.");
            await load();
        } catch (e) { setErr(formatApiError(e)); }
    };

    const copyToken = async (t) => {
        try {
            await navigator.clipboard.writeText(t);
            toast.success("Bridge token copied", {
                description: "Paste it into the EA's BridgeToken input on MT5.",
            });
        } catch (e) {
            toast.error("Couldn't copy — please copy manually", {
                description: e?.message,
            });
        }
    };

    const [refreshingBalance, setRefreshingBalance] = useState({});
    // Bridge token is a SECRET (audit F-05): the full value is shown only
    // once, right after creation/rotation. Otherwise only masked metadata.
    const [revealedTokens, setRevealedTokens] = useState({});
    const [tokenMeta, setTokenMeta] = useState({});
    const fetchTokenMeta = async (id) => {
        try {
            const { data } = await api.get(`/accounts/${id}/bridge-token`);
            setTokenMeta(prev => ({ ...prev, [id]: data }));
        } catch (e) {
            toast.error("Couldn't fetch token status", { description: formatApiError(e) });
        }
    };
    const [importAccount, setImportAccount] = useState(null);   // account selected in the Import Positions modal
    const refreshBalance = async (id) => {
        setRefreshingBalance(prev => ({ ...prev, [id]: true }));
        try {
            // Reuses the existing connection-test endpoint — it returns the
            // current balance/equity from the account doc (last heartbeat).
            const { data } = await api.get(`/accounts/${id}/test-connection`);
            setAccounts(prev => prev.map(a => a.id === id
                ? { ...a, balance: data.balance ?? a.balance,
                    equity: data.equity ?? a.equity,
                    last_heartbeat: data.last_heartbeat ?? a.last_heartbeat,
                    open_positions: data.open_positions ?? a.open_positions,
                    status: data.connected ? "connected" : "disconnected" }
                : a));
            toast.success("Balance refreshed", {
                description: data.connected
                    ? `Latest from MT5: $${(data.balance ?? 0).toFixed(2)}`
                    : "EA heartbeat is stale — start MT5 + EA to update.",
            });
        } catch (e) {
            toast.error("Refresh failed", { description: formatApiError(e) });
        } finally {
            setRefreshingBalance(prev => ({ ...prev, [id]: false }));
        }
    };

    // "Updated Xs ago" timestamp — recomputed each tick so it counts up smoothly.
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
        const id = setInterval(() => setNow(Date.now()), 1000);
        return () => clearInterval(id);
    }, []);
    const ageString = (iso) => {
        if (!iso) return null;
        const t = Date.parse(iso);
        if (isNaN(t)) return null;
        const s = Math.max(0, Math.round((now - t) / 1000));
        if (s < 60)    return `${s}s`;
        if (s < 3600)  return `${Math.round(s / 60)}m`;
        if (s < 86400) return `${Math.round(s / 3600)}h`;
        return `${Math.round(s / 86400)}d`;
    };

    const [testResults, setTestResults] = useState({});
    const [testing, setTesting] = useState({});

    const runConnectionTest = async (id) => {
        setTesting(prev => ({ ...prev, [id]: true }));
        try {
            const { data } = await api.get(`/accounts/${id}/test-connection`);
            setTestResults(prev => ({ ...prev, [id]: data }));
        } catch (e) {
            setTestResults(prev => ({
                ...prev,
                [id]: { connected: false, diagnostic: { severity: "error", message: formatApiError(e) } },
            }));
        } finally {
            setTesting(prev => ({ ...prev, [id]: false }));
        }
    };

    const isFresh = (iso) => {
        if (!iso) return false;
        return (Date.now() - new Date(iso).getTime()) < 60_000;
    };

    // iter-86 · Manual "Force Test Trade" — fires a 0.01 BUY through the
    // full execution pipe to prove the broker connection actually trades.
    const [forcingTest, setForcingTest] = useState({});
    const forceTestTrade = async (id, label) => {
        if (!window.confirm(
            `Fire a small test trade on "${label}"?\n\n` +
            "• BUY 0.01 lot of XAUUSD/BTCUSD/EURUSD (whichever your broker offers).\n" +
            "• Tight TP — closes within seconds in liquid markets.\n" +
            "• Tagged as a test — excluded from win-rate & PnL analytics.\n\n" +
            "Use this to validate that this broker can actually execute orders."
        )) return;
        setForcingTest(prev => ({ ...prev, [id]: true }));
        try {
            const { data } = await api.post(`/accounts/${id}/test-trade`);
            toast.success("Test trade queued", {
                description: data.message || `BUY ${data.lot_size} ${data.symbol} dispatched. Watch the Trades page.`,
            });
        } catch (e) {
            toast.error("Test trade refused", { description: formatApiError(e) });
        } finally {
            setForcingTest(prev => ({ ...prev, [id]: false }));
        }
    };

    // iter-46 · Deep broker sync — EA re-scans 7 days of MT5 deal history and
    // repairs any STOIC trade still carrying estimated/missing P&L.
    const [syncing, setSyncing] = useState({});
    const requestSync = async (id) => {
        setSyncing(prev => ({ ...prev, [id]: true }));
        try {
            const { data } = await api.post(`/accounts/${id}/request-sync`);
            const days = Math.round((data.lookback_seconds || 604800) / 86400);
            toast.success("Broker sync queued", {
                description: `Your EA will re-scan the last ${days} days of broker history within ~10s and repair any inexact trade records. You'll get a confirmation here when it finishes.`,
            });
        } catch (e) {
            toast.error("Sync request failed", { description: formatApiError(e) });
        } finally {
            setTimeout(() => setSyncing(prev => ({ ...prev, [id]: false })), 2000);
        }
    };

    // iter-137 · Multi-account management — patch metadata (group / trading toggle)
    const [overviewKey, setOverviewKey] = useState(0);
    const patchAccount = async (id, payload, okMsg) => {
        try {
            await api.patch(`/accounts/${id}`, payload);
            if (okMsg) toast.success(okMsg);
            await load();
            setOverviewKey(k => k + 1);
        } catch (e) { toast.error(formatApiError(e)); }
    };
    const editGroup = (a) => {
        const g = window.prompt(
            "Group label for this account (e.g. AGGRESSIVE, PROP, FAMILY).\nLeave empty to remove the group.",
            a.group || "");
        if (g === null) return;
        patchAccount(a.id, { group: g }, g.trim() ? `Group set: ${g.trim()}` : "Group removed");
    };

    return (
        <AppLayout>
            <PageHeader
                title="MT5 Accounts"
                subtitle="Connect MetaTrader 5 microcent accounts to execute live trades."
                testid="accounts-header"
                action={
                    <div className="flex gap-2">
                        <a href={`${API}/ea-script?v=${LATEST_EA_VERSION}`} target="_blank" rel="noopener noreferrer" download={`EmergentTradingBridge_v${LATEST_EA_VERSION}.mq5`}
                            data-testid="download-ea-button"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                            <Download className="w-3.5 h-3.5" /> DOWNLOAD EA v{LATEST_EA_VERSION}
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

                {/* Multi-account portfolio overview (iter-137) */}
                <MultiAccountOverview refreshKey={overviewKey} />

                {/* Bridge instructions */}
                <MT5ConnectionGuide />

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
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">ACCOUNT ROLE (PAMM)</label>
                            <select value={form.account_role} onChange={e => setForm({ ...form, account_role: e.target.value })}
                                data-testid="account-role-select"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm outline-none">
                                <option value="STANDARD">Standard — normal STOIC execution</option>
                                <option value="PAMM_MASTER">PAMM Master — executes via certified PAMM program</option>
                                <option value="PAMM_INVESTOR">PAMM Investor — monitor only, never trades</option>
                            </select>
                            {form.account_role === "PAMM_INVESTOR" && (
                                <p className="font-mono text-[10px] text-[#FFB000] mt-1" data-testid="investor-role-note">
                                    Execution Authority will be LOCKED — STOIC never places orders on investor accounts.
                                </p>
                            )}
                        </div>
                        {form.account_role !== "STANDARD" && (
                            <div className="md:col-span-2 grid grid-cols-1 md:grid-cols-3 gap-3">
                                <div>
                                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">PAMM PROVIDER</label>
                                    <input value={form.pamm_provider} onChange={e => setForm({ ...form, pamm_provider: e.target.value })}
                                        data-testid="pamm-provider-input" placeholder="e.g. broker PAMM desk"
                                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                                </div>
                                <div>
                                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">STOIC PAMM PROGRAM ID</label>
                                    <input value={form.pamm_program_id} onChange={e => setForm({ ...form, pamm_program_id: e.target.value })}
                                        data-testid="pamm-program-id-input"
                                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                                </div>
                                <div>
                                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">BROKER PROGRAM ID</label>
                                    <input value={form.pamm_broker_program_id} onChange={e => setForm({ ...form, pamm_broker_program_id: e.target.value })}
                                        data-testid="pamm-broker-program-id-input"
                                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                                </div>
                            </div>
                        )}

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
                                <div key={a.id} id={`account-${a.id}`}
                                    className={`border ${a.id === focusId ? "border-[#FFD700] shadow-[0_0_0_1px_#FFD700]" : "border-[#1F1F1F]"} bg-[#0A0A0A] p-5`}
                                    data-testid={`account-row-${a.account_number}`}>
                                    {a.broker_account_mismatch && (
                                        <div className="mb-4 border border-[#FFB000]/40 bg-[#FFB000]/10 px-3 py-2 flex items-start gap-2"
                                            data-testid={`broker-mismatch-${a.account_number}`}>
                                            <Warning className="w-4 h-4 text-[#FFB000] mt-0.5 shrink-0" />
                                            <div className="font-mono text-[11px] text-[#FFB000] leading-relaxed">
                                                <span className="tracking-widest">WRONG MT5 TERMINAL · </span>
                                                {a.broker_account_mismatch_reason ||
                                                    `EA is logged into MT5 account ${a.broker_account_id_reported}, but this STOIC account is configured for ${a.account_number}.`}
                                                <div className="text-[#A1A1AA] tracking-normal mt-1">
                                                    Run this EA on a DIFFERENT MT5 instance (or remove the duplicate). Two EAs attached to the same terminal will mirror the same balance.
                                                </div>
                                            </div>
                                        </div>
                                    )}
                                    <div className="flex items-start justify-between gap-3 flex-wrap">
                                        <div className="space-y-1">
                                            <div className="flex items-center gap-3 flex-wrap">
                                                <div className="font-display font-bold text-lg">{a.label}</div>
                                                <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${
                                                    live ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"
                                                }`}>
                                                    {live ? <><PlugsConnected className="w-3 h-3 inline mr-1" /> CONNECTED</> : "● DISCONNECTED"}
                                                </span>
                                                <button
                                                    onClick={() => patchAccount(a.id, { trading_enabled: a.trading_enabled === false },
                                                        a.trading_enabled === false ? `Trading enabled on ${a.label}` : `Trading disabled on ${a.label}`)}
                                                    data-testid={`trading-toggle-${a.account_number}`}
                                                    title={a.trading_enabled === false
                                                        ? "Trading is OFF — the bot skips this account. Click to enable."
                                                        : "Trading is ON — click to exclude this account from all bot trading."}
                                                    className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border transition-colors ${
                                                        a.trading_enabled === false
                                                            ? "border-[#FF3B30]/40 text-[#FF3B30] bg-[#FF3B30]/10 hover:bg-[#FF3B30]/20"
                                                            : "border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10"
                                                    }`}>
                                                    {a.trading_enabled === false ? "⏻ TRADING OFF" : "⏻ TRADING ON"}
                                                </button>
                                                {a.trading_enabled !== false && (() => {
                                                    const c = certMap[a.id];
                                                    if (!c || c.passed >= c.total) return null;
                                                    return (
                                                        <span data-testid={`cert-blocked-badge-${a.account_number}`}
                                                            title="Requested ON is not authority — every go-live certification check must pass before this account can open trades"
                                                            className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#FF3B30]/40 text-[#FF3B30] bg-[#FF3B30]/10">
                                                            REQUESTED ON · CERTIFICATION BLOCKED ({c.passed}/{c.total})
                                                        </span>
                                                    );
                                                })()}
                                                <button onClick={() => editGroup(a)}
                                                    data-testid={`group-badge-${a.account_number}`}
                                                    title="Group accounts for portfolio organisation (click to edit)"
                                                    className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border transition-colors ${
                                                        a.group
                                                            ? "border-[#00BFFF]/40 text-[#00BFFF] hover:bg-[#00BFFF]/10"
                                                            : "border-[#1F1F1F] text-[#52525B] hover:border-[#333333] hover:text-[#A1A1AA]"
                                                    }`}>
                                                    {a.group ? `◈ ${a.group.toUpperCase()}` : "+ GROUP"}
                                                </button>
                                            </div>
                                            <div className="font-mono text-xs text-[#A1A1AA]">{a.broker} · {a.server} · #{a.account_number}</div>
                                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest flex items-center gap-2 flex-wrap">
                                                <span>TYPE · {a.account_type?.toUpperCase()} · {a.base_currency}</span>
                                                {a.account_role && a.account_role !== "STANDARD" && (
                                                    <span data-testid={`account-role-badge-${a.account_number}`}
                                                        className={`px-1.5 py-0.5 border font-mono text-[9px] tracking-widest ${a.account_role === "PAMM_INVESTOR" ? "border-[#FFB000]/50 text-[#FFB000]" : "border-[#00BFFF]/50 text-[#00BFFF]"}`}>
                                                        {a.account_role === "PAMM_INVESTOR" ? "PAMM INVESTOR · MONITOR ONLY" : "PAMM MASTER"}
                                                    </span>
                                                )}
                                                {a.account_role === "PAMM_INVESTOR" && (
                                                    <Link to={`/investor${a.pamm_program_id ? `?program=${encodeURIComponent(a.pamm_program_id)}` : ""}`}
                                                        data-testid={`account-investor-monitor-${a.account_number}`}
                                                        className="px-1.5 py-0.5 border border-[#FFB000]/50 text-[#FFB000] hover:bg-[#FFB000]/10 font-mono text-[9px] tracking-widest flex items-center gap-1">
                                                        <Eye className="w-3 h-3" /> OPEN MONITOR
                                                    </Link>
                                                )}
                                                {a.environment && (
                                                    <span data-testid={`account-environment-${a.account_number}`}
                                                        title="Server-owned capital environment — separate from connection state and telemetry freshness"
                                                        className={`px-1.5 py-0.5 border ${a.environment === "LIVE" ? "border-[#FF3B30]/40 text-[#FF3B30]" : a.environment === "DEMO" ? "border-[#FFB000]/40 text-[#FFB000]" : "border-[#00BFFF]/40 text-[#00BFFF]"}`}>
                                                        ENVIRONMENT · {a.environment}
                                                    </span>
                                                )}
                                            </div>
                                            {a.broker_account_id_reported && (
                                                <div className="font-mono text-[10px] tracking-widest flex items-center gap-1.5 mt-1"
                                                    data-testid={`ea-reading-${a.account_number}`}>
                                                    <span className="text-[#52525B]">EA READING FROM ·</span>
                                                    <span className={String(a.broker_account_id_reported) === String(a.account_number)
                                                        ? "text-[#00FF41]" : "text-[#FFB000]"}>
                                                        #{a.broker_account_id_reported}
                                                        {String(a.broker_account_id_reported) === String(a.account_number)
                                                            ? " ✓" : " ⚠ MISMATCH"}
                                                    </span>
                                                </div>
                                            )}
                                        </div>
                                        <div className="space-y-2 min-w-[280px]">
                                            <div className="grid grid-cols-2 gap-3" data-testid={`balance-card-${a.account_number}`}>
                                                <div className="p-3 border border-[#1F1F1F] bg-[#050505] relative">
                                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">BALANCE</div>
                                                    <div className="font-display font-bold text-2xl tabular-nums tracking-tight"
                                                        data-testid={`balance-${a.account_number}`}>
                                                        {a.balance == null ? <span className="text-[#52525B]">—</span> : `$${(a.balance).toFixed(2)}`}
                                                    </div>
                                                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-0.5">
                                                        {a.balance == null ? "AWAITING VALID HEARTBEAT" : (a.base_currency || "USD")}
                                                    </div>
                                                </div>
                                                <div className="p-3 border border-[#1F1F1F] bg-[#050505] relative">
                                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">EQUITY</div>
                                                    <div className="font-display font-bold text-2xl tabular-nums tracking-tight"
                                                        data-testid={`equity-${a.account_number}`}>
                                                        {a.equity == null ? <span className="text-[#52525B]">—</span> : `$${(a.equity).toFixed(2)}`}
                                                    </div>
                                                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-0.5">
                                                        {(a.open_positions ?? 0)} OPEN POSITION{(a.open_positions ?? 0) === 1 ? "" : "S"}
                                                    </div>
                                                </div>
                                            </div>

                                            <div className="flex items-center justify-between gap-3 px-1 flex-wrap">
                                                {(() => {
                                                    const age = ageString(a.last_heartbeat);
                                                    const fresh = a.last_heartbeat && (now - Date.parse(a.last_heartbeat)) < 10_000;
                                                    return (
                                                        <div className="flex items-center gap-2 font-mono text-[10px] tracking-widest"
                                                            data-testid={`balance-age-${a.account_number}`}>
                                                            <span className={`w-1.5 h-1.5 rounded-full ${fresh ? "bg-[#00FF41] pulse-dot" : "bg-[#52525B]"}`} />
                                                            <span className={fresh ? "text-[#00FF41]" : "text-[#52525B]"}>
                                                                {age ? `TELEMETRY · ${fresh ? "FRESH" : "STALE"} · ${age} AGO` : "TELEMETRY · NO HEARTBEAT YET"}
                                                            </span>
                                                        </div>
                                                    );
                                                })()}
                                                <button onClick={() => refreshBalance(a.id)}
                                                    disabled={!!refreshingBalance[a.id]}
                                                    data-testid={`refresh-balance-${a.account_number}`}
                                                    title="Pull the latest balance + equity from your MT5 EA"
                                                    className="flex items-center gap-1.5 px-2.5 py-1 border border-[#1F1F1F] hover:border-[#00FF41]/40 hover:text-[#00FF41] disabled:opacity-50 text-[10px] font-mono tracking-widest transition-colors">
                                                    <ArrowsClockwise className={`w-3 h-3 ${refreshingBalance[a.id] ? "animate-spin" : ""}`} />
                                                    {refreshingBalance[a.id] ? "REFRESHING…" : "REFRESH"}
                                                </button>
                                                <button onClick={() => setImportAccount(a)}
                                                    data-testid={`import-positions-${a.account_number}`}
                                                    title="Manually pull open positions from MT5 — useful for trades opened before the EA was attached."
                                                    className="flex items-center gap-1.5 px-2.5 py-1 border border-[#1F1F1F] hover:border-[#FFD700]/40 hover:text-[#FFD700] text-[10px] font-mono tracking-widest transition-colors">
                                                    <Download className="w-3 h-3" /> IMPORT POSITIONS
                                                </button>
                                            </div>
                                        </div>
                                    </div>

                                    <div className="mt-4 pt-4 border-t border-[#1F1F1F]">
                                        {(() => {
                                            const syms = a.available_symbols || [];
                                            // Only warn once we've actually received a symbol list (v1.34+ EA).
                                            // Empty list => legacy EA / never heartbeated with MarketWatch scan.
                                            if (syms.length === 0) return null;
                                            const hasGold = syms.some(s => /XAU|GOLD/i.test(s));
                                            if (hasGold) return null;
                                            return (
                                                <div className="mb-4 p-3 border border-[#FF3B30]/40 bg-[#FF3B30]/5"
                                                     data-testid={`missing-gold-warning-${a.account_number}`}>
                                                    <div className="flex items-start gap-2">
                                                        <AlertTriangle className="w-3.5 h-3.5 text-[#FF3B30] flex-shrink-0 mt-0.5" />
                                                        <div className="text-[11px] leading-relaxed">
                                                            <div className="font-mono text-[10px] tracking-widest text-[#FF3B30] mb-1">
                                                                NO GOLD SYMBOL IN MT5 MARKET WATCH
                                                            </div>
                                                            <div className="text-[#E4E4E7]">
                                                                This account&apos;s EA reports {syms.length} symbols, none of them XAUUSD/GOLD.
                                                                The bot will keep skipping every gold trade with <code className="text-[#FFB000]">symbol_not_offered_by_broker</code>.
                                                            </div>
                                                            <div className="text-[#A1A1AA] mt-2">
                                                                Fix: open this account&apos;s MT5 → right-click Market Watch → <strong className="text-[#E4E4E7]">Show All</strong>, or press <strong className="text-[#E4E4E7]">Ctrl+U</strong> and enable XAUUSD (or your broker&apos;s gold ticker). Wait ~15s for the next heartbeat.
                                                            </div>
                                                        </div>
                                                    </div>
                                                </div>
                                            );
                                        })()}
                                        <SymbolSuffixRow account={a} onSet={async (suffix) => {
                                            try {
                                                await api.put(`/accounts/${a.id}/symbol-suffix`, { symbol_suffix: suffix });
                                                toast.success(suffix ? `Suffix set: '${suffix}'` : "Suffix cleared (using auto-detected)");
                                                load();
                                            } catch (e) {
                                                toast.error(formatApiError(e));
                                            }
                                        }} />
                                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2 mt-4">BRIDGE TOKEN · secret — shown once at creation/rotation</div>
                                        <div className="flex items-center gap-2 flex-wrap">
                                            <code className="font-mono text-xs px-3 py-2 bg-[#050505] border border-[#1F1F1F] flex-1 break-all" data-testid={`bridge-token-${a.account_number}`}>
                                                {revealedTokens[a.id]
                                                    || tokenMeta[a.id]?.bridge_token_masked
                                                    || "•••••••••••••••• (masked)"}
                                            </code>
                                            {revealedTokens[a.id] ? (
                                                <button onClick={() => copyToken(revealedTokens[a.id])} data-testid={`copy-token-${a.account_number}`}
                                                    className="px-3 py-2 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 text-xs font-mono tracking-widest flex items-center gap-1 transition-colors">
                                                    <Copy className="w-3.5 h-3.5" /> COPY (SHOWN ONCE)
                                                </button>
                                            ) : (
                                                <button onClick={() => fetchTokenMeta(a.id)} data-testid={`token-status-${a.account_number}`}
                                                    title="Shows the masked token and when the EA last authenticated with it"
                                                    className="px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                                                    STATUS
                                                </button>
                                            )}
                                            <button onClick={() => revokeToken(a.id)} data-testid={`revoke-token-${a.account_number}`}
                                                title="Immediately invalidate this token — the EA stops authenticating until you rotate"
                                                className="px-3 py-2 border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 text-xs font-mono tracking-widest transition-colors">
                                                REVOKE
                                            </button>
                                            <button onClick={() => runConnectionTest(a.id)}
                                                disabled={!!testing[a.id]}
                                                data-testid={`test-connection-${a.account_number}`}
                                                className="px-3 py-2 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 text-xs font-mono tracking-widest flex items-center gap-1 transition-colors disabled:opacity-50">
                                                <PlugsConnected className="w-3.5 h-3.5" /> {testing[a.id] ? "TESTING…" : "TEST"}
                                            </button>
                                            {a.mode !== "paper" && (() => {
                                                const eaLive = a.last_heartbeat &&
                                                    (Date.now() - new Date(a.last_heartbeat).getTime()) < 180000;
                                                const cert = certMap[a.id];
                                                const certOk = !!cert && (cert.certified || cert.can_certify);
                                                const gateTitle = !eaLive
                                                    ? "EA disconnected — trade controls are locked. Use TEST for diagnostics and reconnect the terminal first."
                                                    : !certOk
                                                        ? `Locked — go-live certification ${cert ? `${cert.passed}/${cert.total}` : "unavailable"}. Every check must pass for this exact account before FORCE TRADE unlocks.`
                                                        : "Fire a 0.01 lot test trade to validate this broker's execution path";
                                                return (
                                                <button onClick={() => forceTestTrade(a.id, a.label)}
                                                    disabled={!!forcingTest[a.id] || !eaLive || !certOk}
                                                    data-testid={`force-test-trade-${a.account_number}`}
                                                    title={gateTitle}
                                                    className="px-3 py-2 border border-[#FFB020]/50 text-[#FFB020] hover:bg-[#FFB020]/10 text-xs font-mono tracking-widest flex items-center gap-1 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
                                                    <Lightning className="w-3.5 h-3.5" /> {forcingTest[a.id] ? "FIRING…" : (certOk || !eaLive ? "FORCE TRADE" : `FORCE TRADE · ${cert ? `${cert.passed}/${cert.total}` : "—"}`)}
                                                </button>
                                                );
                                            })()}
                                            <button onClick={() => requestSync(a.id)}
                                                disabled={!!syncing[a.id]}
                                                data-testid={`broker-sync-${a.account_number}`}
                                                title="Deep-sync: your EA re-scans the last 7 days of broker deal history and repairs any estimated or missing trade data with exact figures"
                                                className="px-3 py-2 border border-[#00BFFF]/40 text-[#00BFFF] hover:bg-[#00BFFF]/10 text-xs font-mono tracking-widest flex items-center gap-1 transition-colors disabled:opacity-50">
                                                <ArrowsClockwise className={`w-3.5 h-3.5 ${syncing[a.id] ? "animate-spin" : ""}`} /> {syncing[a.id] ? "QUEUED…" : "SYNC"}
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
                                        {(a.pending_history_sync || a.last_full_sync_at) && (
                                            <div className="font-mono text-[10px] tracking-widest mt-2" data-testid={`sync-status-${a.account_number}`}>
                                                {a.pending_history_sync ? (
                                                    <span className="text-[#00BFFF]">BROKER SYNC PENDING · EA picks it up on next poll (requires EA v1.39+){a.pending_history_sync.requested_by === "auto_heal" ? " · auto-heal" : ""}</span>
                                                ) : (
                                                    <span className="text-[#52525B]">LAST BROKER SYNC · {new Date(a.last_full_sync_at).toLocaleString()}{a.last_full_sync ? ` · ${a.last_full_sync.deals_pushed} deals · ${a.last_full_sync.trades_repaired} repaired` : ""}</span>
                                                )}
                                            </div>
                                        )}
                                    </div>

                                    {testResults[a.id] && (
                                        <ConnectionTestResult result={testResults[a.id]} onDismiss={() =>
                                            setTestResults(prev => { const n = { ...prev }; delete n[a.id]; return n; })} />
                                    )}

                                    {a.mode !== "paper" && (
                                        <CredentialsPanel account={a} onUpdate={load} onError={(e) => setErr(e)} onMessage={(m) => setMsg(m)} />
                                    )}

                                    {a.mode !== "paper" && (
                                        <div className="mt-4">
                                            <QuickInstallPanel accountId={a.id} accountLabel={a.label} account={a} onTrusted={load} />
                                            <TrustedTerminals accountId={a.id} refreshKey={a.verified_identity ? 1 : 0} />
                                        </div>
                                    )}

                                    <AccountCertification accountId={a.id} />
                                </div>
                            );
                        })}
                    </div>
                )}

                <PartnerBrokerCard />
            </div>

            {importAccount && (
                <ImportPositionsModal
                    account={importAccount}
                    onClose={() => setImportAccount(null)}
                    onDone={(n) => {
                        setImportAccount(null);
                        if (n > 0) toast.success(`Imported · ${n} position${n === 1 ? "" : "s"} now visible in Trades`);
                    }}
                />
            )}
        </AppLayout>
    );
}


function ImportPositionsModal({ account, onClose, onDone }) {
    // CSV-style textarea so the user can paste rows straight from MT5's Trade tab.
    // One position per line · TICKET,SYMBOL,TYPE,VOLUME,PRICE_OPEN[,SL,TP]
    const [raw, setRaw] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");

    const parse = () => {
        const out = [];
        for (const line of raw.split("\n").map(l => l.trim()).filter(Boolean)) {
            // Accept tab- or comma-separated. Strip $ signs, whitespace.
            const parts = line.split(/[\t,]/).map(p => p.trim());
            if (parts.length < 5) {
                throw new Error(`Line "${line.slice(0, 40)}" needs at least 5 columns (ticket, symbol, type, volume, price_open)`);
            }
            const [ticket, symbol, type, volume, priceOpen, sl, tp] = parts;
            const t = type.toUpperCase();
            if (t !== "BUY" && t !== "SELL") {
                throw new Error(`Invalid type "${type}" — must be BUY or SELL`);
            }
            out.push({
                ticket: parseInt(ticket, 10),
                symbol: symbol.toUpperCase().replace(/[^A-Z0-9._]/g, ""),
                type: t,
                volume: parseFloat(volume),
                price_open: parseFloat(priceOpen),
                sl: sl ? parseFloat(sl) : 0,
                tp: tp ? parseFloat(tp) : 0,
            });
        }
        return out;
    };

    const submit = async () => {
        setBusy(true); setErr("");
        try {
            const positions = parse();
            if (positions.length === 0) {
                setErr("Paste at least one position row.");
                setBusy(false);
                return;
            }
            const { data } = await api.post(`/accounts/${account.id}/import-positions`, { positions });
            onDone(data.created);
        } catch (e) {
            setErr(formatApiError(e));
        } finally { setBusy(false); }
    };

    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="import-positions-modal"
            onClick={() => !busy && onClose()}>
            <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-xl w-full p-6 space-y-4 max-h-[85vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center gap-2">
                    <Download className="w-5 h-5 text-[#FFD700]" />
                    <h2 className="font-display font-bold text-lg tracking-tight">Import positions · {account.label}</h2>
                </div>
                <p className="text-xs text-[#A1A1AA] leading-relaxed">
                    Open MT5 → <span className="text-white">Toolbox → Trade tab</span>. Right-click → <span className="text-white">Copy as CSV</span>, OR type one position per line:
                </p>
                <pre className="font-mono text-[10px] bg-[#050505] border border-[#1F1F1F] p-2 text-[#A1A1AA] whitespace-pre">
TICKET, SYMBOL, BUY|SELL, VOLUME, PRICE_OPEN[, SL, TP]
e.g.
799001, XAUUSD, BUY,  0.50, 4050.10, 4030.00, 4100.00
799002, XAUUSD, SELL, 0.30, 4080.50
799003, BTCUSD, BUY,  0.01, 62000
                </pre>
                <textarea value={raw} onChange={e => setRaw(e.target.value)}
                    data-testid="import-positions-textarea"
                    rows={8}
                    autoFocus
                    placeholder="Paste your MT5 positions here, one per line…"
                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-xs font-mono outline-none" />
                {err && <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}
                <p className="font-mono text-[10px] text-[#52525B] tracking-widest leading-relaxed">
                    DUPLICATES ARE IGNORED · TICKETS ALREADY TRACKED IN STOIC WILL BE SKIPPED. ONCE YOU INSTALL EA v1.25 THIS IS AUTOMATIC.
                </p>
                <div className="flex items-center justify-end gap-2 pt-1">
                    <button onClick={onClose} disabled={busy}
                        data-testid="import-positions-cancel"
                        className="px-4 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#333333] text-[#A1A1AA]">
                        CANCEL
                    </button>
                    <button onClick={submit} disabled={busy || !raw.trim()}
                        data-testid="import-positions-submit"
                        className="px-4 py-2 text-xs font-mono tracking-widest bg-[#FFD700] hover:bg-[#FFE033] disabled:opacity-50 text-black flex items-center gap-1.5">
                        <Download className="w-3 h-3" /> {busy ? "IMPORTING…" : "IMPORT"}
                    </button>
                </div>
            </div>
        </div>
    );
}

function CredentialsPanel({ account, onUpdate, onError, onMessage }) {
    const [editing, setEditing] = useState(false);
    const [investor, setInvestor] = useState("");
    const [master, setMaster] = useState("");
    const [revealed, setRevealed] = useState(null);
    const [saving, setSaving] = useState(false);
    const [revealPrompt, setRevealPrompt] = useState(null); // { kind } | null
    const [pwInput, setPwInput] = useState("");
    const [includeMaster, setIncludeMaster] = useState(false);
    const [revealing, setRevealing] = useState(false);

    const hasAny = account.has_investor_password || account.has_master_password;

    const openRevealPrompt = (kind) => {
        // If already revealed, toggle off
        if (revealed) {
            setRevealed(null);
            return;
        }
        setPwInput("");
        setIncludeMaster(kind === "master");
        setRevealPrompt({ kind });
    };

    const submitReveal = async () => {
        if (!pwInput) {
            onError("Enter your account password to continue.");
            return;
        }
        setRevealing(true);
        try {
            const { data } = await api.post(
                `/accounts/${account.id}/credentials/reveal`,
                { password: pwInput, include_master: includeMaster },
            );
            setRevealed(data);
            setRevealPrompt(null);
            setPwInput("");
            setTimeout(() => setRevealed(null), 30_000); // auto-hide after 30s
        } catch (e) { onError(formatApiError(e)); }
        finally { setRevealing(false); }
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
                                        <button onClick={() => openRevealPrompt(kind)} data-testid={`reveal-${kind}-${account.account_number}`}
                                            className="p-1.5 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]" title={value ? "Hide" : "Reveal (requires password)"}>
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

            {revealPrompt && (
                <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4"
                     data-testid={`reveal-modal-${account.account_number}`}
                     onClick={() => !revealing && setRevealPrompt(null)}>
                    <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-md w-full"
                         onClick={(e) => e.stopPropagation()}>
                        <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center gap-2">
                            <Lock className="w-4 h-4 text-[#FFD700]" />
                            <div>
                                <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">CONFIRM TO REVEAL CREDENTIALS</div>
                                <div className="text-xs text-[#A1A1AA] mt-1">Every reveal is audit-logged with your user, account, and timestamp.</div>
                            </div>
                        </div>
                        <div className="px-5 py-4 space-y-3">
                            <div>
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1.5">YOUR ACCOUNT PASSWORD</div>
                                <input type="password" autoFocus
                                       value={pwInput}
                                       onChange={(e) => setPwInput(e.target.value)}
                                       onKeyDown={(e) => e.key === "Enter" && submitReveal()}
                                       data-testid={`reveal-pw-input-${account.account_number}`}
                                       className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none"
                                       placeholder="•••••••••" />
                            </div>
                            {account.has_master_password && (
                                <label className="flex items-center gap-2 text-xs text-[#A1A1AA] cursor-pointer">
                                    <input type="checkbox"
                                           checked={includeMaster}
                                           onChange={(e) => setIncludeMaster(e.target.checked)}
                                           data-testid={`reveal-include-master-${account.account_number}`}
                                           className="accent-[#FFD700]" />
                                    Also reveal <span className="text-[#FF3B30] font-mono">master password</span> (highest-risk)
                                </label>
                            )}
                        </div>
                        <div className="px-5 py-3 border-t border-[#1F1F1F] flex justify-end gap-2">
                            <button onClick={() => setRevealPrompt(null)} disabled={revealing}
                                    className="px-3 py-1.5 border border-[#1F1F1F] text-[10px] font-mono tracking-widest disabled:opacity-40">CANCEL</button>
                            <button onClick={submitReveal} disabled={revealing || !pwInput}
                                    data-testid={`reveal-submit-${account.account_number}`}
                                    className="px-3 py-1.5 bg-[#FFD700] text-black font-bold text-[10px] tracking-widest disabled:opacity-40">
                                {revealing ? "REVEALING…" : "REVEAL"}
                            </button>
                        </div>
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


// ---------------------------------------------------------------------------
// MT5 Connection Guide — step-by-step walkthrough shown on the Accounts page.
// ---------------------------------------------------------------------------
function MT5ConnectionGuide() {
    const [open, setOpen] = useState(true);
    const [troubleOpen, setTroubleOpen] = useState(false);
    const serverUrl = API.replace(/\/api\/?$/, "");  // strip trailing /api
    const [copied, setCopied] = useState("");

    const copy = (text, label) => {
        navigator.clipboard.writeText(text);
        setCopied(label);
        setTimeout(() => setCopied(""), 1500);
    };

    return (
        <div className="border border-[#FFB000]/30 bg-[#0A0A0A]" data-testid="mt5-connection-guide">
            <button onClick={() => setOpen(!open)} className="w-full flex items-center justify-between px-5 py-3 border-b border-[#1F1F1F] hover:bg-[#101010] transition-colors"
                data-testid="mt5-guide-toggle">
                <div className="flex items-center gap-3">
                    <Info className="w-4 h-4 text-[#FFB000]" />
                    <div className="text-left">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SETUP — 5 MIN</div>
                        <div className="font-display font-bold text-base tracking-tight">Connect an MT5 broker account to STOIC</div>
                    </div>
                </div>
                <ChevronDown className={`w-4 h-4 text-[#A1A1AA] transition-transform ${open ? "rotate-180" : ""}`} />
            </button>
            {open && (
                <div className="p-5 space-y-5">
                    <Step n={1} title="Click ADD ACCOUNT (top-right)" testid="conn-step-1">
                        <p>Pick <strong className="text-white">paper</strong> mode for sandboxing or <strong className="text-white">live</strong> for real trading. Choose a broker preset (RoboForex, Exness, IC Markets, Pepperstone, FXTM…) and STOIC auto-fills the server name. Enter your MT5 login number, account label, and click <strong>CREATE</strong>.</p>
                        <p className="text-xs text-[#52525B] mt-2">A unique <code className="font-mono text-[#00FF41]">bridge_token</code> is generated for this account — that&apos;s what authenticates the EA to STOIC. Keep it private.</p>
                    </Step>

                    <Step n={2} title="Download the EA bridge file" testid="conn-step-2">
                        <p>Click <strong className="text-white">DOWNLOAD EA</strong> in the top-right of this page (next to ADD ACCOUNT). You&apos;ll get a file named:</p>
                        <CopyBox value="EmergentTradingBridge.mq5" onCopy={() => copy("EmergentTradingBridge.mq5", "filename")} copied={copied === "filename"} testid="conn-ea-filename" />
                    </Step>

                    <Step n={3} title="Open your MT5 Data Folder" testid="conn-step-3">
                        <p>In MetaTrader 5: <strong className="text-white">File → Open Data Folder</strong>. A file explorer window opens — navigate to:</p>
                        <CopyBox value={"MQL5\\Experts\\"} onCopy={() => copy("MQL5\\Experts\\", "path")} copied={copied === "path"} testid="conn-ea-path" />
                        <p>Drop <code className="font-mono text-[#00FF41]">EmergentTradingBridge.mq5</code> into that folder. <span className="text-[#52525B]">(Typical full path: <code>{"C:\\Users\\YOU\\AppData\\Roaming\\MetaQuotes\\Terminal\\<ID>\\MQL5\\Experts\\"}</code>)</span></p>
                    </Step>

                    <Step n={4} title="Whitelist the STOIC server URL in MT5" testid="conn-step-4">
                        <p>Still in MT5: <strong className="text-white">Tools → Options → Expert Advisors</strong>. Tick the box labelled <em>&quot;Allow WebRequest for listed URL&quot;</em>, then click <strong>add</strong> and paste:</p>
                        <CopyBox value={serverUrl} onCopy={() => copy(serverUrl, "url")} copied={copied === "url"} testid="conn-server-url" />
                        <p className="text-xs text-[#52525B] mt-2">Click OK to close the dialog. Without this whitelist, the EA gets <code>WebRequest error 4060</code> on every poll.</p>
                    </Step>

                    <Step n={5} title="Compile and attach the EA" testid="conn-step-5">
                        <p>Back in MT5, press <strong className="text-white">Ctrl + N</strong> (or right-click in the Navigator → Refresh). You should now see <code className="font-mono text-[#00FF41]">EmergentTradingBridge</code> under <em>Expert Advisors</em>.</p>
                        <p className="mt-2">Drag it onto <strong className="text-white">any chart</strong> (the symbol doesn&apos;t matter — the EA tracks XAUUSD/BTCUSD regardless). A configuration dialog opens with three inputs:</p>
                        <ul className="font-mono text-xs space-y-1 mt-2 ml-2 text-[#A1A1AA]">
                            <li>• <code className="text-[#00FF41]">ServerUrl</code> → paste the URL from step 4</li>
                            <li>• <code className="text-[#00FF41]">BridgeToken</code> → copy it from this page&apos;s account row (the green token field)</li>
                            <li>• <code className="text-[#00FF41]">PollSeconds</code> → leave at 5 (recommended)</li>
                        </ul>
                        <p className="mt-2">Confirm <strong>AutoTrading is ON</strong> (top-toolbar button is green, says <em>&quot;Algo Trading&quot;</em>), then click OK.</p>
                    </Step>

                    <Step n={6} title="Verify the connection" testid="conn-step-6">
                        <p>Within 5 seconds, the account row below should switch to a green <span className="text-[#00FF41]">● CONNECTED</span> dot, and the EA prints in MT5&apos;s <em>Experts</em> tab:</p>
                        <pre className="bg-[#050505] border border-[#1F1F1F] px-3 py-2 font-mono text-[11px] text-[#00FF41] overflow-x-auto">
{`Heartbeat OK — balance: 10000.00  equity: 10000.00
Polling /api/bridge/poll every 5s`}
                        </pre>
                        <p className="text-xs text-[#52525B] mt-2">You&apos;re live. The EA now polls STOIC every 5 seconds for new trades, executes them via MT5 <code>OrderSend()</code>, and reports fills back. To start auto-trading, go to <a href="/bot" className="text-[#00FF41] hover:underline">Bot Config</a> and click START BOT.</p>
                    </Step>

                    {/* Troubleshooting */}
                    <button onClick={() => setTroubleOpen(!troubleOpen)}
                        className="w-full flex items-center justify-between px-3 py-2 border border-[#1F1F1F] hover:border-[#FF3B30]/40 transition-colors mt-4"
                        data-testid="mt5-troubleshoot-toggle">
                        <div className="flex items-center gap-2">
                            <AlertTriangle className="w-3.5 h-3.5 text-[#FF3B30]" />
                            <span className="font-display font-bold text-xs tracking-tight">Troubleshooting · common issues</span>
                        </div>
                        <ChevronDown className={`w-3.5 h-3.5 text-[#A1A1AA] transition-transform ${troubleOpen ? "rotate-180" : ""}`} />
                    </button>
                    {troubleOpen && (
                        <div className="border border-[#1F1F1F] divide-y divide-[#1F1F1F]" data-testid="mt5-troubleshoot-list">
                            {TROUBLE.map(t => (
                                <div key={t.code} className="px-4 py-3" data-testid={`trouble-${t.code}`}>
                                    <div className="flex items-center gap-2 mb-1">
                                        <span className="font-mono text-[10px] px-2 py-0.5 bg-[#FF3B30]/10 text-[#FF3B30] border border-[#FF3B30]/40 tracking-widest">{t.code}</span>
                                        <span className="font-display font-bold text-xs">{t.symptom}</span>
                                    </div>
                                    <p className="text-xs text-[#A1A1AA] leading-relaxed">{t.fix}</p>
                                </div>
                            ))}
                        </div>
                    )}

                    <div className="flex items-center gap-2 pt-2 border-t border-[#1F1F1F]">
                        <a href="/guide#setup" className="text-xs text-[#00FF41] hover:underline flex items-center gap-1" data-testid="conn-full-guide-link">
                            <ExternalLink className="w-3 h-3" /> Full setup tutorial in the Guide
                        </a>
                    </div>
                </div>
            )}
        </div>
    );
}

const TROUBLE = [
    { code: "4060",
      symptom: "WebRequest error 4060 in the Experts tab",
      fix: "The STOIC server URL is not whitelisted. Go to Tools → Options → Expert Advisors → tick \"Allow WebRequest for listed URL\" and paste the URL exactly (no trailing slash). Click OK and the EA will recover on the next poll." },
    { code: "TOKEN",
      symptom: "EA prints \"Invalid bridge_token\" or \"Account not found\"",
      fix: "The token in the EA inputs doesn't match the one on this page. Click the copy icon next to the green token field above, then in MT5 right-click the EA → Properties → Inputs tab → paste over BridgeToken → OK." },
    { code: "AUTO",
      symptom: "EA attached but no trades execute",
      fix: "AutoTrading is OFF. The top-toolbar \"Algo Trading\" button must be green. If it's red/grey, click it once. Also confirm the EA smiley face on the chart's top-right corner is happy (not sad)." },
    { code: "POLL",
      symptom: "Account heartbeat goes stale (●CONNECTED dot turns grey)",
      fix: "Either your MT5 terminal closed or lost internet. The EA polls every 5s — if no heartbeat for 60s the dot drops. Re-attach the EA or restart MT5. For 24/7 autopilot, run MT5 on a VPS (~$5–15/mo)." },
    { code: "SPREAD",
      symptom: "Trades open then immediately close with \"slippage too wide\"",
      fix: "Your broker's spread on XAU/BTC is too high at trade time. Microcent / ECN accounts have tighter spreads — check your broker tier. Or widen Bot Config → Risk → max-slippage cap." },
    { code: "PERM",
      symptom: "EA prints \"DLL imports not allowed\" or \"No permission to trade\"",
      fix: "On the EA's first attach, MT5 asks for permissions. Right-click EA → Properties → Common tab → tick \"Allow algo trading\" and \"Allow DLL imports\" → OK." },
];

function Step({ n, title, children, testid }) {
    return (
        <div className="flex gap-4" data-testid={testid}>
            <div className="shrink-0 w-8 h-8 rounded-full bg-[#00FF41]/10 border border-[#00FF41]/40 flex items-center justify-center font-mono text-xs text-[#00FF41]">{n}</div>
            <div className="flex-1 space-y-1">
                <div className="font-display font-bold text-sm tracking-tight">{title}</div>
                <div className="text-xs text-[#A1A1AA] leading-relaxed space-y-2">{children}</div>
            </div>
        </div>
    );
}

function CopyBox({ value, onCopy, copied, testid }) {
    return (
        <div className="flex items-center gap-2 my-2">
            <code className="flex-1 font-mono text-xs px-3 py-2 bg-[#050505] border border-[#1F1F1F] text-[#00FF41] break-all" data-testid={testid}>{value}</code>
            <button onClick={onCopy} className="px-3 py-2 border border-[#1F1F1F] hover:border-[#00FF41]/40 transition-colors" data-testid={`${testid}-copy`}>
                {copied
                    ? <CheckCircle2 className="w-3.5 h-3.5 text-[#00FF41]" />
                    : <Copy className="w-3.5 h-3.5 text-[#A1A1AA]" />}
            </button>
        </div>
    );
}

function ConnectionTestResult({ result, onDismiss }) {
    const sev = result?.diagnostic?.severity || "error";
    const palette = {
        ok:    { bd: "border-[#00FF41]/40", bg: "bg-[#00FF41]/5",  fg: "text-[#00FF41]", icon: CheckCircle2, label: "CONNECTED" },
        warn:  { bd: "border-[#FFB000]/40", bg: "bg-[#FFB000]/5",  fg: "text-[#FFB000]", icon: AlertTriangle, label: "STALE"     },
        error: { bd: "border-[#FF3B30]/40", bg: "bg-[#FF3B30]/5",  fg: "text-[#FF3B30]", icon: AlertTriangle, label: "OFFLINE"   },
    }[sev] || { bd: "border-[#1F1F1F]", bg: "bg-[#0A0A0A]", fg: "text-[#A1A1AA]", icon: Info, label: "UNKNOWN" };
    const Icon = palette.icon;
    const spreads = result?.current_spreads || {};
    const spreadEntries = Object.entries(spreads);

    return (
        <div className={`mt-4 border ${palette.bd} ${palette.bg} px-4 py-3`} data-testid="connection-test-result">
            <div className="flex items-start gap-3">
                <Icon className={`w-5 h-5 ${palette.fg} shrink-0 mt-0.5`} />
                <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2 flex-wrap mb-1">
                        <div className="flex items-center gap-2">
                            <span className={`font-mono text-[10px] tracking-widest ${palette.fg}`}>● {palette.label}</span>
                            {result.age_seconds !== null && result.age_seconds !== undefined && (
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                    LAST HB · {result.age_seconds < 60 ? `${result.age_seconds}s` : `${Math.floor(result.age_seconds / 60)}min`} AGO
                                </span>
                            )}
                            {result.checked_at && (
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                    CHECKED {new Date(result.checked_at).toLocaleTimeString("en-US", { hour12: false })}
                                </span>
                            )}
                        </div>
                        <button onClick={onDismiss} data-testid="dismiss-test-result"
                            className="font-mono text-[10px] text-[#52525B] hover:text-[#A1A1AA] tracking-widest">✕ DISMISS</button>
                    </div>
                    <div className="text-sm text-[#E4E4E7] leading-relaxed mb-2">{result.diagnostic?.message}</div>
                    {(result.balance !== undefined || spreadEntries.length > 0) && (
                        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 font-mono text-[11px] text-[#A1A1AA] mt-2 pt-2 border-t border-[#1F1F1F]">
                            {result.balance !== undefined && result.balance !== null && (
                                <span>BAL <span className="text-white tabular-nums">{result.balance.toFixed(2)}</span></span>
                            )}
                            {result.equity !== undefined && result.equity !== null && (
                                <span>EQ <span className="text-white tabular-nums">{result.equity.toFixed(2)}</span></span>
                            )}
                            {spreadEntries.map(([sym, sp]) => (
                                <span key={sym} data-testid={`test-spread-${sym}`}>
                                    {sym} SPR <span className="text-white tabular-nums">{Number(sp).toFixed(1)}p</span>
                                </span>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}


