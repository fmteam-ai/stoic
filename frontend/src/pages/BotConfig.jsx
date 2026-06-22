import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Play, Pause, Save as FloppyDisk, Plus, X, AlertTriangle, Shield, TrendingUp, Scissors, OctagonAlert, Gauge, Activity } from "lucide-react";

const RISK_DESCRIPTIONS = {
    low: "Capital preservation. Smaller positions, tighter stops, only high-conviction setups.",
    medium: "Balanced approach. Moderate position sizing with prudent stop placement.",
    high: "Aggressive growth. Larger positions, wider targets, accepts lower confidence trades.",
    extreme: "Maximum risk. Largest size, lowest threshold, suitable only for microcent or experimental accounts.",
};

export default function BotConfig() {
    const [cfg, setCfg] = useState(null);
    const [profiles, setProfiles] = useState({});
    const [supported, setSupported] = useState([]);
    const [saving, setSaving] = useState(false);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");
    const [newSym, setNewSym] = useState("");

    const load = useCallback(async () => {
        try {
            const [c, p, s] = await Promise.all([
                api.get("/bot/config"),
                api.get("/market/risk-profiles"),
                api.get("/market/symbols"),
            ]);
            setCfg(c.data); setProfiles(p.data.profiles); setSupported(s.data.symbols);
        } catch (e) { setErr(formatApiError(e)); }
    }, []);

    useEffect(() => { load(); }, [load]);

    const save = async () => {
        setSaving(true); setErr(""); setMsg("");
        try {
            const { data } = await api.put("/bot/config", {
                risk_level: cfg.risk_level,
                symbols: cfg.symbols,
                active: cfg.active,
                max_concurrent_trades: cfg.max_concurrent_trades,
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
                spread_filter_enabled: cfg.spread_filter_enabled,
                max_spread_pips: cfg.max_spread_pips || {},
                auto_tune_enabled: cfg.auto_tune_enabled,
            });
            setCfg(data); setMsg("Configuration saved.");
        } catch (e) { setErr(formatApiError(e)); }
        finally { setSaving(false); }
    };

    const toggleBot = async () => {
        setErr(""); setMsg("");
        try {
            const ep = cfg.active ? "/bot/stop" : "/bot/start";
            await api.post(ep);
            setCfg({ ...cfg, active: !cfg.active });
            setMsg(cfg.active ? "Bot stopped." : "Bot started.");
        } catch (e) { setErr(formatApiError(e)); }
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
                                        <div className="font-display font-bold tracking-tight">{p.label}</div>
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
                    </div>
                </div>

                {/* Section 04 — Profit Protection Suite */}
                <ProfitProtectionSection cfg={cfg} setCfg={setCfg} />

                {/* Section 05 — Trading Intelligence */}
                <TradingIntelligenceSection cfg={cfg} setCfg={setCfg} />

                <div className="flex justify-end">
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
            </div>
        </div>
    );
}
