import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { toast } from "sonner";
import {
    Sparkles, BarChart3, TrendingUp, TrendingDown, Save,
    Trash2, Library, Send, Bot, Clock, Code2, Wand2, Trophy, Rocket,
} from "lucide-react";

const EXAMPLES = [
    "Conservative gold scalping during London session, max 2 trades",
    "Aggressive Bitcoin swing trading with auto-execute, high risk",
    "Balanced multi-asset trend follower on XAUUSD and BTCUSD",
    "Tokyo-session XAUUSD mean-reversion, low risk, 1 trade max",
];

export default function Strategies() {
    const [prompt, setPrompt] = useState("");
    const [compiling, setCompiling] = useState(false);
    const [compiled, setCompiled] = useState(null);
    const [name, setName] = useState("");
    const [err, setErr] = useState("");

    const [backtestLoading, setBacktestLoading] = useState(false);
    const [backtest, setBacktest] = useState(null);

    // Stage 2: code + optimize
    const [codeLoading, setCodeLoading] = useState(false);
    const [dsl, setDsl] = useState(null);
    const [optLoading, setOptLoading] = useState(false);
    const [optResult, setOptResult] = useState(null);

    const [library, setLibrary] = useState([]);
    const [libLoading, setLibLoading] = useState(true);

    const loadLibrary = useCallback(async () => {
        try {
            const { data } = await api.get("/strategies");
            setLibrary(data.items || []);
        } catch (e) {
            console.warn("[strategies] load failed", e?.message);
        } finally {
            setLibLoading(false);
        }
    }, []);

    useEffect(() => { loadLibrary(); }, [loadLibrary]);

    const compile = async () => {
        if (!prompt.trim()) return;
        setErr(""); setCompiling(true);
        setBacktest(null); setDsl(null); setOptResult(null);
        try {
            const { data } = await api.post("/nl/strategy", { prompt });
            if (data.compiled?.clarification_needed) {
                setErr(`Needs more detail: ${data.compiled.clarification_needed}`);
                setCompiled(null);
            } else {
                setCompiled(data.compiled);
                if (!name.trim()) {
                    setName(prompt.slice(0, 40));
                }
            }
        } catch (e) {
            setErr(formatApiError(e));
        } finally { setCompiling(false); }
    };

    const writeCode = async () => {
        if (!compiled) return;
        setCodeLoading(true); setOptResult(null);
        try {
            const { data } = await api.post("/nl/strategy/code", { compiled });
            setDsl(data.dsl);
            toast.success("Code generated");
        } catch (e) {
            toast.error("Code generation failed", { description: formatApiError(e) });
        } finally {
            setCodeLoading(false);
        }
    };

    const runBacktest = async () => {
        if (!compiled) return;
        setBacktestLoading(true);
        try {
            const { data } = await api.post("/nl/strategy/backtest", {
                compiled, lookback_days: 30,
            });
            setBacktest(data);
        } catch (e) {
            toast.error("Backtest failed", { description: formatApiError(e) });
        } finally {
            setBacktestLoading(false);
        }
    };

    const optimize = async () => {
        const target = dsl || compiled;
        if (!target?.symbols) {
            toast.error("Need a compiled strategy with symbols first");
            return;
        }
        setOptLoading(true);
        try {
            const { data } = await api.post("/nl/strategy/optimize", { dsl: target });
            setOptResult(data);
            toast.success(
                data.improvement_pct > 0
                    ? `Optimizer found +${data.improvement_pct}% improvement`
                    : "Baseline already optimal across tested grid"
            );
        } catch (e) {
            toast.error("Optimize failed", { description: formatApiError(e) });
        } finally {
            setOptLoading(false);
        }
    };

    const applyOptimized = () => {
        if (!optResult?.best?.filters) return;
        const merged = {
            ...(compiled || {}),
            symbols: optResult.best.filters.symbols,
            session_preference: optResult.best.filters.session_preference,
        };
        setCompiled(merged);
        toast.success("Optimized filters merged into compiled strategy");
    };

    const [applyTarget, setApplyTarget] = useState("matching");
    const [applyTargets, setApplyTargets] = useState(null);

    // Refresh candidate-bots list whenever the compiled strategy's symbols change.
    useEffect(() => {
        if (!compiled?.symbols?.length) { setApplyTargets(null); return; }
        const syms = compiled.symbols.join(",");
        api.get(`/nl/strategy/targets?symbols=${encodeURIComponent(syms)}`)
            .then(r => setApplyTargets(r.data))
            .catch(() => setApplyTargets(null));
    }, [compiled?.symbols]);

    const applyToBot = async () => {
        if (!compiled) return;
        try {
            const { data } = await api.post("/nl/strategy/apply", {
                compiled, target: applyTarget,
            });
            const n = data.applied_count ?? 1;
            const names = (data.applied_to || [])
                .map(a => a.is_default ? "Default profile" : `acct ${String(a.account_id).slice(-6)}`)
                .join(", ");
            toast.success(`Strategy deployed to ${n} bot${n === 1 ? "" : "s"}${names ? ` — ${names}` : ""}`);
        } catch (e) {
            toast.error("Apply failed", { description: formatApiError(e) });
        }
    };

    const saveStrategy = async () => {
        if (!compiled || !name.trim()) {
            toast.error("Compile a strategy and give it a name first");
            return;
        }
        try {
            await api.post("/strategies", {
                name: name.trim(), prompt, compiled, backtest,
                dsl, optimization: optResult,
            });
            toast.success("Saved to library");
            setName("");
            loadLibrary();
        } catch (e) {
            toast.error("Save failed", { description: formatApiError(e) });
        }
    };

    const removeStrategy = async (id) => {
        if (!window.confirm("Delete this saved strategy?")) return;
        try {
            await api.delete(`/strategies/${id}`);
            loadLibrary();
        } catch (e) {
            toast.error("Delete failed", { description: formatApiError(e) });
        }
    };

    const loadIntoEditor = (s) => {
        setPrompt(s.prompt || "");
        setCompiled(s.compiled || null);
        setBacktest(s.backtest || null);
        setDsl(s.dsl || null);
        setOptResult(s.optimization || null);
        setName(s.name || "");
        window.scrollTo({ top: 0, behavior: "smooth" });
    };

    return (
        <AppLayout>
            <PageHeader
                title="AI Strategy Generator"
                subtitle="Describe a strategy in plain English — STOIC compiles, backtests, and applies it to your bot."
                testid="strategies-header"
            />
            <div className="p-4 md:p-8 space-y-6 max-w-5xl">
                {err && (
                    <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono"
                         data-testid="strategies-error">
                        {err}
                    </div>
                )}

                {/* Prompt input */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <Sparkles className="w-4 h-4 text-[#FFB000]" />
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                            STRATEGY PROMPT
                        </span>
                    </div>
                    <div className="p-4 space-y-3">
                        <textarea
                            value={prompt}
                            onChange={e => setPrompt(e.target.value)}
                            data-testid="strategy-prompt-input"
                            placeholder="Describe your trading strategy in plain English…"
                            rows={3}
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFB000] px-3 py-2 text-sm font-mono outline-none transition-colors resize-none"
                        />
                        <div className="flex flex-wrap gap-1.5">
                            {EXAMPLES.map((ex) => (
                                <button key={ex} onClick={() => setPrompt(ex)}
                                    data-testid={`strategy-example-${ex.slice(0, 12)}`}
                                    className="font-mono text-[10px] text-[#A1A1AA] hover:text-[#FFB000] bg-[#121212] border border-[#1F1F1F] px-2 py-1 transition-colors">
                                    <Clock className="w-3 h-3 inline mr-1" />{ex}
                                </button>
                            ))}
                        </div>
                        <div className="flex gap-2">
                            <button
                                onClick={compile}
                                disabled={compiling || !prompt.trim()}
                                data-testid="strategy-compile-button"
                                className="px-4 py-2 text-xs font-mono tracking-widest bg-[#FFB000] hover:bg-[#E59E00] disabled:opacity-40 text-black flex items-center gap-1.5">
                                <Send className="w-3.5 h-3.5" />
                                {compiling ? "COMPILING…" : "COMPILE"}
                            </button>
                        </div>
                    </div>
                </div>

                {/* Compiled preview + 5-stage pipeline */}
                {compiled && (
                    <div className="border border-[#FFB000]/30 bg-[#FFB000]/5 p-4 space-y-3"
                         data-testid="strategy-compiled-panel">
                        <div className="flex items-center gap-2">
                            <Bot className="w-4 h-4 text-[#FFB000]" />
                            <span className="font-mono text-[10px] text-[#FFB000] tracking-widest">
                                STAGE 1 · COMPILED STRATEGY
                            </span>
                        </div>
                        {compiled.notes && (
                            <div className="text-sm text-[#E4E4E7]" data-testid="strategy-notes">
                                {compiled.notes}
                            </div>
                        )}
                        <pre className="font-mono text-[10px] text-[#A1A1AA] whitespace-pre-wrap bg-[#050505] border border-[#1F1F1F] p-3 max-h-48 overflow-y-auto"
                             data-testid="strategy-compiled-json">
                            {JSON.stringify(compiled, null, 2)}
                        </pre>

                        {/* Pipeline buttons — Code → Backtest → Optimize → Apply */}
                        <div className="flex gap-2 flex-wrap items-center">
                            <input
                                value={name}
                                onChange={e => setName(e.target.value)}
                                data-testid="strategy-name-input"
                                placeholder="Name to save (optional)…"
                                maxLength={80}
                                className="flex-1 min-w-[200px] bg-[#050505] border border-[#1F1F1F] focus:border-[#FFB000] px-3 py-2 text-xs font-mono outline-none"
                            />
                            <button onClick={writeCode}
                                disabled={codeLoading}
                                data-testid="strategy-code-button"
                                className="px-3 py-2 text-xs font-mono tracking-widest border border-[#A855F7]/50 text-[#A855F7] hover:bg-[#A855F7]/10 disabled:opacity-40 flex items-center gap-1.5">
                                <Code2 className="w-3.5 h-3.5" />
                                {codeLoading ? "WRITING…" : "WRITE CODE"}
                            </button>
                            <button onClick={runBacktest}
                                disabled={backtestLoading}
                                data-testid="strategy-backtest-button"
                                className="px-3 py-2 text-xs font-mono tracking-widest border border-[#00FF41]/50 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-40 flex items-center gap-1.5">
                                <BarChart3 className="w-3.5 h-3.5" />
                                {backtestLoading ? "BACKTESTING…" : "BACKTEST 30d"}
                            </button>
                            <button onClick={optimize}
                                disabled={optLoading}
                                data-testid="strategy-optimize-button"
                                className="px-3 py-2 text-xs font-mono tracking-widest border border-[#06B6D4]/50 text-[#06B6D4] hover:bg-[#06B6D4]/10 disabled:opacity-40 flex items-center gap-1.5">
                                <Wand2 className="w-3.5 h-3.5" />
                                {optLoading ? "OPTIMIZING…" : "OPTIMIZE"}
                            </button>
                            <button onClick={saveStrategy}
                                data-testid="strategy-save-button"
                                className="px-3 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#FFB000] text-[#A1A1AA] hover:text-[#FFB000] flex items-center gap-1.5">
                                <Save className="w-3.5 h-3.5" /> SAVE
                            </button>
                            {(applyTargets?.total_count ?? 0) > 1 && (
                                <select value={applyTarget}
                                    onChange={(e) => setApplyTarget(e.target.value)}
                                    data-testid="strategy-apply-target"
                                    className="bg-[#050505] border border-[#FFB000]/40 focus:border-[#FFB000] text-[10px] font-mono tracking-widest px-2 py-2 outline-none">
                                    <option value="matching">
                                        Matching bots ({applyTargets.matching_count})
                                    </option>
                                    <option value="all">
                                        All bots ({applyTargets.total_count})
                                    </option>
                                    {(applyTargets.candidates || []).map(c => (
                                        <option key={c.key} value={c.key}>
                                            Only · {c.label}{c.matches_proposal_symbols ? " ✓" : ""}
                                        </option>
                                    ))}
                                </select>
                            )}
                            <button onClick={applyToBot}
                                data-testid="strategy-apply-button"
                                className="px-3 py-2 text-xs font-mono tracking-widest bg-[#FFB000] text-black hover:bg-[#E59E00] flex items-center gap-1.5">
                                <Rocket className="w-3.5 h-3.5" /> DEPLOY
                            </button>
                        </div>
                    </div>
                )}

                {/* Stage 2: Generated code/DSL */}
                {dsl && (
                    <CodePanel dsl={dsl} />
                )}

                {/* Stage 3: Backtest results */}
                {backtest && (
                    <BacktestPanel backtest={backtest} />
                )}

                {/* Stage 4: Optimization results */}
                {optResult && (
                    <OptimizePanel result={optResult} onApply={applyOptimized} />
                )}

                {/* Library */}
                <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="strategy-library">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <Library className="w-4 h-4 text-[#06B6D4]" />
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                            YOUR STRATEGY LIBRARY · {library.length}
                        </span>
                    </div>
                    {libLoading ? (
                        <div className="p-6 text-center font-mono text-xs text-[#52525B] tracking-widest">
                            LOADING…
                        </div>
                    ) : library.length === 0 ? (
                        <div className="p-8 text-center" data-testid="library-empty">
                            <Library className="w-8 h-8 text-[#52525B] mx-auto mb-2" />
                            <div className="text-sm text-[#A1A1AA]">
                                No saved strategies yet. Compile + save one above to build your library.
                            </div>
                        </div>
                    ) : (
                        <div className="divide-y divide-[#1F1F1F]">
                            {library.map((s) => (
                                <div key={s.id} className="px-5 py-3 flex items-center gap-3 flex-wrap"
                                     data-testid={`library-row-${s.id}`}>
                                    <div className="flex-1 min-w-0">
                                        <div className="font-display font-bold text-sm tracking-tight truncate">
                                            {s.name}
                                        </div>
                                        <div className="text-xs text-[#A1A1AA] truncate" title={s.prompt}>
                                            {s.prompt}
                                        </div>
                                        <div className="flex items-center gap-2 mt-1 font-mono text-[10px] text-[#52525B] tracking-widest">
                                            <span>{(s.compiled?.symbols || []).join(", ")}</span>
                                            <span>·</span>
                                            <span>{s.compiled?.risk_level?.toUpperCase()}</span>
                                            <span>·</span>
                                            <span>{s.compiled?.session_preference?.toUpperCase()}</span>
                                            {s.backtest?.win_rate != null && (
                                                <>
                                                    <span>·</span>
                                                    <span className="text-[#00FF41]">{(s.backtest.win_rate * 100).toFixed(0)}% WIN</span>
                                                </>
                                            )}
                                        </div>
                                    </div>
                                    <button onClick={() => loadIntoEditor(s)}
                                        data-testid={`library-load-${s.id}`}
                                        className="px-2 py-1.5 text-[10px] font-mono tracking-widest border border-[#1F1F1F] hover:border-[#FFB000] text-[#A1A1AA] hover:text-[#FFB000]">
                                        LOAD
                                    </button>
                                    <button onClick={() => removeStrategy(s.id)}
                                        data-testid={`library-delete-${s.id}`}
                                        className="p-1.5 text-[#52525B] hover:text-[#FF3B30]">
                                        <Trash2 className="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}
                </section>
            </div>
        </AppLayout>
    );
}

function BacktestPanel({ backtest }) {
    return (
        <div className="border border-[#00FF41]/30 bg-[#00FF41]/5 p-4 space-y-3"
             data-testid="strategy-backtest-results">
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="font-mono text-[10px] text-[#00FF41] tracking-widest flex items-center gap-1.5">
                    <BarChart3 className="w-3.5 h-3.5" />
                    STAGE 3 · BACKTEST · LAST {backtest.lookback_days}d
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    {backtest.matched_trades}/{backtest.total_trades} TRADES MATCHED
                </div>
            </div>

            {backtest.matched_trades === 0 ? (
                <div className="text-xs text-[#A1A1AA] font-mono">
                    No closed trades match these filters in the last {backtest.lookback_days}d.
                    Run the bot in paper mode to build history first.
                </div>
            ) : (
                <>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                        <Metric label="WIN RATE"
                            value={backtest.win_rate != null ? `${(backtest.win_rate * 100).toFixed(1)}%` : "—"}
                            color={(backtest.win_rate ?? 0) >= 0.5 ? "#00FF41" : "#FFB000"}
                            sub={`${backtest.wins}W · ${backtest.losses}L`} />
                        <Metric label="TOTAL P&L"
                            value={`$${backtest.total_pnl_usd.toFixed(2)}`}
                            color={backtest.total_pnl_usd >= 0 ? "#00FF41" : "#FF3B30"}
                            icon={backtest.total_pnl_usd >= 0 ? TrendingUp : TrendingDown} />
                        <Metric label="AVG / TRADE"
                            value={backtest.avg_pnl_usd != null ? `$${backtest.avg_pnl_usd.toFixed(2)}` : "—"}
                            color="#E4E4E7" />
                        <Metric label="BEST / WORST"
                            value={<>
                                <span className="text-[#00FF41]">+${backtest.best_trade?.toFixed(2) ?? "—"}</span>
                                <span className="text-[#52525B] mx-1.5">·</span>
                                <span className="text-[#FF3B30]">${backtest.worst_trade?.toFixed(2) ?? "—"}</span>
                            </>}
                            small />
                    </div>

                    {Object.keys(backtest.by_symbol || {}).length > 0 && (
                        <div className="border-t border-[#00FF41]/20 pt-3 space-y-1">
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-2">PER SYMBOL</div>
                            {Object.entries(backtest.by_symbol).map(([sym, row]) => (
                                <div key={sym} className="flex items-center gap-3 font-mono text-[11px]">
                                    <span className="text-[#FFB000] w-16">{sym}</span>
                                    <span className="text-[#A1A1AA]">{row.trades} trades</span>
                                    <span className="text-[#52525B]">·</span>
                                    <span className="text-[#A1A1AA]">{row.win_rate != null ? `${(row.win_rate * 100).toFixed(0)}% wins` : "—"}</span>
                                    <span className="text-[#52525B]">·</span>
                                    <span style={{ color: row.pnl >= 0 ? "#00FF41" : "#FF3B30" }}>
                                        ${row.pnl.toFixed(2)}
                                    </span>
                                </div>
                            ))}
                        </div>
                    )}
                </>
            )}

            {backtest.notes?.length > 0 && (
                <div className="border-t border-[#00FF41]/20 pt-3 space-y-1">
                    {backtest.notes.map((n) => (
                        <div key={n} className="text-[10px] text-[#A1A1AA] font-mono leading-snug">· {n}</div>
                    ))}
                </div>
            )}
        </div>
    );
}

function Metric({ label, value, color = "#E4E4E7", sub, icon: Icon, small }) {
    return (
        <div>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className={`font-mono font-bold flex items-center gap-1 ${small ? "text-xs" : "text-xl"}`}
                 style={{ color }}>
                {Icon && <Icon className="w-4 h-4" />}
                {value}
            </div>
            {sub && <div className="text-[10px] text-[#52525B] font-mono mt-0.5">{sub}</div>}
        </div>
    );
}

function CodePanel({ dsl }) {
    return (
        <div className="border border-[#A855F7]/30 bg-[#A855F7]/5 p-4 space-y-3"
             data-testid="strategy-code-panel">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <Code2 className="w-4 h-4 text-[#A855F7]" />
                    <span className="font-mono text-[10px] text-[#A855F7] tracking-widest">
                        STAGE 2 · GENERATED CODE
                    </span>
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    DSL v{dsl.version} · {dsl.entry_rules?.length || 0} entry · {dsl.exit_rules?.length || 0} exit
                </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {/* Pseudocode block */}
                <div data-testid="strategy-pseudocode">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">
                        PSEUDOCODE
                    </div>
                    <pre className="font-mono text-[11px] text-[#E4E4E7] bg-[#050505] border border-[#1F1F1F] p-3 leading-snug whitespace-pre-wrap max-h-72 overflow-y-auto">
                        {dsl.pseudocode}
                    </pre>
                </div>

                {/* DSL rules */}
                <div data-testid="strategy-dsl-rules">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">
                        EXECUTABLE DSL
                    </div>
                    <div className="bg-[#050505] border border-[#1F1F1F] p-3 max-h-72 overflow-y-auto space-y-2">
                        <div>
                            <div className="font-mono text-[9px] text-[#A855F7] tracking-widest mb-1">PARAMS</div>
                            {Object.entries(dsl.params || {}).map(([k, v]) => (
                                <div key={k} className="font-mono text-[10px] flex justify-between">
                                    <span className="text-[#A1A1AA]">{k}</span>
                                    <span className="text-[#E4E4E7]">{String(v)}</span>
                                </div>
                            ))}
                        </div>
                        <div>
                            <div className="font-mono text-[9px] text-[#00FF41] tracking-widest mb-1">ENTRY RULES</div>
                            {(dsl.entry_rules || []).map((r, i) => (
                                <div key={`e${i}`} className="font-mono text-[10px] text-[#A1A1AA] leading-snug">
                                    <span className="text-[#00FF41]">{r.side}</span> when{" "}
                                    <span className="text-[#FFB000]">{r.field}</span>{" "}
                                    <span className="text-white">{r.op}</span>{" "}
                                    <span className="text-[#06B6D4]">{r.value_ref || JSON.stringify(r.value)}</span>
                                    {r.description && <div className="text-[#52525B] text-[9px]">{r.description}</div>}
                                </div>
                            ))}
                        </div>
                        <div>
                            <div className="font-mono text-[9px] text-[#FF3B30] tracking-widest mb-1">EXIT RULES</div>
                            {(dsl.exit_rules || []).map((r, i) => (
                                <div key={`x${i}`} className="font-mono text-[10px] flex justify-between">
                                    <span className="text-[#A1A1AA]">{r.kind}</span>
                                    <span className="text-[#E4E4E7]">{r.value}%</span>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}

function OptimizePanel({ result, onApply }) {
    const better = result.improvement_pct > 0
        && JSON.stringify(result.best.filters) !== JSON.stringify(result.baseline.filters);
    return (
        <div className="border border-[#06B6D4]/30 bg-[#06B6D4]/5 p-4 space-y-3"
             data-testid="strategy-optimize-panel">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <Wand2 className="w-4 h-4 text-[#06B6D4]" />
                    <span className="font-mono text-[10px] text-[#06B6D4] tracking-widest">
                        STAGE 4 · OPTIMIZER
                    </span>
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    {result.tested_variants} VARIANTS TESTED
                </div>
            </div>

            {/* Baseline vs Best */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="border border-[#1F1F1F] p-3" data-testid="opt-baseline">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-2">BASELINE</div>
                    <VariantStats v={result.baseline} />
                </div>
                <div className={`border p-3 ${better ? "border-[#00FF41]/40 bg-[#00FF41]/5" : "border-[#1F1F1F]"}`}
                     data-testid="opt-best">
                    <div className="flex items-center justify-between mb-2">
                        <div className="font-mono text-[9px] text-[#06B6D4] tracking-widest flex items-center gap-1">
                            <Trophy className="w-3 h-3" /> BEST VARIANT
                        </div>
                        {result.improvement_pct !== 0 && (
                            <div className={`font-mono text-[10px] tracking-widest ${
                                result.improvement_pct > 0 ? "text-[#00FF41]" : "text-[#FF3B30]"
                            }`}>
                                {result.improvement_pct > 0 ? "+" : ""}{result.improvement_pct}%
                            </div>
                        )}
                    </div>
                    <VariantStats v={result.best} />
                    {better && (
                        <button onClick={onApply}
                            data-testid="opt-apply-best"
                            className="mt-3 w-full px-3 py-1.5 text-[10px] font-mono tracking-widest bg-[#06B6D4] hover:bg-[#0891B2] text-black flex items-center justify-center gap-1.5">
                            <Rocket className="w-3 h-3" /> APPLY BEST VARIANT
                        </button>
                    )}
                </div>
            </div>

            {/* Top variants table */}
            {result.variants?.length > 0 && (
                <div data-testid="opt-variants-list" className="border-t border-[#06B6D4]/20 pt-3">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-2">
                        TOP {result.variants.length} VARIANTS (RANKED BY COMPOSITE SCORE)
                    </div>
                    <div className="space-y-1 max-h-64 overflow-y-auto">
                        {result.variants.map((v, i) => (
                            <div key={`v${i}`} className="font-mono text-[10px] flex items-center gap-2 text-[#A1A1AA]">
                                <span className="text-[#52525B] w-5">#{i + 1}</span>
                                <span className="text-[#06B6D4] w-20 truncate">{(v.filters.symbols || []).join(",")}</span>
                                <span className="w-14 text-[#FFB000]">{v.filters.session_preference?.toUpperCase()}</span>
                                <span className="w-10 text-[#A1A1AA]">{v.filters.lookback_days}d</span>
                                <span className="w-16">
                                    {v.win_rate != null ? `${(v.win_rate * 100).toFixed(0)}% W` : "—"}
                                </span>
                                <span className="w-12 text-[#52525B]">n={v.matched_trades}</span>
                                <span className="flex-1 text-right" style={{ color: (v.total_pnl_usd || 0) >= 0 ? "#00FF41" : "#FF3B30" }}>
                                    ${(v.total_pnl_usd || 0).toFixed(2)}
                                </span>
                                <span className="w-16 text-right text-[#06B6D4]">{(v.score || 0).toFixed(3)}</span>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Transparency notes */}
            {result.notes?.length > 0 && (
                <div className="border-t border-[#06B6D4]/20 pt-3 space-y-1" data-testid="opt-notes">
                    {result.notes.map((n) => (
                        <div key={n} className="text-[10px] text-[#A1A1AA] font-mono leading-snug">· {n}</div>
                    ))}
                </div>
            )}
        </div>
    );
}

function VariantStats({ v }) {
    return (
        <div className="space-y-1 font-mono text-[11px]">
            <div className="flex justify-between text-[#A1A1AA]">
                <span>Symbols</span>
                <span className="text-[#FFB000]">{(v.filters?.symbols || []).join(", ")}</span>
            </div>
            <div className="flex justify-between text-[#A1A1AA]">
                <span>Session</span>
                <span className="text-[#E4E4E7]">{v.filters?.session_preference?.toUpperCase()}</span>
            </div>
            <div className="flex justify-between text-[#A1A1AA]">
                <span>Lookback</span>
                <span className="text-[#E4E4E7]">{v.filters?.lookback_days || 30}d</span>
            </div>
            <div className="flex justify-between text-[#A1A1AA] pt-1 border-t border-[#1F1F1F]">
                <span>Win rate</span>
                <span style={{ color: (v.win_rate || 0) >= 0.5 ? "#00FF41" : "#FFB000" }}>
                    {v.win_rate != null ? `${(v.win_rate * 100).toFixed(1)}%` : "—"}
                </span>
            </div>
            <div className="flex justify-between text-[#A1A1AA]">
                <span>Total P&amp;L</span>
                <span style={{ color: (v.total_pnl_usd || 0) >= 0 ? "#00FF41" : "#FF3B30" }}>
                    ${(v.total_pnl_usd || 0).toFixed(2)}
                </span>
            </div>
            <div className="flex justify-between text-[#A1A1AA]">
                <span>Sample</span>
                <span className="text-[#E4E4E7]">{v.matched_trades || 0} trades</span>
            </div>
            <div className="flex justify-between text-[#A1A1AA]">
                <span>Score</span>
                <span className="text-[#06B6D4]">{(v.score || 0).toFixed(3)}</span>
            </div>
        </div>
    );
}
