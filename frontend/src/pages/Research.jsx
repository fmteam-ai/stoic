import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { toast } from "sonner";
import {
    Brain, RefreshCw, Sparkles, TrendingDown, TrendingUp,
    AlertTriangle, CheckCircle2, X, Clock, Trophy, Lightbulb,
} from "lucide-react";

export default function Research() {
    const [proposals, setProposals] = useState({ pending: [], history: [] });
    const [lastRun, setLastRun] = useState(null);
    const [autoAccept, setAutoAccept] = useState({ enabled: false, min_delta_pct: 10.0 });
    const [running, setRunning] = useState(false);
    const [latest, setLatest] = useState(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);

    const load = useCallback(async () => {
        setRefreshing(true);
        try {
            const [p, l, aa] = await Promise.allSettled([
                api.get("/research/proposals"),
                api.get("/research/last-run"),
                api.get("/research/auto-accept"),
            ]);
            if (p.status === "fulfilled") setProposals(p.value.data);
            if (l.status === "fulfilled") setLastRun(l.value.data);
            if (aa.status === "fulfilled") setAutoAccept(aa.value.data);
        } catch { /* silent — page surfaces errors elsewhere */ }
        finally { setLoading(false); setRefreshing(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    const runNow = async () => {
        setRunning(true);
        try {
            const { data } = await api.post("/research/run");
            setLatest(data);
            if (data.status === "ran") {
                toast.success(`Research run complete · ${data.proposal_count} proposals generated`);
            } else if (data.status === "skipped_insufficient_data") {
                toast.info("Not enough trade history yet — keep the bot running.");
            }
            await load();
        } catch (e) {
            toast.error("Run failed", { description: formatApiError(e) });
        } finally { setRunning(false); }
    };

    const accept = async (id, target) => {
        try {
            const { data } = await api.post(`/research/proposals/${id}/accept`,
                { target: target || "matching" });
            const labels = (data.applied_to || [])
                .map(a => a.is_default ? "Default profile" : `acct ${String(a.account_id).slice(-6)}`)
                .join(", ");
            if (data.applied_count === 0) {
                toast.warning("No bots matched that target — nothing was applied");
            } else if (data.applied_count === 1) {
                toast.success(`Applied to 1 bot — ${labels}`);
            } else {
                toast.success(`Applied to ${data.applied_count} bots — ${labels}`);
            }
            load();
        } catch (e) {
            toast.error("Accept failed", { description: formatApiError(e) });
        }
    };

    const dismiss = async (id) => {
        try {
            await api.post(`/research/proposals/${id}/dismiss`);
            load();
        } catch (e) {
            toast.error("Dismiss failed", { description: formatApiError(e) });
        }
    };

    const toggleAutoAccept = async (enabled) => {
        try {
            const { data } = await api.post("/research/auto-accept", {
                enabled, min_delta_pct: autoAccept.min_delta_pct,
            });
            setAutoAccept(data);
            toast.success(enabled ? "Auto-accept enabled" : "Auto-accept disabled");
        } catch (e) {
            toast.error("Failed to update setting", { description: formatApiError(e) });
        }
    };

    const updateMinDelta = async (val) => {
        try {
            const { data } = await api.post("/research/auto-accept", {
                enabled: autoAccept.enabled, min_delta_pct: val,
            });
            setAutoAccept(data);
        } catch (e) {
            toast.error("Failed", { description: formatApiError(e) });
        }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Self-Improving Research Agent"
                subtitle="Daily: analyze trades · spot weaknesses · generate hypotheses · backtest · propose improvements"
                testid="research-header"
                action={
                    <div className="flex gap-2">
                        <button onClick={load} disabled={refreshing} data-testid="research-refresh"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#00FF41]/40 text-xs font-mono tracking-widest disabled:opacity-60">
                            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin text-[#00FF41]" : ""}`} />
                            {refreshing ? "REFRESHING…" : "REFRESH"}
                        </button>
                        <button onClick={runNow} disabled={running}
                            data-testid="research-run-now"
                            className="flex items-center gap-2 px-4 py-2 bg-[#FFB000] hover:bg-[#E59E00] disabled:opacity-40 text-black text-xs font-mono tracking-widest">
                            <Sparkles className="w-3.5 h-3.5" />
                            {running ? "RUNNING…" : "RUN ANALYSIS NOW"}
                        </button>
                    </div>
                }
            />
            <div className="p-4 md:p-8 space-y-6 max-w-6xl">
                {/* Last-run header */}
                {lastRun && (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] px-5 py-3 flex flex-wrap items-center gap-3"
                         data-testid="last-run-status">
                        <Clock className="w-4 h-4 text-[#52525B]" />
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">LAST RUN</span>
                        <span className="font-mono text-xs text-[#E4E4E7]">
                            {lastRun.last_run_at ? new Date(lastRun.last_run_at).toLocaleString() : "Never"}
                        </span>
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-3">STATUS</span>
                        <span className="font-mono text-xs"
                              style={{ color: lastRun.status === "ran" ? "#00FF41" : "#FFB000" }}>
                            {lastRun.status?.toUpperCase() || "—"}
                        </span>
                        {lastRun.trade_count != null && (
                            <>
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-3">TRADES ANALYZED</span>
                                <span className="font-mono text-xs text-[#06B6D4]">{lastRun.trade_count}</span>
                            </>
                        )}
                    </div>
                )}

                {/* Auto-accept opt-in */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="auto-accept-panel">
                    <div className="flex items-center justify-between gap-3 flex-wrap">
                        <div className="flex-1 min-w-[200px]">
                            <div className="font-mono text-[10px] text-[#A855F7] tracking-widest mb-1">
                                AUTO-ACCEPT · OPT-IN
                            </div>
                            <div className="text-sm text-[#E4E4E7]">
                                Apply the top daily proposal automatically when its score delta
                                clears your threshold. You can always rollback via the History panel.
                            </div>
                        </div>
                        <div className="flex items-center gap-3">
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                MIN DELTA %
                                <input type="number" min="1" max="100" step="1"
                                    value={autoAccept.min_delta_pct}
                                    onChange={e => setAutoAccept(s => ({ ...s, min_delta_pct: parseFloat(e.target.value) || 10 }))}
                                    onBlur={e => updateMinDelta(parseFloat(e.target.value) || 10)}
                                    data-testid="auto-accept-min-delta"
                                    className="ml-2 w-16 bg-[#050505] border border-[#1F1F1F] px-2 py-1 text-xs font-mono text-[#E4E4E7]"/>
                            </label>
                            <button onClick={() => toggleAutoAccept(!autoAccept.enabled)}
                                data-testid="auto-accept-toggle"
                                className={`px-4 py-2 text-xs font-mono tracking-widest border transition-colors ${
                                    autoAccept.enabled
                                        ? "border-[#00FF41] bg-[#00FF41]/10 text-[#00FF41]"
                                        : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#A855F7]"
                                }`}>
                                {autoAccept.enabled ? "● ENABLED" : "○ DISABLED"}
                            </button>
                        </div>
                    </div>
                </div>

                {/* Latest run details */}
                {latest && (
                    <LatestRunPanel latest={latest} />
                )}

                {/* Pending proposals */}
                <Section icon={Lightbulb} color="#FFB000"
                         title={`Pending Proposals · ${proposals.pending?.length || 0}`}>
                    {loading ? (
                        <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                    ) : (proposals.pending || []).length === 0 ? (
                        <div className="text-sm text-[#A1A1AA] py-4" data-testid="no-pending">
                            No pending proposals. The research agent will scan your trades nightly
                            and surface tweaks here. Click &quot;RUN ANALYSIS NOW&quot; above to trigger immediately.
                        </div>
                    ) : (
                        <div className="space-y-3" data-testid="pending-proposals">
                            {(proposals.pending || []).map((p) => (
                                <ProposalCard key={p.id} p={p} onAccept={() => accept(p.id)} onDismiss={() => dismiss(p.id)} />
                            ))}
                        </div>
                    )}
                </Section>

                {/* History */}
                {(proposals.history || []).length > 0 && (
                    <Section icon={Brain} color="#A855F7"
                             title={`Historical Proposals · ${proposals.history.length}`}>
                        <div className="space-y-1" data-testid="history-list">
                            {proposals.history.map((p) => (
                                <div key={p.id} className="font-mono text-xs flex items-center gap-3 py-1">
                                    {p.status === "accepted" ? (
                                        <CheckCircle2 className="w-3 h-3 text-[#00FF41] shrink-0" />
                                    ) : (
                                        <X className="w-3 h-3 text-[#52525B] shrink-0" />
                                    )}
                                    <span className="text-[#FFB000] w-32 truncate">{p.name}</span>
                                    <span className={p.status === "accepted" ? "text-[#00FF41]" : "text-[#52525B]"}>
                                        {p.status?.toUpperCase()}
                                    </span>
                                    <span className="text-[#52525B] ml-auto">
                                        {p.created_at ? new Date(p.created_at).toLocaleDateString() : ""}
                                    </span>
                                </div>
                            ))}
                        </div>
                    </Section>
                )}
            </div>
        </AppLayout>
    );
}

function Section({ icon: Icon, color, title, children }) {
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Icon className="w-4 h-4" style={{ color }} />
                <div className="font-display font-bold text-base tracking-tight">{title}</div>
            </div>
            <div className="p-5">{children}</div>
        </section>
    );
}

function LatestRunPanel({ latest }) {
    if (latest.status === "skipped_insufficient_data") {
        return (
            <div className="border border-[#FFB000]/30 bg-[#FFB000]/5 p-4 flex items-start gap-3"
                 data-testid="insufficient-data-warning">
                <AlertTriangle className="w-5 h-5 text-[#FFB000] shrink-0 mt-0.5" />
                <div>
                    <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-1">
                        INSUFFICIENT DATA
                    </div>
                    <div className="text-sm text-[#E4E4E7]">
                        Only {latest.weaknesses?.trade_count || 0} closed trades in the last 30 days.
                        The research agent needs ≥5 trades to draw conclusions. Keep the bot running.
                    </div>
                </div>
            </div>
        );
    }
    const weak = latest.weaknesses?.weaknesses || [];
    const strong = latest.weaknesses?.strengths || [];
    const overall = latest.weaknesses?.overall;
    return (
        <div className="border border-[#06B6D4]/30 bg-[#06B6D4]/5 p-4 space-y-3"
             data-testid="latest-run-panel">
            <div className="flex items-center gap-2">
                <Brain className="w-4 h-4 text-[#06B6D4]" />
                <span className="font-mono text-[10px] text-[#06B6D4] tracking-widest">
                    LATEST ANALYSIS — {latest.weaknesses?.trade_count} TRADES OVER {latest.weaknesses?.lookback_days}d
                </span>
            </div>

            {/* Overall KPIs */}
            {overall && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    <Kpi label="WIN RATE"
                         value={overall.win_rate != null ? `${(overall.win_rate * 100).toFixed(1)}%` : "—"}
                         color={(overall.win_rate ?? 0) >= 0.5 ? "#00FF41" : "#FFB000"} />
                    <Kpi label="TOTAL P&L"
                         value={`$${overall.total_pnl_usd.toFixed(2)}`}
                         color={overall.total_pnl_usd >= 0 ? "#00FF41" : "#FF3B30"} />
                    <Kpi label="WINS / LOSSES"
                         value={`${overall.wins} / ${overall.losses}`} />
                    <Kpi label="AVG / TRADE"
                         value={overall.avg_pnl_usd != null ? `$${overall.avg_pnl_usd.toFixed(2)}` : "—"} />
                </div>
            )}

            {/* Weakness/Strength side-by-side */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2">
                <div data-testid="weaknesses-list">
                    <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest mb-2 flex items-center gap-1">
                        <TrendingDown className="w-3 h-3" /> WEAKNESSES
                    </div>
                    {weak.length === 0 ? (
                        <div className="text-[10px] text-[#52525B] font-mono">No clear weaknesses.</div>
                    ) : (
                        <div className="space-y-1">
                            {weak.slice(0, 5).map((w) => (
                                <div key={`${w.dimension}-${w.bucket}`}
                                     className="text-[11px] font-mono text-[#A1A1AA] leading-snug">
                                    · {w.summary}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
                <div data-testid="strengths-list">
                    <div className="font-mono text-[10px] text-[#00FF41] tracking-widest mb-2 flex items-center gap-1">
                        <TrendingUp className="w-3 h-3" /> STRENGTHS
                    </div>
                    {strong.length === 0 ? (
                        <div className="text-[10px] text-[#52525B] font-mono">No clear strengths yet.</div>
                    ) : (
                        <div className="space-y-1">
                            {strong.slice(0, 5).map((s) => (
                                <div key={`${s.dimension}-${s.bucket}`}
                                     className="text-[11px] font-mono text-[#A1A1AA] leading-snug">
                                    · {s.summary}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>

            {latest.baseline && (
                <div className="border-t border-[#06B6D4]/20 pt-3 font-mono text-[10px] text-[#52525B]">
                    BASELINE SCORE · {(latest.baseline.score ?? 0).toFixed(3)} · matched {latest.baseline.matched_trades} trades
                </div>
            )}
        </div>
    );
}

function ProposalCard({ p, onAccept, onDismiss }) {
    const beats = p.beats_baseline;
    const [targets, setTargets] = useState(null);
    const [target, setTarget] = useState("matching");

    useEffect(() => {
        let cancelled = false;
        api.get(`/research/proposals/${p.id}/targets`).then(r => {
            if (!cancelled) setTargets(r.data);
        }).catch(() => { /* dropdown stays in default mode */ });
        return () => { cancelled = true; };
    }, [p.id]);

    const matchingCount = targets?.matching_count ?? null;
    const totalCount = targets?.total_count ?? null;
    const hasMultiple = (totalCount || 0) > 1;

    return (
        <div className={`border p-4 ${beats ? "border-[#00FF41]/40 bg-[#00FF41]/5" : "border-[#1F1F1F]"}`}
             data-testid={`proposal-${p.id}`}>
            <div className="flex items-start justify-between gap-3 flex-wrap mb-2">
                <div className="flex-1 min-w-[200px]">
                    <div className="flex items-center gap-2 mb-1">
                        {beats && <Trophy className="w-4 h-4 text-[#00FF41]" />}
                        <span className="font-display font-bold text-base tracking-tight">{p.name}</span>
                        {p.delta_vs_baseline != null && (
                            <span className="font-mono text-[10px] tracking-widest"
                                  style={{ color: p.delta_vs_baseline > 0 ? "#00FF41" : "#FF3B30" }}>
                                {p.delta_vs_baseline > 0 ? "+" : ""}{p.delta_vs_baseline.toFixed(3)}
                            </span>
                        )}
                    </div>
                    <div className="text-xs text-[#A1A1AA] leading-snug">{p.rationale}</div>
                </div>
                <div className="flex flex-col gap-1.5 items-end">
                    {hasMultiple && (
                        <select value={target} onChange={(e) => setTarget(e.target.value)}
                            data-testid={`proposal-target-${p.id}`}
                            className="bg-[#050505] border border-[#1F1F1F] focus:border-[#FFB000] text-[10px] font-mono tracking-widest px-2 py-1.5 outline-none min-w-[180px]">
                            <option value="matching">{`Matching-symbol bots (${matchingCount ?? "…"})`}</option>
                            <option value="all">{`All bots (${totalCount ?? "…"})`}</option>
                            {(targets?.candidates || []).map(c => (
                                <option key={c.key} value={c.key}>{`Only · ${c.label}${c.matches_proposal_symbols ? " ✓" : ""}`}</option>
                            ))}
                        </select>
                    )}
                    <div className="flex gap-1.5">
                        <button onClick={onDismiss}
                            data-testid={`proposal-dismiss-${p.id}`}
                            className="px-3 py-1.5 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#FF3B30] hover:text-[#FF3B30] flex items-center gap-1">
                            <X className="w-3 h-3" /> DISMISS
                        </button>
                        <button onClick={() => onAccept(target)}
                            data-testid={`proposal-accept-${p.id}`}
                            className="px-3 py-1.5 text-[10px] font-mono tracking-widest bg-[#FFB000] hover:bg-[#E59E00] text-black flex items-center gap-1">
                            <CheckCircle2 className="w-3 h-3" /> APPLY TO BOT
                        </button>
                    </div>
                </div>
            </div>

            {/* Backtest stats + compiled diff */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-2 pt-3 border-t border-[#1F1F1F]">
                <Mini label="WIN RATE"
                      value={p.backtest_summary?.win_rate != null ? `${(p.backtest_summary.win_rate * 100).toFixed(0)}%` : "—"}
                      color={(p.backtest_summary?.win_rate ?? 0) >= 0.5 ? "#00FF41" : "#FFB000"} />
                <Mini label="P&L"
                      value={`$${(p.backtest_summary?.total_pnl_usd ?? 0).toFixed(2)}`}
                      color={(p.backtest_summary?.total_pnl_usd ?? 0) >= 0 ? "#00FF41" : "#FF3B30"} />
                <Mini label="SAMPLE" value={`${p.backtest_summary?.matched_trades ?? 0} trades`} />
                <Mini label="SCORE" value={(p.score ?? 0).toFixed(3)} color="#06B6D4" />
            </div>

            <div className="mt-3 pt-2 border-t border-[#1F1F1F] grid grid-cols-2 md:grid-cols-5 gap-2 text-[10px] font-mono">
                <Field label="symbols" value={(p.compiled.symbols || []).join(",")} />
                <Field label="session" value={p.compiled.session_preference?.toUpperCase()} />
                <Field label="risk" value={p.compiled.risk_level?.toUpperCase()} />
                <Field label="style" value={p.compiled.strategy_style} />
                <Field label="max_concur" value={p.compiled.max_concurrent_trades} />
            </div>
        </div>
    );
}

function Kpi({ label, value, color = "#E4E4E7" }) {
    return (
        <div className="border border-[#1F1F1F] p-3">
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className="font-mono font-bold text-base" style={{ color }}>{value}</div>
        </div>
    );
}

function Mini({ label, value, color = "#E4E4E7" }) {
    return (
        <div>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-0.5">{label}</div>
            <div className="font-mono text-xs" style={{ color }}>{value}</div>
        </div>
    );
}

function Field({ label, value }) {
    return (
        <div>
            <div className="text-[9px] text-[#52525B] tracking-widest">{label}</div>
            <div className="text-[#E4E4E7]">{value ?? "—"}</div>
        </div>
    );
}
