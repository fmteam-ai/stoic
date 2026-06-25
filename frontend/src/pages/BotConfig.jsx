import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Play, Pause, Save as FloppyDisk, Plus, X, AlertTriangle, Shield, TrendingUp, Scissors, OctagonAlert, Gauge, Activity, Snowflake, CalendarClock, MoonStar, Timer, Megaphone, CheckCircle2, Flame, Crosshair, Zap, Rocket, Scale, Sparkles, Trash2, Bookmark, Layers, RotateCcw, Eye, Bitcoin } from "lucide-react";
import { toast } from "sonner";

const RISK_DESCRIPTIONS = {
    low: "Capital preservation. Smaller positions, tighter stops, only high-conviction setups.",
    medium: "Balanced approach. Moderate position sizing with prudent stop placement.",
    high: "Aggressive growth. Larger positions, wider targets, accepts lower confidence trades.",
    extreme: "Maximum risk. Largest size, lowest threshold, suitable only for microcent or experimental accounts.",
};

// Visual multiplier badge mapped to backend risk_pct
const RISK_MULTIPLIER = {
    low:     "×0.5",
    medium:  "×1.0",
    high:    "×2.5",
    extreme: "×5.0",
};

// Preset key → lucide-react icon mapping (kept in sync with strategy_presets.py)
const PRESET_ICONS = {
    Crosshair, Zap, TrendingUp, Rocket, Activity, Flame, Scale,
};

export default function BotConfig() {
    const [cfg, setCfg] = useState(null);
    const [profiles, setProfiles] = useState({});
    const [supported, setSupported] = useState([]);
    const [presets, setPresets] = useState([]);
    const [customPresets, setCustomPresets] = useState([]);
    // Account selector — null = the user's default profile; otherwise a specific account_id
    const [accounts, setAccounts] = useState([]);
    const [selectedAccountId, setSelectedAccountId] = useState(null);
    const [allConfigs, setAllConfigs] = useState([]);   // index of configs the user owns
    const [resetting, setResetting] = useState(false);
    const [applyingPreset, setApplyingPreset] = useState(null);
    const [showSaveModal, setShowSaveModal] = useState(false);
    const [presetForm, setPresetForm] = useState({ name: "", description: "" });
    const [savingPreset, setSavingPreset] = useState(false);
    const [saving, setSaving] = useState(false);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");
    const [saveMsg, setSaveMsg] = useState("");
    const [newSym, setNewSym] = useState("");

    const accountQuery = selectedAccountId ? `?account_id=${selectedAccountId}` : "";

    const load = useCallback(async () => {
        try {
            const [c, p, s, pr, ac, cfgs] = await Promise.all([
                api.get(`/bot/config${accountQuery}`),
                api.get("/market/risk-profiles"),
                api.get("/market/symbols"),
                api.get("/bot/presets"),
                api.get("/accounts"),
                api.get("/bot/configs"),
            ]);
            setCfg(c.data); setProfiles(p.data.profiles); setSupported(s.data.symbols);
            setPresets(pr.data.presets || []);
            setCustomPresets(pr.data.custom || []);
            setAccounts(ac.data || []);
            setAllConfigs(cfgs.data || []);
        } catch (e) { setErr(formatApiError(e)); }
    }, [accountQuery]);

    useEffect(() => { load(); }, [load]);

    // Auto-dismiss the "Configuration saved." toast 3s after it appears
    useEffect(() => {
        if (!saveMsg) return;
        const t = setTimeout(() => setSaveMsg(""), 3000);
        return () => clearTimeout(t);
    }, [saveMsg]);

    const applyPreset = async (key, label) => {
        setApplyingPreset(key); setErr("");
        try {
            const { data } = await api.post(`/bot/preset/${key}${accountQuery}`);
            setCfg(data.config);
            toast.success(`Preset applied · ${label}`, {
                description: "Behaviour knobs updated. Click SAVE CONFIGURATION to persist.",
            });
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setApplyingPreset(null);
        }
    };

    const saveCustomPreset = async () => {
        if (!presetForm.name.trim()) {
            toast.error("Preset name required");
            return;
        }
        setSavingPreset(true);
        try {
            await api.post(`/bot/my-presets${accountQuery}`, presetForm);
            toast.success(`Saved · "${presetForm.name}"`, {
                description: "Find it under 'Your Presets' below the built-ins.",
            });
            setShowSaveModal(false);
            setPresetForm({ name: "", description: "" });
            await load();   // re-fetch presets so the new one shows up
        } catch (e) {
            toast.error("Save failed", { description: formatApiError(e) });
        } finally {
            setSavingPreset(false);
        }
    };

    const deleteCustomPreset = async (id, name) => {
        if (!window.confirm(`Delete preset "${name}"?`)) return;
        try {
            await api.delete(`/bot/my-presets/${id}`);
            toast.success(`Deleted · "${name}"`);
            await load();
        } catch (e) {
            toast.error("Delete failed", { description: formatApiError(e) });
        }
    };

    const save = async () => {
        setSaving(true); setErr(""); setSaveMsg("");
        try {
            const { data } = await api.put(`/bot/config${accountQuery}`, {
                risk_level: cfg.risk_level,
                symbols: cfg.symbols,
                active: cfg.active,
                max_concurrent_trades: cfg.max_concurrent_trades,
                max_lot_size: cfg.max_lot_size || 0,
                auto_execute: cfg.auto_execute,
                breakeven_enabled: cfg.breakeven_enabled,
                breakeven_trigger_r: cfg.breakeven_trigger_r,
                partial_close_enabled: cfg.partial_close_enabled,
                partial_close_trigger_r: cfg.partial_close_trigger_r,
                partial_close_fraction: cfg.partial_close_fraction,
                trailing_enabled: cfg.trailing_enabled,
                trailing_start_r: cfg.trailing_start_r,
                trailing_distance_r: cfg.trailing_distance_r,
                daily_drawdown_pct: cfg.daily_drawdown_pct,
                daily_drawdown_enabled: cfg.daily_drawdown_enabled,
                weekly_drawdown_pct: cfg.weekly_drawdown_pct,
                weekly_drawdown_enabled: cfg.weekly_drawdown_enabled,
                spread_filter_enabled: cfg.spread_filter_enabled,
                max_spread_pips: cfg.max_spread_pips || {},
                auto_tune_enabled: cfg.auto_tune_enabled,
                slippage_veto_enabled: cfg.slippage_veto_enabled,
                max_slippage_pips: cfg.max_slippage_pips || {},
                anti_tilt_enabled: cfg.anti_tilt_enabled,
                anti_tilt_consecutive_losses: cfg.anti_tilt_consecutive_losses,
                anti_tilt_freeze_hours: cfg.anti_tilt_freeze_hours,
                trade_of_day_cap: cfg.trade_of_day_cap,
                asia_session_skip_xau: cfg.asia_session_skip_xau,
                sl_cooldown_enabled: cfg.sl_cooldown_enabled,
                sl_cooldown_minutes: cfg.sl_cooldown_minutes,
                pre_news_protect_enabled: cfg.pre_news_protect_enabled,
                pre_news_protect_minutes: cfg.pre_news_protect_minutes,
                aggressive_mode: cfg.aggressive_mode,
                min_confidence_override: cfg.min_confidence_override,
                paper_shadow_mode: cfg.paper_shadow_mode,
                crypto_risk_pct_per_trade: cfg.crypto_risk_pct_per_trade,
            });
            setCfg(data); setSaveMsg("Configuration saved.");
            // refresh allConfigs index so the selector reflects new state
            try { const c = await api.get("/bot/configs"); setAllConfigs(c.data || []); } catch (_e) { /* non-fatal */ }
        } catch (e) { setErr(formatApiError(e)); }
        finally { setSaving(false); }
    };

    const toggleBot = async () => {
        setErr(""); setMsg("");
        try {
            const ep = cfg.active ? "/bot/stop" : "/bot/start";
            await api.post(`${ep}${accountQuery}`);
            setCfg({ ...cfg, active: !cfg.active });
            setMsg(cfg.active ? "Bot stopped." : "Bot started.");
            try { const c = await api.get("/bot/configs"); setAllConfigs(c.data || []); } catch (_e) { /* non-fatal */ }
        } catch (e) { setErr(formatApiError(e)); }
    };

    const resetAccountConfig = async () => {
        if (!selectedAccountId) return;
        const accLabel = accounts.find(a => a.id === selectedAccountId)?.label || "this account";
        if (!window.confirm(`Reset bot config for "${accLabel}" to the default profile?\n\nThis deletes the per-account override. The account will trade using your DEFAULT settings.`)) return;
        setResetting(true); setErr("");
        try {
            await api.delete(`/bot/config?account_id=${selectedAccountId}`);
            toast.success("Per-account override removed", { description: "This account now uses the default profile." });
            setSelectedAccountId(null);  // jump back to default view
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setResetting(false);
        }
    };

    const triggerPanic = async () => {
        if (!window.confirm("PANIC LOCK\n\nThis will:\n• Stop your bot\n• Cancel all pending trades\n• Request close on all open trades\n\nProceed?")) return;
        setErr(""); setMsg("");
        try {
            const { data } = await api.post("/panic");
            setMsg(`PANIC LOCK engaged · ${data.bots_disabled} bot(s) disabled · ${data.trades_cancelled} pending cancelled · ${data.open_trades_marked_for_close} open marked-for-close.`);
            await load();
        } catch (e) { setErr(formatApiError(e)); }
    };

    const addSymbol = () => {
        const sym = newSym.trim().toUpperCase();
        if (!sym || cfg.symbols.includes(sym)) return;
        setCfg({ ...cfg, symbols: [...cfg.symbols, sym] });
        setNewSym("");
    };

    const removeSymbol = (s) => setCfg({ ...cfg, symbols: cfg.symbols.filter(x => x !== s) });

    if (!cfg) return (
        <AppLayout>
            <div className="p-8 font-mono text-xs text-[#52525B] tracking-widest">LOADING CONFIG…</div>
        </AppLayout>
    );

    return (
        <AppLayout>
            <PageHeader
                title="Bot Configuration"
                subtitle="Risk profile, symbols and execution behaviour."
                testid="bot-header"
                action={
                    <div className="flex items-center gap-2">
                        <div className="font-mono text-[10px] tracking-widest text-[#52525B]">STATUS</div>
                        <div className={`font-mono text-xs px-2 py-1 border ${cfg.active ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"}`}
                            data-testid="bot-status-pill">
                            {cfg.active ? "● ACTIVE" : "○ INACTIVE"}
                        </div>
                        <button onClick={toggleBot} data-testid="bot-toggle-button"
                            className={`px-4 py-2 text-xs tracking-widest font-medium flex items-center gap-2 transition-colors duration-150 ${
                                cfg.active ? "bg-[#FF3B30] hover:bg-[#E53527] text-white" : "bg-[#00FF41] hover:bg-[#00E53A] text-black"
                            }`}>
                            {cfg.active ? <><Pause className="w-3.5 h-3.5" /> STOP BOT</> : <><Play className="w-3.5 h-3.5" /> START BOT</>}
                        </button>
                        <button onClick={triggerPanic} data-testid="panic-button"
                            title="Stops bot, cancels pending trades, and requests close on all open positions"
                            className="px-3 py-2 text-xs tracking-widest font-medium flex items-center gap-2 border border-[#FF3B30]/50 text-[#FF3B30] hover:bg-[#FF3B30]/10 transition-colors duration-150">
                            <AlertTriangle className="w-3.5 h-3.5" /> PANIC
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-6 max-w-4xl">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}
                {msg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-4 py-2 text-xs text-[#00FF41] font-mono">{msg}</div>}

                {/* Account scope selector — switch between the user's DEFAULT bot
                    profile and per-account overrides. Each scope edits its own cfg. */}
                <AccountScopeBar
                    accounts={accounts}
                    allConfigs={allConfigs}
                    selectedAccountId={selectedAccountId}
                    onSelect={setSelectedAccountId}
                    onReset={resetAccountConfig}
                    resetting={resetting}
                />

                {/* Strategy Presets */}
                <div className="border border-[#FFD700]/30 bg-[#0A0A0A]" data-testid="strategy-presets-section">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <Sparkles className="w-4 h-4 text-[#FFD700]" />
                        <div>
                            <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">STRATEGY PRESETS · ONE-CLICK PERSONAS</div>
                            <div className="font-display font-bold text-lg tracking-tight">Pick a personality for the bot</div>
                        </div>
                    </div>
                    <div className="p-5 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="presets-grid">
                        {presets.map(p => {
                            const Icon = PRESET_ICONS[p.icon] || Sparkles;
                            const active = cfg.active_preset === p.key;
                            const isApplying = applyingPreset === p.key;
                            return (
                                <button key={p.key} onClick={() => applyPreset(p.key, p.label)}
                                    disabled={isApplying}
                                    data-testid={`preset-card-${p.key}`}
                                    className={`text-left p-4 border transition-colors duration-150 disabled:opacity-50 ${
                                        active ? "border-[#FFD700] bg-[#FFD700]/5" : "border-[#1F1F1F] hover:border-[#FFD700]/40"
                                    }`}>
                                    <div className="flex items-center justify-between mb-2">
                                        <div className="flex items-center gap-2">
                                            <Icon className="w-4 h-4" style={{ color: p.color }} />
                                            <span className="font-display font-bold text-sm tracking-tight">{p.label}</span>
                                        </div>
                                        {active && (
                                            <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700] bg-[#FFD700]/10">
                                                ACTIVE
                                            </span>
                                        )}
                                    </div>
                                    <div className="font-mono text-[10px] text-[#A1A1AA] tracking-wide mb-2">{p.tagline}</div>
                                    <p className="text-xs text-[#52525B] leading-relaxed line-clamp-3">{p.description}</p>
                                    <div className="mt-3 font-mono text-[9px] text-[#52525B] tracking-widest">
                                        {isApplying ? "APPLYING…" : "TAP TO APPLY →"}
                                    </div>
                                </button>
                            );
                        })}
                    </div>
                    <div className="px-5 py-2 border-t border-[#1F1F1F] flex items-center justify-between gap-3 flex-wrap">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                            TIP · Presets overlay behaviour knobs only. Your risk level, symbols, and drawdown limits stay untouched. Click SAVE to persist.
                        </div>
                        <button onClick={() => setShowSaveModal(true)}
                            data-testid="save-as-preset-button"
                            className="px-3 py-1.5 text-[10px] font-mono tracking-widest border border-[#FFD700]/40 text-[#FFD700] hover:bg-[#FFD700]/10 flex items-center gap-1.5 transition-colors">
                            <Bookmark className="w-3 h-3" /> SAVE CURRENT AS PRESET
                        </button>
                    </div>

                    {customPresets.length > 0 && (
                        <div className="border-t border-[#1F1F1F]" data-testid="custom-presets-section">
                            <div className="px-5 py-3 border-b border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                                YOUR PRESETS · {customPresets.length} / 10
                            </div>
                            <div className="p-5 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                                {customPresets.map(p => {
                                    const customKey = `custom:${p.id}`;
                                    const active = cfg.active_preset === customKey;
                                    const isApplying = applyingPreset === customKey;
                                    return (
                                        <div key={p.id}
                                            data-testid={`custom-preset-${p.id}`}
                                            className={`text-left p-4 border transition-colors duration-150 group ${
                                                active ? "border-[#9B59B6] bg-[#9B59B6]/5" : "border-[#1F1F1F] hover:border-[#9B59B6]/40"
                                            }`}>
                                            <div className="flex items-center justify-between mb-2">
                                                <div className="flex items-center gap-2 min-w-0">
                                                    <Bookmark className="w-4 h-4 text-[#9B59B6] shrink-0" />
                                                    <span className="font-display font-bold text-sm tracking-tight truncate">{p.name}</span>
                                                </div>
                                                <div className="flex items-center gap-1.5">
                                                    {active && (
                                                        <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#9B59B6]/40 text-[#9B59B6] bg-[#9B59B6]/10">
                                                            ACTIVE
                                                        </span>
                                                    )}
                                                    <button onClick={() => deleteCustomPreset(p.id, p.name)}
                                                        data-testid={`delete-preset-${p.id}`}
                                                        className="opacity-0 group-hover:opacity-100 text-[#52525B] hover:text-[#FF3B30] transition-opacity"
                                                        title="Delete">
                                                        <Trash2 className="w-3.5 h-3.5" />
                                                    </button>
                                                </div>
                                            </div>
                                            <p className="text-xs text-[#A1A1AA] leading-relaxed line-clamp-2 mb-3 min-h-[2.5rem]">
                                                {p.description || <span className="text-[#52525B]">No description.</span>}
                                            </p>
                                            <button onClick={() => applyPreset(customKey, p.name)}
                                                disabled={isApplying}
                                                data-testid={`apply-custom-${p.id}`}
                                                className="font-mono text-[9px] text-[#52525B] tracking-widest hover:text-[#9B59B6] disabled:opacity-50">
                                                {isApplying ? "APPLYING…" : "TAP TO APPLY →"}
                                            </button>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    )}
                </div>

                {showSaveModal && (
                    <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
                        data-testid="save-preset-modal"
                        onClick={() => !savingPreset && setShowSaveModal(false)}>
                        <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-md w-full p-6 space-y-4"
                            onClick={(e) => e.stopPropagation()}>
                            <div className="flex items-center gap-2">
                                <Bookmark className="w-5 h-5 text-[#FFD700]" />
                                <h2 className="font-display font-bold text-lg tracking-tight">Save Current Config as Preset</h2>
                            </div>
                            <p className="text-xs text-[#A1A1AA] leading-relaxed">
                                Snapshots your current behaviour knobs (confidence, trade cap, trailing, cooldowns, etc).
                                Your risk level, symbols, and drawdown limits are <span className="text-[#FFD700]">not</span> captured.
                            </p>
                            <div>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">PRESET NAME</label>
                                <input value={presetForm.name}
                                    onChange={e => setPresetForm({ ...presetForm, name: e.target.value })}
                                    data-testid="preset-name-input"
                                    maxLength={40}
                                    autoFocus
                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none"
                                    placeholder="My Sniper" />
                            </div>
                            <div>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">DESCRIPTION (OPTIONAL)</label>
                                <textarea value={presetForm.description}
                                    onChange={e => setPresetForm({ ...presetForm, description: e.target.value })}
                                    data-testid="preset-desc-input"
                                    maxLength={200}
                                    rows={3}
                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none resize-none"
                                    placeholder="Sniper with looser trailing, no SL cooldown" />
                                <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1 text-right">
                                    {presetForm.description.length}/200
                                </div>
                            </div>
                            <div className="flex items-center justify-end gap-2 pt-2">
                                <button onClick={() => setShowSaveModal(false)}
                                    disabled={savingPreset}
                                    data-testid="preset-cancel-button"
                                    className="px-4 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#333333] text-[#A1A1AA]">
                                    CANCEL
                                </button>
                                <button onClick={saveCustomPreset}
                                    disabled={savingPreset || !presetForm.name.trim()}
                                    data-testid="preset-save-button"
                                    className="px-4 py-2 text-xs font-mono tracking-widest bg-[#FFD700] hover:bg-[#FFE033] disabled:opacity-50 text-black flex items-center gap-1.5">
                                    <Bookmark className="w-3 h-3" /> {savingPreset ? "SAVING…" : "SAVE PRESET"}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Risk Profile */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 01</div>
                        <div className="font-display font-bold text-lg tracking-tight">Risk Management Profile</div>
                    </div>
                    <div className="p-5 grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="risk-grid">
                        {Object.entries(profiles).map(([key, p]) => {
                            const selected = cfg.risk_level === key;
                            return (
                                <button key={key} onClick={() => setCfg({ ...cfg, risk_level: key })}
                                    data-testid={`risk-option-${key}`}
                                    className={`text-left p-4 border transition-colors duration-150 ${
                                        selected ? "border-[#00FF41] bg-[#00FF41]/5" : "border-[#1F1F1F] hover:border-[#333333]"
                                    }`}>
                                    <div className="flex items-center justify-between mb-2">
                                        <div className="flex items-center gap-2">
                                            <div className="font-display font-bold tracking-tight">{p.label}</div>
                                            <span className="font-mono text-[10px] tracking-widest px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700] bg-[#FFD700]/10"
                                                data-testid={`risk-multiplier-${key}`}>
                                                {RISK_MULTIPLIER[key] || `×${p.risk_pct}`}
                                            </span>
                                        </div>
                                        <div className={`w-3 h-3 border-2 ${selected ? "border-[#00FF41] bg-[#00FF41]" : "border-[#333333]"}`} />
                                    </div>
                                    <p className="text-xs text-[#A1A1AA] mb-3 leading-relaxed">{RISK_DESCRIPTIONS[key]}</p>
                                    <div className="grid grid-cols-2 gap-1.5 font-mono text-[10px]">
                                        <div><span className="text-[#52525B]">RISK</span> <span className="text-white">{p.risk_pct}%</span></div>
                                        <div><span className="text-[#52525B]">CONF≥</span> <span className="text-white">{p.min_confidence}%</span></div>
                                        <div><span className="text-[#52525B]">MAX POS</span> <span className="text-white">{p.max_concurrent}</span></div>
                                        <div><span className="text-[#52525B]">LEV ≤</span> <span className="text-white">{p.leverage_cap}x</span></div>
                                    </div>
                                </button>
                            );
                        })}
                    </div>
                </div>

                {/* Symbols */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 02</div>
                        <div className="font-display font-bold text-lg tracking-tight">Active Symbols</div>
                    </div>
                    <div className="p-5 space-y-4">
                        <div className="flex flex-wrap gap-2" data-testid="symbols-list">
                            {cfg.symbols.map(s => (
                                <span key={s} className="font-mono text-xs px-2 py-1.5 bg-[#121212] border border-[#1F1F1F] flex items-center gap-2">
                                    {s}
                                    <button onClick={() => removeSymbol(s)} data-testid={`remove-symbol-${s}`} className="text-[#52525B] hover:text-[#FF3B30]">
                                        <X className="w-3 h-3" />
                                    </button>
                                </span>
                            ))}
                            {cfg.symbols.length === 0 && <span className="font-mono text-xs text-[#52525B] tracking-widest">NO SYMBOLS</span>}
                        </div>
                        <div className="flex gap-2">
                            <input value={newSym} onChange={e => setNewSym(e.target.value.toUpperCase())}
                                onKeyDown={e => e.key === "Enter" && addSymbol()}
                                data-testid="add-symbol-input"
                                list="supported-symbols"
                                className="flex-1 bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none transition-colors"
                                placeholder="e.g. EURUSD, ETHUSD" />
                            <datalist id="supported-symbols">
                                {supported.map(s => <option key={s} value={s} />)}
                            </datalist>
                            <button onClick={addSymbol} data-testid="add-symbol-button"
                                className="bg-[#121212] hover:bg-[#1F1F1F] border border-[#1F1F1F] px-4 text-xs font-mono tracking-widest flex items-center gap-1">
                                <Plus className="w-3.5 h-3.5" /> ADD
                            </button>
                        </div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SUPPORTED · {supported.join(" · ")}</div>
                    </div>
                </div>

                {/* Behaviour */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 03</div>
                        <div className="font-display font-bold text-lg tracking-tight">Execution Behaviour</div>
                    </div>
                    <div className="p-5 grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">MAX CONCURRENT TRADES</label>
                            <input type="number" min="1" max="10" value={cfg.max_concurrent_trades}
                                onChange={e => setCfg({ ...cfg, max_concurrent_trades: parseInt(e.target.value) || 1 })}
                                data-testid="max-concurrent-input"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
                        </div>
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">AUTO-EXECUTE SIGNALS</label>
                            <button onClick={() => setCfg({ ...cfg, auto_execute: !cfg.auto_execute })}
                                data-testid="auto-execute-toggle"
                                className={`w-full px-3 py-2 text-sm font-mono tracking-widest border transition-colors ${
                                    cfg.auto_execute ? "bg-[#00FF41]/10 border-[#00FF41] text-[#00FF41]" : "bg-[#0A0A0A] border-[#1F1F1F] text-[#A1A1AA]"
                                }`}>
                                {cfg.auto_execute ? "● ENABLED" : "○ DISABLED"}
                            </button>
                        </div>
                        <div className="md:col-span-2">
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2 flex items-center gap-2">
                                <Layers className="w-3 h-3 text-[#FFD700]" />
                                MAX LOT SIZE PER TRADE
                                <span className="font-mono text-[9px] text-[#52525B] normal-case tracking-normal ml-auto">
                                    Lot used at <span className="text-[#FFD700]">peak confidence</span> — scales down with signal confidence. <span className="text-[#FFD700]">0</span> = pure Kelly.
                                </span>
                            </label>
                            <input type="number" min="0" step="0.01" value={cfg.max_lot_size ?? 0}
                                onChange={e => setCfg({ ...cfg, max_lot_size: Math.max(0, parseFloat(e.target.value) || 0) })}
                                data-testid="max-lot-size-input"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none"
                                placeholder="e.g. 0.10  (0 = pure Kelly)" />
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1.5">
                                {selectedAccountId
                                    ? "APPLIES TO THIS ACCOUNT ONLY"
                                    : "APPLIES TO ANY ACCOUNT USING THE DEFAULT PROFILE"}
                                <span className="ml-2 text-[#A1A1AA]">
                                    · LOW CONFIDENCE = SMALLER LOT · MAX REACHED ONLY ON PEAK-CONVICTION SIGNALS
                                </span>
                            </div>
                        </div>
                    </div>
                </div>

                {/* Position Sizing Preview — shows the lot the bot would open
                    at various confidence levels given current settings. */}
                <PositionSizingPreview accountId={selectedAccountId} cfg={cfg} />

                {/* Section 04 — Profit Protection Suite */}
                <ProfitProtectionSection cfg={cfg} setCfg={setCfg} />

                {/* Section 05 — Trading Intelligence */}
                <TradingIntelligenceSection cfg={cfg} setCfg={setCfg} />

                {/* Section 06 — Capital Preservation Guards */}
                <CapitalGuardsSection cfg={cfg} setCfg={setCfg} />

                <div className="flex items-center justify-end gap-3 flex-wrap" data-testid="bot-save-row">
                    {saveMsg && (
                        <div className="flex items-center gap-2 border border-[#00FF41]/30 bg-[#00FF41]/10 px-3 py-2 text-xs text-[#00FF41] font-mono"
                            data-testid="bot-save-message">
                            <CheckCircle2 className="w-3.5 h-3.5" /> {saveMsg}
                        </div>
                    )}
                    <button onClick={save} disabled={saving}
                        data-testid="bot-save-button"
                        className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium px-6 py-2.5 text-xs tracking-widest flex items-center gap-2 transition-colors">
                        <FloppyDisk className="w-3.5 h-3.5" /> {saving ? "SAVING…" : "SAVE CONFIGURATION"}
                    </button>
                </div>
            </div>
        </AppLayout>
    );
}

function CapitalGuardsSection({ cfg, setCfg }) {
    return (
        <div className="border border-[#FF3B30]/30 bg-[#0A0A0A]" data-testid="capital-guards-section">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Snowflake className="w-4 h-4 text-[#FF3B30]" />
                <div>
                    <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest">SECTION 06 · CAPITAL PRESERVATION</div>
                    <div className="font-display font-bold text-lg tracking-tight">Anti-tilt · trade caps · session filters</div>
                </div>
            </div>
            <div className="p-5 space-y-4">
                {/* Anti-tilt */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="anti_tilt_enabled" label="Anti-Tilt Freeze" icon={Snowflake} color="#FF3B30"
                        desc="Auto-pause bot execution after N consecutive losing trades. Prevents revenge-trading and over-leveraging in chop. Bot resumes after the freeze window expires." />
                    {cfg.anti_tilt_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="anti_tilt_consecutive_losses" label="CONSECUTIVE LOSSES" suffix="trades" step={1} min={1} max={10} />
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="anti_tilt_freeze_hours" label="FREEZE WINDOW" suffix="hours" step={1} min={1} max={48} />
                        </div>
                    )}
                </div>

                {/* Trade-of-day cap */}
                <div>
                    <div className="border border-[#1F1F1F] p-4 bg-[#050505]">
                        <div className="flex items-center gap-2 mb-2">
                            <CalendarClock className="w-4 h-4 text-[#FFD700]" />
                            <span className="font-display font-bold text-sm">Trade-of-the-Day Cap</span>
                        </div>
                        <p className="text-xs text-[#A1A1AA] leading-relaxed mb-3">
                            Max new trades per symbol per UTC day. Forces selectivity — bot waits for the A-grade
                            setup instead of churning. Set to <span className="text-[#52525B]">0</span> for unlimited.
                        </p>
                        <PPNumInput cfg={cfg} setCfg={setCfg} field="trade_of_day_cap" label="MAX TRADES PER SYMBOL / DAY" suffix="trades" step={1} min={0} max={20} />
                    </div>
                </div>

                {/* Asia-session skip XAU */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="asia_session_skip_xau" label="Skip Asia-Session Gold (XAU)" icon={MoonStar} color="#FFD700"
                        desc="XAUUSD chops sideways during the Asia session (00:00–07:00 UTC) with razor-thin moves and wide spreads. Toggle ON to skip new XAU entries during this window." />
                </div>

                {/* SL Cooldown */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="sl_cooldown_enabled" label="Post-SL Cooldown" icon={Timer} color="#FF3B30"
                        desc="If a trade was just stopped out, block new entries on the same symbol for N minutes. Prevents revenge-regime re-entry into the same losing setup." />
                    {cfg.sl_cooldown_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="sl_cooldown_minutes" label="COOLDOWN WINDOW" suffix="minutes" step={5} min={5} max={240} />
                        </div>
                    )}
                </div>

                {/* Pre-news Position Protector */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="pre_news_protect_enabled" label="Pre-News Position Protector" icon={Megaphone} color="#FFB000"
                        desc="Automatically flatten OPEN trades right before a high-impact macro event (NFP / CPI / FOMC). Veto #2 already blocks NEW entries — this protects positions already in flight." />
                    {cfg.pre_news_protect_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="pre_news_protect_minutes" label="FLATTEN WINDOW BEFORE EVENT" suffix="minutes" step={1} min={1} max={30} />
                        </div>
                    )}
                </div>

                {/* Aggressive Mode */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="aggressive_mode" label="Aggressive Mode" icon={Flame} color="#FF6B00"
                        desc="When Claude returns HOLD with any non-zero conviction AND the macro window is open, infer a direction from indicators (Kalman velocity + price-vs-MA200 + DXY bias) and convert HOLD → BUY/SELL. Increases trade frequency at the cost of per-trade edge. The full 10-layer veto cascade still runs." />
                    {cfg.aggressive_mode && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="min_confidence_override" label="MIN-CONF OVERRIDE (0 = use profile)" suffix="%" step={1} min={0} max={95} />
                        </div>
                    )}
                </div>

                {/* iter-39 — Paper-Shadow Mode */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="paper_shadow_mode" label="Paper Shadow Mode" icon={Eye} color="#06B6D4"
                        desc="Run the full signal pipeline but NEVER execute trades. Every signal is logged with origin='shadow' so you can A/B test your config against the market for weeks before flipping live. Activates even when the bot is paused. Recommended for new traders or after big config changes." />
                </div>

                {/* iter-39 — Per-account crypto risk cap */}
                <div>
                    <div className="border border-[#1F1F1F] p-4 bg-[#050505]">
                        <div className="flex items-center gap-2 mb-2">
                            <Bitcoin className="w-4 h-4 text-[#FFD700]" />
                            <span className="font-display font-bold text-sm">Crypto Risk Cap (per-account override)</span>
                        </div>
                        <p className="text-xs text-[#A1A1AA] leading-relaxed mb-3">
                            Override the global crypto-per-trade risk cap (default 0.5% of equity). Set to a tighter value (e.g. 0.1%) on volatile pairs or larger account balances. Leave at 0 to fall back to the global env default.
                        </p>
                        <div className="grid grid-cols-2 gap-3">
                            <div>
                                <label className="font-mono text-[9px] text-[#52525B] tracking-widest block mb-1">CAP % OF EQUITY (0 = use env default)</label>
                                <input type="number" step="0.05" min="0" max="5"
                                    value={cfg.crypto_risk_pct_per_trade ?? 0}
                                    onChange={(e) => {
                                        const v = parseFloat(e.target.value);
                                        setCfg({ ...cfg, crypto_risk_pct_per_trade: (isNaN(v) || v <= 0) ? null : v });
                                    }}
                                    data-testid="crypto-risk-pct-per-trade"
                                    className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}

function PPToggle({ cfg, setCfg, field, label, icon: Icon, desc, color = "#FFD700" }) {
    return (
        <div className="border border-[#1F1F1F] p-4 bg-[#050505]">
            <button onClick={() => setCfg({ ...cfg, [field]: !cfg[field] })}
                data-testid={`toggle-${field}`}
                className="w-full flex items-center justify-between gap-3 mb-2">
                <div className="flex items-center gap-2">
                    <Icon className="w-4 h-4" style={{ color: cfg[field] ? color : "#52525B" }} />
                    <span className="font-display font-bold text-sm">{label}</span>
                </div>
                <div className={`font-mono text-[10px] px-2 py-0.5 border ${
                    cfg[field] ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#52525B]"
                }`}>{cfg[field] ? "● ON" : "○ OFF"}</div>
            </button>
            <p className="text-xs text-[#A1A1AA] leading-relaxed">{desc}</p>
        </div>
    );
}

function PPNumInput({ cfg, setCfg, field, label, suffix, step = 0.1, min = 0, max = 100 }) {
    return (
        <div>
            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">{label}</label>
            <div className="flex items-center bg-[#050505] border border-[#1F1F1F] focus-within:border-[#FFD700]">
                <input type="number" step={step} min={min} max={max} value={cfg[field] ?? 0}
                    onChange={e => setCfg({ ...cfg, [field]: parseFloat(e.target.value) || 0 })}
                    data-testid={`input-${field}`}
                    className="flex-1 bg-transparent px-3 py-2 text-sm font-mono outline-none" />
                {suffix && <span className="font-mono text-[10px] text-[#52525B] tracking-widest px-2">{suffix}</span>}
            </div>
        </div>
    );
}

function ProfitProtectionSection({ cfg, setCfg }) {
    return (
        <div className="border border-[#FFD700]/30 bg-[#0A0A0A]" data-testid="profit-protection-section">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Shield className="w-4 h-4 text-[#FFD700]" />
                <div>
                    <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">SECTION 04 · PROFIT PROTECTION SUITE</div>
                    <div className="font-display font-bold text-lg tracking-tight">Lock profits · trim risk · automated</div>
                </div>
            </div>
            <div className="p-5 space-y-4">
                {/* Break-even */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="breakeven_enabled" label="Break-Even Auto-Shift" icon={Shield}
                        desc="Once a trade moves +1R in your favor, automatically shift Stop-Loss to entry price. Result: zero risk on the remaining trade." />
                    {cfg.breakeven_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="breakeven_trigger_r" label="TRIGGER (R-multiple)" suffix="R" step={0.1} min={0.5} max={5} />
                        </div>
                    )}
                </div>

                {/* Partial close */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="partial_close_enabled" label="Partial Close at TP1" icon={Scissors} color="#00FF41"
                        desc="Take 50% of the position off at the first target (1R), let the runner go for the bigger target. Locks in profit and reduces stress." />
                    {cfg.partial_close_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="partial_close_trigger_r" label="TRIGGER (R-multiple)" suffix="R" step={0.1} min={0.5} max={5} />
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="partial_close_fraction" label="CLOSE FRACTION" suffix="0-1" step={0.05} min={0.1} max={0.9} />
                        </div>
                    )}
                </div>

                {/* Trailing */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="trailing_enabled" label="Trailing Stop-Loss" icon={TrendingUp} color="#00FF41"
                        desc="After the trade moves +1.5R, SL trails behind price at 0.7R distance. Captures larger trends while locking in gains as they grow." />
                    {cfg.trailing_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="trailing_start_r" label="START TRAILING AT" suffix="R" step={0.1} min={1.0} max={10} />
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="trailing_distance_r" label="TRAIL DISTANCE" suffix="R" step={0.1} min={0.2} max={5} />
                        </div>
                    )}
                </div>

                {/* Daily drawdown */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="daily_drawdown_enabled" label="Daily Drawdown Circuit Breaker" icon={OctagonAlert} color="#FF3B30"
                        desc="Bot auto-stops if today's realised P&L drops below the threshold. Prevents the revenge-trading death spiral that kills most retail accounts." />
                    {cfg.daily_drawdown_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="daily_drawdown_pct" label="DAILY LOSS LIMIT" suffix="% of equity" step={0.5} min={0.5} max={20} />
                        </div>
                    )}
                </div>

                {/* Weekly drawdown */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="weekly_drawdown_enabled" label="Weekly Drawdown Circuit Breaker" icon={OctagonAlert} color="#FFB000"
                        desc="A 7-day rolling kill-switch that catches slow-bleed losing streaks the daily limit misses. Conservative default: 7% — bumps to 8% at medium risk, 14% at high." />
                    {cfg.weekly_drawdown_enabled && (
                        <div className="grid grid-cols-2 gap-3 mt-3">
                            <PPNumInput cfg={cfg} setCfg={setCfg} field="weekly_drawdown_pct" label="WEEKLY LOSS LIMIT" suffix="% of equity" step={0.5} min={1} max={40} />
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function TradingIntelligenceSection({ cfg, setCfg }) {
    const spreadMap = cfg.max_spread_pips || {};
    const setSpread = (sym, val) => {
        const next = { ...spreadMap, [sym]: parseFloat(val) || 0 };
        setCfg({ ...cfg, max_spread_pips: next });
    };
    return (
        <div className="border border-[#0099FF]/30 bg-[#0A0A0A]" data-testid="trading-intelligence-section">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Activity className="w-4 h-4 text-[#0099FF]" />
                <div>
                    <div className="font-mono text-[10px] text-[#0099FF] tracking-widest">SECTION 05 · TRADING INTELLIGENCE</div>
                    <div className="font-display font-bold text-lg tracking-tight">Adaptive thresholds · spread guard · trend confluence</div>
                </div>
            </div>
            <div className="p-5 space-y-4">
                {/* Auto-Tune */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="auto_tune_enabled" label="Auto-Tune Confidence Threshold" icon={Gauge} color="#0099FF"
                        desc="Raises the minimum confidence to trade based on your historical win-rate per confidence bucket. Lower-performing buckets are silently skipped. Tuned per-symbol from closed trades." />
                    <div className="mt-2 font-mono text-[10px] text-[#52525B] tracking-widest">
                        VIEW LIVE THRESHOLDS · /analytics/auto-tune
                    </div>
                </div>

                {/* Spread Filter */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="spread_filter_enabled" label="MT5 Spread Filter" icon={Activity} color="#0099FF"
                        desc="Skips auto-execution on LIVE accounts when the broker's current spread (in pips) exceeds your cap for that symbol. EA v1.21+ required — re-download from Accounts." />
                    {cfg.spread_filter_enabled && (
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3" data-testid="spread-caps">
                            {(cfg.symbols || []).map(sym => (
                                <div key={sym}>
                                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">{sym} · MAX SPREAD (PIPS)</label>
                                    <div className="flex items-center bg-[#050505] border border-[#1F1F1F] focus-within:border-[#0099FF]">
                                        <input type="number" step="0.1" min="0" value={spreadMap[sym] ?? ""}
                                            onChange={e => setSpread(sym, e.target.value)}
                                            data-testid={`spread-cap-${sym}`}
                                            placeholder="e.g. 5"
                                            className="flex-1 bg-transparent px-3 py-2 text-sm font-mono outline-none" />
                                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest px-2">PIPS</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* MTF Gate — informational (always-on) */}
                <div className="border border-[#1F1F1F] p-4 bg-[#050505]">
                    <div className="flex items-center gap-2 mb-2">
                        <TrendingUp className="w-4 h-4 text-[#0099FF]" />
                        <span className="font-display font-bold text-sm">Multi-Timeframe Trend Gate</span>
                        <div className="font-mono text-[10px] px-2 py-0.5 border border-[#0099FF]/40 text-[#0099FF] bg-[#0099FF]/10">● ALWAYS ON</div>
                    </div>
                    <p className="text-xs text-[#A1A1AA] leading-relaxed">
                        Every BUY/SELL signal is veto-checked against three trend confluence rules
                        (SMA20 slope, SMA50 vs SMA200, price vs SMA50). Counter-trend setups are
                        silently held. Look for <span className="text-[#0099FF]">mtf_gate</span> on the Signals page.
                    </p>
                </div>

                {/* Slippage Veto */}
                <div>
                    <PPToggle cfg={cfg} setCfg={setCfg} field="slippage_veto_enabled" label="Server-Side Slippage Veto" icon={OctagonAlert} color="#FF3B30"
                        desc="When the MT5 EA reports a fill, compare actual entry vs intended. If slippage > cap, the bot force-closes the position immediately. Hard guard against ECN bad-fills during news." />
                    {cfg.slippage_veto_enabled && (
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3" data-testid="slippage-caps">
                            {(cfg.symbols || []).map(sym => (
                                <div key={sym}>
                                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">{sym} · MAX SLIPPAGE (PIPS)</label>
                                    <div className="flex items-center bg-[#050505] border border-[#1F1F1F] focus-within:border-[#FF3B30]">
                                        <input type="number" step="1" min="0" value={(cfg.max_slippage_pips || {})[sym] ?? ""}
                                            onChange={e => setCfg({ ...cfg, max_slippage_pips: { ...(cfg.max_slippage_pips || {}), [sym]: parseFloat(e.target.value) || 0 } })}
                                            data-testid={`slippage-cap-${sym}`}
                                            placeholder="e.g. 20"
                                            className="flex-1 bg-transparent px-3 py-2 text-sm font-mono outline-none" />
                                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest px-2">PIPS</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}


function AccountScopeBar({ accounts, allConfigs, selectedAccountId, onSelect, onReset, resetting }) {
    // Configs keyed by account_id ("default" for null) so we can show ACTIVE / OVERRIDE badges
    // next to each option in the selector.
    const cfgByAccount = {};
    (allConfigs || []).forEach(c => {
        cfgByAccount[c.account_id || "default"] = c;
    });
    const currentKey = selectedAccountId || "default";
    const currentCfg = cfgByAccount[currentKey];
    const hasOverride = selectedAccountId
        ? Boolean(cfgByAccount[selectedAccountId])
        : true;  // default profile always exists

    return (
        <div className="border border-[#FFD700]/30 bg-[#0A0A0A]" data-testid="account-scope-bar">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Layers className="w-4 h-4 text-[#FFD700]" />
                <div className="flex-1 min-w-0">
                    <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">BOT SCOPE · WHICH ACCOUNT ARE YOU CONFIGURING?</div>
                    <div className="font-display font-bold text-lg tracking-tight">Independent bots, independent settings</div>
                </div>
                {selectedAccountId && hasOverride && (
                    <button onClick={onReset} disabled={resetting}
                        data-testid="reset-account-cfg-button"
                        title="Delete this account's override — it will inherit the default profile."
                        className="px-3 py-1.5 text-[10px] font-mono tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 disabled:opacity-50 flex items-center gap-1.5 transition-colors">
                        <RotateCcw className="w-3 h-3" /> {resetting ? "RESETTING…" : "RESET TO DEFAULT"}
                    </button>
                )}
            </div>
            <div className="p-5 space-y-3">
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2" data-testid="account-scope-options">
                    {/* Default tile */}
                    <ScopeTile
                        active={!selectedAccountId}
                        onClick={() => onSelect(null)}
                        title="Default Profile"
                        subtitle="Fallback for accounts without an override"
                        cfg={cfgByAccount["default"]}
                        testid="scope-default"
                    />
                    {(accounts || []).map(a => (
                        <ScopeTile key={a.id}
                            active={selectedAccountId === a.id}
                            onClick={() => onSelect(a.id)}
                            title={a.label || a.account_number}
                            subtitle={`${(a.mode || "live").toUpperCase()} · ${a.broker || "—"}`}
                            cfg={cfgByAccount[a.id]}
                            testid={`scope-account-${a.id}`}
                        />
                    ))}
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest leading-relaxed">
                    {selectedAccountId
                        ? hasOverride
                            ? "EDITING A PER-ACCOUNT OVERRIDE · CHANGES APPLY ONLY TO THIS ACCOUNT"
                            : "NO OVERRIDE YET · SAVING WILL CREATE A NEW PER-ACCOUNT CONFIG"
                        : "EDITING THE DEFAULT PROFILE · APPLIES TO EVERY ACCOUNT THAT HAS NO OVERRIDE"}
                    {currentCfg && (
                        <span className="ml-2 text-[#A1A1AA]">
                            · RISK <span className="text-white">{(currentCfg.risk_level || "—").toUpperCase()}</span>
                            {currentCfg.max_lot_size > 0 && (
                                <> · MAX LOT <span className="text-[#FFD700]">{currentCfg.max_lot_size}</span></>
                            )}
                        </span>
                    )}
                </div>
            </div>
        </div>
    );
}

function ScopeTile({ active, onClick, title, subtitle, cfg, testid }) {
    const isActive = Boolean(cfg?.active);
    const hasOverride = Boolean(cfg);
    return (
        <button onClick={onClick}
            data-testid={testid}
            className={`text-left p-3 border transition-colors duration-150 ${
                active ? "border-[#FFD700] bg-[#FFD700]/5" : "border-[#1F1F1F] hover:border-[#FFD700]/40"
            }`}>
            <div className="flex items-center justify-between mb-1.5">
                <div className="font-display font-bold text-sm tracking-tight truncate">{title}</div>
                <div className="flex items-center gap-1 shrink-0">
                    {hasOverride && (
                        <span className={`font-mono text-[9px] tracking-widest px-1.5 py-0.5 border ${
                            isActive ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#A1A1AA]"
                        }`}>
                            {isActive ? "● ON" : "○ OFF"}
                        </span>
                    )}
                </div>
            </div>
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">{subtitle}</div>
            {!hasOverride && (
                <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-1.5 italic">USES DEFAULT</div>
            )}
        </button>
    );
}


const PREVIEW_SYMBOLS = ["XAUUSD", "BTCUSD"];

function PositionSizingPreview({ accountId, cfg }) {
    const [symbol, setSymbol] = useState("XAUUSD");
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState("");

    // Recompute whenever the user changes the symbol, risk level, max_lot_size,
    // or switches to a different account scope.
    const riskLevel = cfg?.risk_level;
    const maxLot = cfg?.max_lot_size;

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            setLoading(true);
            setErr("");
            try {
                const params = new URLSearchParams({ symbol });
                if (accountId) params.set("account_id", accountId);
                const { data: d } = await api.get(`/bot/sizing-preview?${params.toString()}`);
                if (!cancelled) setData(d);
            } catch (e) {
                if (!cancelled) setErr(formatApiError(e));
            } finally {
                if (!cancelled) setLoading(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [symbol, accountId, riskLevel, maxLot]);

    const maxEff = data ? Math.max(...data.rows.map(r => r.effective_lot || 0)) : 0;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="sizing-preview">
            <div className="border-b border-[#1F1F1F] px-5 py-3 flex items-center gap-3 flex-wrap">
                <Gauge className="w-4 h-4 text-[#FFD700]" />
                <div className="font-display text-base">Position Sizing Preview</div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    LIVE · UPDATES AS YOU TUNE
                </div>
                <div className="ml-auto flex items-center gap-1.5">
                    {PREVIEW_SYMBOLS.map(s => (
                        <button key={s} type="button" onClick={() => setSymbol(s)}
                                data-testid={`sizing-symbol-${s}`}
                                className={`font-mono text-[10px] tracking-widest px-2.5 py-1 border transition-colors ${
                                    symbol === s
                                        ? "border-[#FFD700] text-[#FFD700] bg-[#FFD700]/10"
                                        : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333]"
                                }`}>
                            {s}
                        </button>
                    ))}
                </div>
            </div>

            {err && (
                <div className="px-5 py-4 font-mono text-xs text-[#FF3B30]"
                     data-testid="sizing-error">{err}</div>
            )}

            {!err && data && data.rows.length === 0 && (
                <div className="px-5 py-4 font-mono text-xs text-[#A1A1AA]"
                     data-testid="sizing-empty">
                    Connect an account to see lot-size projections.
                </div>
            )}

            {!err && data && data.rows.length > 0 && (
                <>
                    <div className="px-5 py-3 grid grid-cols-2 md:grid-cols-4 gap-3 border-b border-[#1F1F1F]"
                         data-testid="sizing-context">
                        <PreviewStat label="Account" value={data.account_label || "—"}
                                     sub={`${data.account_type || ""}`.toUpperCase()} />
                        <PreviewStat label="Equity" value={`$${(data.account_equity || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                                     sub={`RISK ${(data.risk_pct).toFixed(1)}%`} />
                        <PreviewStat label="Risk Level"
                                     value={(data.risk_level || "").toUpperCase()}
                                     sub={`KELLY CAP ${(data.kelly_cap * 100).toFixed(0)}%`} />
                        <PreviewStat label="Max Lot Cap"
                                     value={data.max_lot_size > 0 ? data.max_lot_size : "—"}
                                     sub={data.max_lot_size > 0 ? "AT PEAK KELLY" : "UNSET · PURE KELLY"} />
                    </div>

                    <div className="px-5 py-3 font-mono text-[10px] text-[#52525B] tracking-widest border-b border-[#1F1F1F]">
                        SCENARIO · {data.symbol} · entry {data.scenario.entry_price} · SL {data.scenario.stop_loss}
                        ({data.scenario.sl_distance_price.toFixed(2)} price units)
                    </div>

                    <div className="overflow-x-auto">
                        <table className="w-full text-xs font-mono">
                            <thead>
                                <tr className="text-[10px] tracking-widest text-[#52525B] border-b border-[#1F1F1F]">
                                    <th className="px-4 py-2 text-left">CONF</th>
                                    <th className="px-4 py-2 text-right">KELLY f</th>
                                    <th className="px-4 py-2 text-right">RISK USD</th>
                                    <th className="px-4 py-2 text-right">ABS LOT</th>
                                    <th className="px-4 py-2 text-right">YOU GET</th>
                                    <th className="px-4 py-2 text-left">BAR</th>
                                </tr>
                            </thead>
                            <tbody data-testid="sizing-rows">
                                {data.rows.map(r => {
                                    const pct = maxEff > 0 ? (r.effective_lot / maxEff) * 100 : 0;
                                    return (
                                        <tr key={r.confidence_pct}
                                            data-testid={`sizing-row-${r.confidence_pct}`}
                                            className={r.below_min_confidence
                                                ? "border-b border-[#1F1F1F] opacity-40"
                                                : "border-b border-[#1F1F1F]"}>
                                            <td className="px-4 py-2 text-left text-[#A1A1AA]">
                                                {r.confidence_pct}%
                                                {r.below_min_confidence && (
                                                    <span className="ml-1.5 text-[9px] text-[#FF3B30]">VETO</span>
                                                )}
                                            </td>
                                            <td className="px-4 py-2 text-right text-[#A1A1AA]">
                                                {r.kelly_f?.toFixed(2) ?? "—"}
                                            </td>
                                            <td className="px-4 py-2 text-right text-[#A1A1AA]">
                                                ${(r.risk_amount_usd || 0).toFixed(2)}
                                            </td>
                                            <td className="px-4 py-2 text-right text-[#52525B]">
                                                {r.absolute_lot?.toFixed(2)}
                                            </td>
                                            <td className="px-4 py-2 text-right text-[#FFD700] font-bold">
                                                {r.effective_lot?.toFixed(2)}
                                            </td>
                                            <td className="px-4 py-2">
                                                <div className="w-full bg-[#1F1F1F] h-1.5 overflow-hidden">
                                                    <div className="bg-[#FFD700] h-full transition-all"
                                                         style={{ width: `${Math.min(100, pct)}%` }} />
                                                </div>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>

                    <div className="px-5 py-3 border-t border-[#1F1F1F] font-mono text-[9px] text-[#52525B] tracking-widest">
                        ABS LOT = pure Kelly with no cap.
                        YOU GET = effective lot after applying your Max Lot Cap (scaled by confidence).
                        Below {data.min_confidence}% confidence the signal is vetoed → no trade.
                    </div>
                </>
            )}

            {loading && (
                <div className="px-5 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">
                    RECOMPUTING…
                </div>
            )}
        </div>
    );
}

function PreviewStat({ label, value, sub }) {
    return (
        <div>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-0.5">{label}</div>
            <div className="font-display text-sm tracking-tight truncate">{value}</div>
            {sub && (
                <div className="font-mono text-[9px] text-[#A1A1AA] tracking-widest mt-0.5">{sub}</div>
            )}
        </div>
    );
}
