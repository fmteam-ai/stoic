import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { FlaskConical, Cpu, Scale, Play, RefreshCw, ArrowUpCircle, XCircle } from "lucide-react";
import { toast } from "sonner";

const TUNABLE_ENGINES = ["hf_scalp", "hf_scalp_fast", "range_fade", "breakout_m15"];
const SYMBOLS = ["XAUUSD", "BTCUSD", "EURUSD", "GBPUSD"];
const mono10 = "font-mono text-[10px]";

function Panel({ title, subtitle, icon: Icon, children, testid }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid={testid}>
            <div className={`${mono10} text-[#52525B] tracking-widest mb-1 flex items-center gap-2`}>
                <Icon size={12} /> {title}
            </div>
            {subtitle && <div className={`${mono10} text-[#3F3F46] mb-3`}>{subtitle}</div>}
            {children}
        </div>
    );
}

function ParamsDiff({ params, defaults }) {
    return (
        <div className="flex flex-wrap gap-2">
            {Object.entries(params || {}).map(([k, v]) => {
                const d = defaults?.[k];
                const changed = d !== undefined && Number(d) !== Number(v);
                return (
                    <span key={k} className={`${mono10} px-2 py-0.5 border ${changed ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA]"}`}>
                        {k}={v}{changed && <span className="text-[#52525B]"> (was {d})</span>}
                    </span>
                );
            })}
        </div>
    );
}

export function TuningPanel() {
    const [engine, setEngine] = useState("hf_scalp");
    const [symbol, setSymbol] = useState("XAUUSD");
    const [running, setRunning] = useState(false);
    const [proposals, setProposals] = useState([]);

    const load = useCallback(() => {
        api.get("/quant/bayes/proposals").then(({ data }) => setProposals(data.proposals || [])).catch(() => {});
    }, []);
    useEffect(() => { load(); }, [load]);

    const run = async () => {
        setRunning(true);
        try {
            const { data } = await api.post("/quant/bayes/run", { engine, symbol });
            toast.success(`Bayes run done: best score ${data.best?.score} vs default ${data.default?.score}`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setRunning(false); }
    };

    const toShadow = async (p) => {
        try {
            await api.post("/shadow/models/register", { proposal_id: p._id });
            toast.success(`${p.version} locked into the Shadow Lab`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    return (
        <Panel title="BAYESIAN TUNING · GP-EI PARAMETER SEARCH" icon={FlaskConical} testid="tuning-panel"
            subtitle="Walk-forward replay of real M15 bars — runs automatically every night for your active engines; promising results are queued straight into the Shadow Lab. Proposals never touch live config without shadow proof.">
            <div className="flex flex-wrap items-center gap-2 mb-3">
                <select value={engine} onChange={(e) => setEngine(e.target.value)} data-testid="tuning-engine-select"
                    className={`${mono10} bg-[#111111] border border-[#1F1F1F] text-white px-2 py-1.5`}>
                    {TUNABLE_ENGINES.map((e) => <option key={e} value={e}>{e}</option>)}
                </select>
                <select value={symbol} onChange={(e) => setSymbol(e.target.value)} data-testid="tuning-symbol-select"
                    className={`${mono10} bg-[#111111] border border-[#1F1F1F] text-white px-2 py-1.5`}>
                    {SYMBOLS.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
                <button onClick={run} disabled={running} data-testid="tuning-run-btn"
                    className={`${mono10} tracking-widest px-3 py-1.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-50 flex items-center gap-1.5`}>
                    {running ? <RefreshCw size={11} className="animate-spin" /> : <Play size={11} />}
                    {running ? "OPTIMIZING…" : "RUN OPTIMIZATION"}
                </button>
            </div>
            <div className="space-y-2" data-testid="tuning-proposals">
                {proposals.map((p) => (
                    <div key={p._id} className="border-t border-[#141414] pt-2" data-testid={`proposal-${p.version}`}>
                        <div className="flex flex-wrap items-center gap-3">
                            <span className={`${mono10} text-white`}>{p.engine} · {p.symbol}</span>
                            <span className={`${mono10} text-[#52525B]`}>{p.version}</span>
                            <span className={`${mono10} ${p.improvement > 0 ? "text-[#00FF41]" : "text-[#A1A1AA]"}`}>
                                score {p.best?.score} vs default {p.default?.score} ({p.improvement >= 0 ? "+" : ""}{p.improvement})
                            </span>
                            <span className={`${mono10} text-[#52525B]`}>{p.best?.trades} sim trades · {p.days_span}d of bars</span>
                            <span className="ml-auto flex items-center gap-2">
                                <span className={`${mono10} px-2 py-0.5 border ${p.status === "shadow_testing" ? "text-[#FFD700] border-[#FFD700]/40" : "text-[#A1A1AA] border-[#1F1F1F]"}`}>
                                    {(p.status || "proposed").toUpperCase()}
                                </span>
                                {p.status !== "shadow_testing" && (
                                    <button onClick={() => toShadow(p)} data-testid={`proposal-shadow-btn-${p.version}`}
                                        className={`${mono10} tracking-widest px-2 py-0.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10`}>
                                        SEND TO SHADOW
                                    </button>
                                )}
                            </span>
                        </div>
                        <div className="mt-1.5"><ParamsDiff params={p.params} defaults={p.default_params} /></div>
                    </div>
                ))}
                {!proposals.length && <div className={`${mono10} text-[#52525B]`}>No tuning proposals yet — run an optimization above.</div>}
            </div>
        </Panel>
    );
}

export function AllocatorPanel() {
    const [data, setData] = useState(null);
    const load = useCallback(() => {
        api.get("/quant/allocator").then(({ data }) => setData(data)).catch(() => {});
    }, []);
    useEffect(() => { load(); }, [load]);

    const setMode = async (mode) => {
        if (mode === "enforce" && !window.confirm("ENFORCE mode: proven-loser engines get their lot sizes cut automatically (down to 0.25×). Continue?")) return;
        try {
            const { data: res } = await api.post("/quant/allocator/mode", { mode });
            toast.success(`Allocator mode → ${res.mode.toUpperCase()} (${res.configs_updated} bot config(s))`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const rows = data?.allocations || [];
    const mode = data?.mode || "advisory";
    return (
        <Panel title="RL CAPITAL ALLOCATOR · WHO DESERVES THE BUDGET" icon={Scale} testid="allocator-panel"
            subtitle={`Shrink-only capital weights from P(edge>0) over the last ${data?.params?.lookback_days ?? 60} days`}>
            <div className="flex items-center gap-2 mb-3" data-testid="allocator-mode-row">
                <span className={`${mono10} text-[#52525B] tracking-widest`}>MODE</span>
                {["off", "advisory", "enforce"].map((m) => (
                    <button key={m} onClick={() => setMode(m)} data-testid={`allocator-mode-${m}`}
                        className={`${mono10} tracking-widest px-2.5 py-1 border transition-colors ${
                            mode === m
                                ? (m === "enforce" ? "border-[#FF3B30] text-[#FF3B30]" : "border-[#00FF41] text-[#00FF41]")
                                : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"}`}>
                        {m.toUpperCase()}
                    </button>
                ))}
                {mode === "enforce" && (
                    <span className={`${mono10} text-[#FF3B30]`}>· weights below 1.0× actively cut lot sizes</span>
                )}
            </div>
            {rows.length ? rows.map((a) => (
                <div key={a.scope} className="flex flex-wrap items-center gap-3 py-1.5 border-t border-[#141414]" data-testid={`allocator-${a.scope}`}>
                    <div className={`w-36 ${mono10} text-[#A1A1AA] truncate`}>{a.scope}</div>
                    <div className={`${mono10} text-[#52525B]`}>{a.n} trades · mean {a.mean_pnl >= 0 ? "+$" : "-$"}{Math.abs(a.mean_pnl).toFixed(2)}</div>
                    <div className={`${mono10} text-[#52525B]`}>P(edge&gt;0) {(a.p_positive * 100).toFixed(0)}%</div>
                    <div className="flex-1 h-2 bg-[#111111] min-w-[80px]">
                        <div className={`h-2 ${a.weight >= 1 ? "bg-[#00FF41]/60" : "bg-[#FFD700]/60"}`} style={{ width: `${a.weight * 100}%` }} />
                    </div>
                    <div className={`w-12 text-right ${mono10} ${a.weight >= 1 ? "text-[#00FF41]" : "text-[#FFD700]"}`}>{a.weight}×</div>
                </div>
            )) : <div className={`${mono10} text-[#52525B]`}>No closed bot trades in the lookback window yet.</div>}
        </Panel>
    );
}

function PromotionChecks({ promotion }) {
    return (
        <div className="flex flex-wrap gap-1.5 mt-1.5">
            {(promotion?.checks || []).map((c) => (
                <span key={c.name} className={`${mono10} px-1.5 py-0.5 border ${c.passed ? "text-[#00FF41] border-[#00FF41]/40" : "text-[#52525B] border-[#1F1F1F]"}`}>
                    {c.passed ? "✓" : "•"} {c.name} ({String(c.value)})
                </span>
            ))}
        </div>
    );
}

export function ShadowLabPanel() {
    const [data, setData] = useState(null);
    const [recon, setRecon] = useState(null);
    const [loading, setLoading] = useState(true);

    const load = useCallback(() => {
        setLoading(true);
        api.get("/shadow/models").then(({ data }) => setData(data)).catch(() => {}).finally(() => setLoading(false));
        api.get("/shadow/reconciliation?days=14").then(({ data }) => setRecon(data)).catch(() => {});
    }, []);
    useEffect(() => { load(); }, [load]);

    const promote = async (m) => {
        if (!window.confirm(`Promote ${m.version} to LIVE engine params on all active bots?`)) return;
        try {
            const { data: res } = await api.post(`/shadow/models/${m._id}/promote`);
            toast.success(`${res.version} promoted — ${res.configs_updated} bot config(s) updated`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
    };
    const retire = async (m) => {
        try {
            await api.post(`/shadow/models/${m._id}/retire`);
            toast.success(`${m.version} retired`);
            load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const Row = ({ m, live }) => {
        const ch = m.challenger_state || {};
        const bl = m.baseline_state || {};
        return (
            <div className="border-t border-[#141414] py-2" data-testid={`shadow-model-${m.version}`}>
                <div className="flex flex-wrap items-center gap-3">
                    <span className={`${mono10} text-white`}>{m.engine} · {m.symbol}</span>
                    <span className={`${mono10} text-[#52525B]`}>{m.version} · locked {new Date(m.registered_at).toLocaleDateString()}</span>
                    <span className={`${mono10} ${ch.total_r > 0 ? "text-[#00FF41]" : "text-[#A1A1AA]"}`}>
                        challenger {ch.trades || 0}T · {(ch.total_r || 0).toFixed(1)}R
                    </span>
                    <span className={`${mono10} text-[#52525B]`}>vs production {(bl.total_r || 0).toFixed(1)}R</span>
                    <span className="ml-auto flex items-center gap-2">
                        <span className={`${mono10} px-2 py-0.5 border ${m.status === "promoted" ? "text-[#00FF41] border-[#00FF41]/40" : m.status === "retired" ? "text-[#52525B] border-[#1F1F1F]" : "text-[#FFD700] border-[#FFD700]/40"}`}>
                            {m.status.toUpperCase()}
                        </span>
                        {live && (
                            <>
                                <button onClick={() => promote(m)} disabled={!m.promotion?.ready} data-testid={`shadow-promote-${m.version}`}
                                    className={`${mono10} tracking-widest px-2 py-0.5 border flex items-center gap-1 ${m.promotion?.ready ? "border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#3F3F46] cursor-not-allowed"}`}>
                                    <ArrowUpCircle size={11} /> PROMOTE
                                </button>
                                <button onClick={() => retire(m)} data-testid={`shadow-retire-${m.version}`}
                                    className={`${mono10} tracking-widest px-2 py-0.5 border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#FF3B30]/40 hover:text-[#FF3B30] flex items-center gap-1`}>
                                    <XCircle size={11} /> RETIRE
                                </button>
                            </>
                        )}
                    </span>
                </div>
                {live && <PromotionChecks promotion={m.promotion} />}
            </div>
        );
    };

    return (
        <Panel title="SHADOW LAB · LOCKED CHALLENGERS vs PRODUCTION" icon={Cpu} testid="shadow-lab-panel"
            subtitle="Challenger parameter sets replayed continuously on live bars with a locked version. Promotion requires every P3 gate — then human approval.">
            {loading ? (
                <div className={`${mono10} text-[#52525B] flex items-center gap-2`}><RefreshCw size={11} className="animate-spin" /> Evaluating challengers…</div>
            ) : (
                <>
                    <div data-testid="shadow-testing-list">
                        {(data?.testing || []).map((m) => <Row key={m._id} m={m} live />)}
                        {!data?.testing?.length && <div className={`${mono10} text-[#52525B]`}>No challengers in shadow — send a tuning proposal here.</div>}
                    </div>
                    {!!data?.finished?.length && (
                        <div className="mt-3">
                            <div className={`${mono10} text-[#52525B] tracking-widest mb-1`}>HISTORY</div>
                            {data.finished.map((m) => <Row key={m._id} m={m} />)}
                        </div>
                    )}
                    {recon && (
                        <div className="mt-4 border-t border-[#1F1F1F] pt-3" data-testid="reconciliation-card">
                            <div className={`${mono10} text-[#52525B] tracking-widest mb-1`}>BROKER-FILL RECONCILIATION · SIMULATOR HONESTY (14D)</div>
                            <div className="flex flex-wrap gap-4">
                                <span className={`${mono10} text-[#A1A1AA]`}>{recon.resolved ?? 0} fills replayed</span>
                                <span className={`${mono10} ${recon.match_rate_pct >= 80 ? "text-[#00FF41]" : "text-[#FFD700]"}`}>
                                    match rate {recon.match_rate_pct != null ? `${recon.match_rate_pct}%` : "—"}
                                </span>
                                <span className={`${mono10} text-[#A1A1AA]`}>avg |R gap| {recon.avg_abs_r_gap ?? "—"}</span>
                                <span className={`${mono10} text-[#52525B]`}>{recon.mismatches?.length || 0} mismatches</span>
                            </div>
                        </div>
                    )}
                </>
            )}
        </Panel>
    );
}
