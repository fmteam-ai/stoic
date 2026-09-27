import { useEffect, useState, useCallback, Fragment } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ScalpExecutions } from "@/components/ScalpExecutions";
import { ScalpReview } from "@/components/ScalpReview";
import { DriftGauge } from "@/components/DriftGauge";
import { RefreshCw, ShieldAlert, X } from "lucide-react";
import { toast } from "sonner";

const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));

// iter-140 · Quote-age tiers: <500 excellent · <1000 acceptable · <2000 caution · beyond reject
const quoteAge = (ms) => {
    if (ms == null || ms >= 86400000) return { label: "—", color: "#52525B", tier: "OFFLINE" };
    if (ms < 500) return { label: `${ms}ms`, color: "#00FF41", tier: "EXCELLENT" };
    if (ms < 1000) return { label: `${ms}ms`, color: "#A1A1AA", tier: "ACCEPTABLE" };
    if (ms < 2000) return { label: `${ms}ms`, color: "#FFB000", tier: "CAUTION" };
    return { label: `${ms}ms`, color: "#FF3B30", tier: "REJECT" };
};

const REGIME_COLORS = {
    TRENDING_UP: "#00FF41", TRENDING_DOWN: "#00FF41", FLAT: "#A1A1AA",
    RANGE: "#A1A1AA", VOLATILITY_SHOCK: "#FFB000", UNKNOWN: "#FF3B30",
};

const STAGE_LABELS = {
    pre_submit_quote_invalid: "Execution Gate · quote", pre_submit_drift: "Execution Gate · drift",
    pre_submit_spread: "Execution Gate · spread", pre_submit_reforecast: "Forecast Revalidation",
    pre_submit_edge_revalidation: "EV Revalidation", pre_submit_execution_quality: "Execution Quality",
    pre_submit_adaptive_edge: "Adaptive Edge", low_fill_probability_ev: "Fill Probability EV",
    lease_lost_before_submit: "Submission Lease", pre_submit_broker_state: "Broker State",
    pre_submit_risk_unknown: "Risk Engine", pre_submit_resize: "Risk Engine · resize",
    combined_decision_quality: "Decision Quality", pre_submit_exposure: "Exposure Cap",
    portfolio_risk: "Portfolio Risk", submission_capacity: "Capacity",
    capacity_integrity: "Capacity Integrity", submission_capacity_broker: "Broker Capacity",
    pre_submit_broker_constraints: "Broker Constraints",
};

// Which subsystem stopped a rejected candidate.
function rejectStage(d) {
    if (d.reject_stage) return STAGE_LABELS[d.reject_stage] || d.reject_stage;
    const g = d.gates || {};
    if (g.permission && g.permission.ok === false) {
        const rs = (g.permission.reasons || []).join(" ");
        if (/news/i.test(rs)) return "News Filter";
        if (/session/i.test(rs)) return "Session Window";
        return "Regime Filter";
    }
    if (g.edge && g.edge.ok === false) return "Expected Value";
    if (g.risk && g.risk.ok === false) return "Risk Engine";
    if (g.final && g.final.ok === false) {
        const failed = Object.entries(g.final.checks || {}).filter(([, v]) => !v).map(([k]) => k);
        return `Execution Gate${failed.length ? ` · ${failed.join(",")}` : ""}`;
    }
    return "—";
}

// Ordered pipeline ladder for one decision: [{name, status: pass|fail|info, detail}]
function pipelineStages(d) {
    const g = d.gates || {};
    const stages = [{ name: "Signal", status: "pass", detail: d.setup ? `${d.setup.preset || "setup"} · impulse ${fmt(d.setup.impulse_pips, 1)}p` : "candidate" }];
    if (g.permission) {
        const rs = (g.permission.reasons || []).join(", ");
        stages.push({ name: `Regime/News (${g.permission.regime || "?"})`, status: g.permission.ok ? "pass" : "fail", detail: rs || "permitted" });
    }
    if (d.forecast) stages.push({ name: "Forecast", status: "pass", detail: `p(target) ${fmt(d.forecast.p_target_before_stop, 2)} · move +${fmt(d.forecast.expected_favorable_move_pips, 2)}p / −${fmt(d.forecast.expected_adverse_move_pips, 2)}p` });
    if (g.edge) stages.push({ name: "Expected Value", status: g.edge.ok ? "pass" : "fail", detail: g.edge.reason || `net edge ${fmt(g.edge.net_edge_pips, 2)}p vs cost ${fmt(g.edge.cost_pips, 2)}p` });
    if (g.risk) stages.push({ name: "Risk Engine", status: g.risk.ok ? "pass" : "fail", detail: g.risk.reason || `lot ${fmt(g.risk.lot, 2)} · risk $${fmt(g.risk.actual_risk_usd, 2)}` });
    if (d.quality) stages.push({ name: "Quality Score", status: d.quality.gate_enabled ? (d.quality.score >= d.quality.gate_min ? "pass" : "fail") : "info", detail: `${d.quality.score}/100${d.quality.gate_enabled ? ` (min ${d.quality.gate_min})` : " · advisory"}` });
    if (g.final) {
        const failed = Object.entries(g.final.checks || {}).filter(([, v]) => !v).map(([k]) => k);
        stages.push({ name: "Execution Gate", status: g.final.ok ? "pass" : "fail", detail: failed.length ? `failed: ${failed.join(", ")}` : "all checks pass" });
    }
    stages.push({ name: "Broker", status: d.verdict === "rejected" ? "skip" : "pass", detail: d.submission_status || (d.verdict === "rejected" ? "not submitted" : d.verdict) });
    return stages;
}

function Chip({ ok, label, testid }) {
    return (
        <span data-testid={testid}
              className={`px-2 py-0.5 text-xs border ${ok
                  ? "text-[#00FF41] border-[#00FF41]/40"
                  : "text-[#FF3B30] border-[#FF3B30]/40"}`}>
            {label}
        </span>
    );
}

function Metric({ label, value, testid }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3" data-testid={testid}>
            <div className="text-[10px] uppercase tracking-widest text-[#52525B]">{label}</div>
            <div className="text-lg text-[#E4E4E7] font-mono mt-1">{value ?? "—"}</div>
        </div>
    );
}

export default function Scalp() {
    const [accounts, setAccounts] = useState([]);
    const [accountId, setAccountId] = useState("");
    const [runners, setRunners] = useState([]);
    const [metrics, setMetrics] = useState(null);
    const [decisions, setDecisions] = useState([]);
    const [expanded, setExpanded] = useState(null);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [sess, setSess] = useState(null);
    const [sessDraft, setSessDraft] = useState({ start: 7, end: 20 });
    const [sessSaving, setSessSaving] = useState(false);

    const loadSession = useCallback(async () => {
        try {
            const { data } = await api.get("/scalp/session-window?symbol=EURUSD");
            setSess(data);
            setSessDraft({ start: data.start_utc, end: data.end_utc });
        } catch { /* silent */ }
    }, []);
    useEffect(() => { loadSession(); }, [loadSession]);

    const saveSession = async (reset = false) => {
        setSessSaving(true);
        try {
            await api.post("/scalp/session-window", reset
                ? { symbol: "EURUSD", reset: true }
                : { symbol: "EURUSD", start_utc: Number(sessDraft.start), end_utc: Number(sessDraft.end) });
            toast.success(reset
                ? "Session window reset to instrument default"
                : `Session window saved — ${String(sessDraft.start).padStart(2, "0")}:00–${String(sessDraft.end).padStart(2, "0")}:00 UTC`);
            await loadSession();
            load();
        } catch (e) {
            toast.error("Could not save session window", { description: formatApiError(e) });
        } finally {
            setSessSaving(false);
        }
    };

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [st, mt, dc] = await Promise.all([
                api.get("/scalp/status"),
                api.get("/scalp/metrics?symbol=EURUSD"),
                api.get("/scalp/decisions?limit=25&symbol=EURUSD"),
            ]);
            setRunners(st.data.runners || []);
            setMetrics(mt.data);
            setDecisions(dc.data.decisions || []);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        api.get("/accounts").then((r) => {
            setAccounts(r.data || []);
            if (r.data?.length) setAccountId((cur) => cur || r.data[0].id);
        }).catch(() => {});
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 15000);
        return () => clearInterval(t);
    }, [load]);

    const acctName = (id) => {
        const ac = accounts.find((x) => x.id === id);
        return ac ? (ac.label || ac.account_number) : (id || "").slice(-6);
    };

    const setConfig = async (enabled, mode) => {
        if (!accountId) return toast.error("Select an account first");
        if (mode === "demo_live"
            && !window.confirm("DEMO LIVE mode sends REAL orders to the broker on this account. Continue?"))
            return;
        setSaving(true);
        try {
            await api.post("/scalp/config", {
                account_id: accountId, symbol: "EURUSD",
                enabled, mode, confirm_live: mode === "demo_live",
            });
            toast.success(enabled
                ? `Scalp ${mode.toUpperCase()} enabled on ${acctName(accountId)}`
                : `Scalp disabled on ${acctName(accountId)}`);
            load();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setSaving(false);
        }
    };

    const removeRunner = async (accId, symbol) => {
        if (!window.confirm(`Remove ${symbol} runner on ${acctName(accId)} from the Scalp page? Incoming ticks from that terminal will be ignored until you re-enable it.`))
            return;
        setSaving(true);
        try {
            await api.delete(`/scalp/config?account_id=${accId}&symbol=${symbol}`);
            toast.success(`Removed ${symbol} runner on ${acctName(accId)}`);
            load();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setSaving(false);
        }
    };

    const a = metrics?.alpha || {};
    return (
        <AppLayout>
            <PageHeader
                title="Scalp Fast Path"
                subtitle="EURUSD micro-pullback continuation · tick-driven · shadow-first"
                action={
                    <button onClick={load} data-testid="scalp-refresh-btn"
                            className="p-2 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white">
                        <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
                    </button>
                }
            />
            <div className="p-4 md:p-8">

            {/* Config */}
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 mb-6" data-testid="scalp-config-card">
                <div className="flex flex-wrap items-center gap-3">
                    <select value={accountId} onChange={(e) => setAccountId(e.target.value)}
                            data-testid="scalp-account-select"
                            className="bg-[#0A0A0A] border border-[#1F1F1F] text-[#E4E4E7] text-sm px-3 py-2">
                        {accounts.map((ac) => (
                            <option key={ac.id} value={ac.id}>{ac.label || ac.account_number}</option>
                        ))}
                    </select>
                    <button disabled={saving} onClick={() => setConfig(true, "shadow")}
                            data-testid="scalp-enable-shadow-btn"
                            className="px-4 py-2 text-sm border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10">
                        ENABLE SHADOW
                    </button>
                    <button disabled={saving} onClick={() => setConfig(true, "demo_live")}
                            data-testid="scalp-enable-demolive-btn"
                            className="px-4 py-2 text-sm border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10">
                        <ShieldAlert size={12} className="inline mr-1" />DEMO LIVE
                    </button>
                    <button disabled={saving} onClick={() => setConfig(false, "shadow")}
                            data-testid="scalp-disable-btn"
                            className="px-4 py-2 text-sm border border-[#1F1F1F] text-[#A1A1AA] hover:text-white">
                        DISABLE
                    </button>
                </div>
                <p className="text-xs text-[#52525B] mt-3">
                    Requires EA v1.48+ attached with <span className="font-mono text-[#A1A1AA]">TickStreamEnabled=true, TickStreamSymbol=EURUSD</span>.
                    Shadow mode runs the full pipeline and logs decisions without sending orders.
                </p>
                {sess && (
                    <div className="flex flex-wrap items-center gap-2 mt-3 pt-3 border-t border-[#1F1F1F]"
                         data-testid="scalp-session-window-editor">
                        <span className="text-[10px] font-mono tracking-widest text-[#52525B]">TRADING SESSION (UTC)</span>
                        <input type="number" min={0} max={23} value={sessDraft.start}
                               onChange={(e) => setSessDraft(d => ({ ...d, start: e.target.value }))}
                               data-testid="scalp-session-start-input"
                               className="w-16 bg-[#0A0A0A] border border-[#1F1F1F] text-[#E4E4E7] text-sm px-2 py-1.5 font-mono" />
                        <span className="text-[#52525B] text-xs">to</span>
                        <input type="number" min={1} max={24} value={sessDraft.end}
                               onChange={(e) => setSessDraft(d => ({ ...d, end: e.target.value }))}
                               data-testid="scalp-session-end-input"
                               className="w-16 bg-[#0A0A0A] border border-[#1F1F1F] text-[#E4E4E7] text-sm px-2 py-1.5 font-mono" />
                        <button disabled={sessSaving} onClick={() => saveSession(false)}
                                data-testid="scalp-session-save-btn"
                                className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#FFD700]/40 text-[#FFD700] hover:bg-[#FFD700]/10 disabled:opacity-50">
                            {sessSaving ? "SAVING…" : "SAVE WINDOW"}
                        </button>
                        {sess.override && (
                            <button disabled={sessSaving} onClick={() => saveSession(true)}
                                    data-testid="scalp-session-reset-btn"
                                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:text-white disabled:opacity-50">
                                RESET TO DEFAULT ({String(sess.default_start_utc).padStart(2, "0")}–{String(sess.default_end_utc).padStart(2, "0")})
                            </button>
                        )}
                        <span className="text-[10px] text-[#52525B]">
                            Candidates are only evaluated inside this window — ticks keep flowing outside it.
                        </span>
                    </div>
                )}
            </div>

            {/* Runners */}
            <div className="grid gap-4 mb-6">
                {runners.length === 0 && (
                    <div className="text-sm text-[#52525B]" data-testid="scalp-no-runners">
                        No tick stream received yet — attach EA v1.48 with tick streaming enabled.
                    </div>
                )}
                {runners.map((r) => (
                    <div key={`${r.account_id}:${r.symbol}`}
                         className="border border-[#1F1F1F] bg-[#0A0A0A] p-4"
                         data-testid={`scalp-runner-${r.symbol}`}>
                        <div className="flex flex-wrap items-center gap-2 mb-3">
                            <span className="text-[#E4E4E7] font-mono">{r.symbol}</span>
                            <span className="text-xs text-[#A1A1AA] border border-[#1F1F1F] px-2 py-0.5"
                                  data-testid="scalp-runner-account">
                                {acctName(r.account_id)}
                            </span>
                            <Chip ok={r.enabled} label={r.enabled ? `ON · ${r.mode.toUpperCase()}` : "OFF"}
                                  testid="scalp-runner-enabled" />
                            <Chip ok={r.health?.status === "OK"} label={`HEALTH ${r.health?.status}`}
                                  testid="scalp-runner-health" />
                            {(() => {
                                const reg = r.permissions?.regime || "?";
                                const c = REGIME_COLORS[reg] || "#FF3B30";
                                return (
                                    <span data-testid="scalp-runner-regime"
                                          title={r.permissions?.regime_detail
                                              ? `slope ${r.permissions.regime_detail.ema_slope_pips}p · range ${r.permissions.regime_detail.range_pips}p · ER ${r.permissions.regime_detail.efficiency_ratio} · conf ${r.permissions.regime_detail.confidence}`
                                              : (r.permissions?.regime_reason || "")}
                                          className="px-2 py-0.5 text-xs border"
                                          style={{ color: c, borderColor: `${c}66` }}>
                                        REGIME {reg}
                                        {reg === "UNKNOWN" && r.permissions?.regime_reason
                                            ? ` · ${r.permissions.regime_reason}` : ""}
                                    </span>
                                );
                            })()}
                            <button disabled={saving}
                                    onClick={() => removeRunner(r.account_id, r.symbol)}
                                    data-testid="scalp-runner-remove-btn"
                                    title="Remove this runner from the page"
                                    className="ml-auto flex items-center gap-1 px-2 py-0.5 text-xs border border-[#1F1F1F] text-[#52525B] hover:text-[#FF3B30] hover:border-[#FF3B30]/40">
                                <X size={12} /> REMOVE
                            </button>
                        </div>
                        <div className="grid grid-cols-2 md:grid-cols-7 gap-2 text-xs text-[#A1A1AA]">
                            <div>Ticks <span className="text-[#E4E4E7] font-mono">{r.counters?.ticks}</span></div>
                            <div>Candidates <span className="text-[#E4E4E7] font-mono">{r.counters?.candidates}</span></div>
                            <div>Shadow <span className="text-[#E4E4E7] font-mono">{r.counters?.shadow_trades}</span></div>
                            <div>Live <span className="text-[#E4E4E7] font-mono">{r.counters?.live_trades}</span></div>
                            <div>Spread <span className="text-[#E4E4E7] font-mono">{fmt(r.spread_pips, 2)}p</span></div>
                            <DriftGauge residualMs={r.clock_drift_residual_ms} offsetMs={r.clock_drift_ms}
                                        samples={r.clock_drift_samples} limitMs={r.clock_drift_limit_ms}
                                        testid={`scalp-runner-drift-${r.symbol}`} />
                            {(() => {
                                const qa = quoteAge(r.quote_age_ms);
                                return (
                                    <div title={`Tiers: <500ms excellent · <1s acceptable · <2s caution · beyond reject`}>
                                        Quote age <span className="font-mono" style={{ color: qa.color }}>{qa.label}</span>
                                        <span className="ml-1 text-[9px] tracking-widest" style={{ color: qa.color }}>{qa.tier !== "OFFLINE" ? qa.tier : ""}</span>
                                    </div>
                                );
                            })()}
                        </div>
                        {quoteAge(r.quote_age_ms).tier === "OFFLINE" && (() => {
                            const ing = r.ingress;
                            const term = r.terminal || {};
                            const hbFresh = term.connected === true;
                            const batchAgeS = ing?.last_batch_at
                                ? Math.round((Date.now() - new Date(ing.last_batch_at).getTime()) / 1000)
                                : null;
                            let title, detail;
                            if (ing?.last_status === "rejected") {
                                title = "TICK STREAM BLOCKED";
                                detail = `Batches ARE reaching the server (last ${batchAgeS}s ago) but are rejected: ${ing.last_reason}. This usually clears itself within a minute; if it persists, the account is being processed by another worker.`;
                            } else if (ing?.last_status === "ignored") {
                                title = "TICK STREAM IGNORED";
                                detail = `Batches ARE reaching the server (last ${batchAgeS}s ago) but are ignored: ${ing.last_reason}.`;
                            } else if (ing && batchAgeS !== null && batchAgeS < 120) {
                                title = "TICKS ARRIVING BUT NOT ACCEPTED";
                                detail = `A batch reached the server ${batchAgeS}s ago but no valid tick was accepted — quotes may be duplicated/out-of-order (market closed?).`;
                            } else if (ing && batchAgeS !== null) {
                                title = "TICK STREAM STOPPED";
                                detail = `This terminal last delivered ticks ${batchAgeS > 3600 ? `${Math.round(batchAgeS / 3600)}h` : `${Math.round(batchAgeS / 60)}min`} ago${hbFresh ? " while its heartbeat is still fresh — the EA is running but its tick timer stalled; re-attach the EA or restart the terminal" : " and its heartbeat is also stale — start the MT5 terminal/VPS first"}.`;
                            } else if (hbFresh) {
                                title = "TICK STREAM NEVER STARTED";
                                detail = `The EA${term.ea_version ? ` v${term.ea_version}` : ""} is heartbeating but has NEVER sent a tick batch. In the EA inputs (F7): TickStreamEnabled=true, TickStreamSymbol=${r.symbol}. Tick streaming needs EA v1.48+ — if yours is older, re-install from Accounts → Quick Install.`;
                            } else {
                                title = "TERMINAL OFFLINE";
                                detail = `No heartbeat from this terminal${term.heartbeat_age_seconds != null ? ` for ${Math.round(term.heartbeat_age_seconds / 60)}min` : ""} — start MT5 (or the VPS) with the EA attached and AutoTrading ON, then ticks resume automatically.`;
                            }
                            return (
                                <div className="text-xs text-[#FFB000] mt-2 border border-[#FFB000]/30 bg-[#FFB000]/5 px-2 py-1.5"
                                     data-testid="scalp-tick-offline">
                                    <span className="font-bold tracking-widest font-mono">{title}</span> — {detail}
                                </div>
                            );
                        })()}
                        {(() => {
                            const s = r.permissions?.session;
                            if (!s || s.open) return null;
                            const h = Math.floor((s.opens_in_minutes || 0) / 60);
                            const m = (s.opens_in_minutes || 0) % 60;
                            return (
                                <div className="text-xs text-[#FFB000] mt-2 border border-[#FFB000]/30 bg-[#FFB000]/5 px-2 py-1.5 font-mono"
                                     data-testid="scalp-session-closed">
                                    SESSION CLOSED — trading window {String(s.start_utc).padStart(2, "0")}:00–{String(s.end_utc).padStart(2, "0")}:00 UTC
                                    · opens in {h > 0 ? `${h}h ` : ""}{m}m. Ticks keep flowing; candidate evaluation resumes automatically.
                                </div>
                            );
                        })()}
                        {(r.health?.reasons?.length > 0 || r.permissions?.reasons?.length > 0) && (
                            <div className="text-xs text-[#FF9F0A] mt-2" data-testid="scalp-runner-reasons">
                                {[...(r.health?.reasons || []), ...(r.permissions?.reasons || [])].join(" · ")}
                            </div>
                        )}
                        {r.permissions?.news && (
                            <div className="mt-3 border border-[#1F1F1F] bg-[#050505] p-2.5 grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px] text-[#A1A1AA]"
                                 data-testid="scalp-news-diagnostics">
                                <div>
                                    <div className="text-[9px] uppercase tracking-widest text-[#52525B]">News provider</div>
                                    <div className="font-mono text-[#E4E4E7]">{r.permissions.news.provider || "—"}</div>
                                </div>
                                <div>
                                    <div className="text-[9px] uppercase tracking-widest text-[#52525B]">Status</div>
                                    <span className="font-mono" style={{ color: r.permissions.news.status === "OK" ? "#00FF41" : r.permissions.news.status === "DEGRADED" ? "#FFB000" : "#FF3B30" }}>
                                        {r.permissions.news.status}
                                    </span>
                                </div>
                                <div>
                                    <div className="text-[9px] uppercase tracking-widest text-[#52525B]">Last update</div>
                                    <div className="font-mono text-[#E4E4E7]">
                                        {r.permissions.news.last_fetch_age_min != null ? `${r.permissions.news.last_fetch_age_min}min ago` : "—"}
                                    </div>
                                </div>
                                <div>
                                    <div className="text-[9px] uppercase tracking-widest text-[#52525B]">Next high impact</div>
                                    <div className="font-mono text-[#E4E4E7] truncate"
                                         title={r.permissions.news.next_high_impact?.title || ""}>
                                        {r.permissions.news.next_high_impact
                                            ? `${r.permissions.news.next_high_impact.title} · ${Math.round(r.permissions.news.next_high_impact.minutes_away)}min`
                                            : "none in 24h"}
                                    </div>
                                </div>
                                <div>
                                    <div className="text-[9px] uppercase tracking-widest text-[#52525B]">Blackout · TZ</div>
                                    <div className="font-mono text-[#E4E4E7]">±{r.permissions.news.blackout_minutes}min · UTC</div>
                                </div>
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {/* Metrics */}
            <h2 className="text-sm text-[#A1A1AA] uppercase tracking-widest mb-2">Shadow Performance (labeled outcomes)</h2>
            {metrics?.n != null && metrics.n < 100 && (
                <div className="text-xs text-[#FFB000] border border-[#FFB000]/30 bg-[#FFB000]/5 px-3 py-2 mb-2"
                     data-testid="scalp-low-sample-note">
                    LOW SAMPLE — {metrics.n} labeled outcome{metrics.n === 1 ? "" : "s"}. Statistics below are not yet
                    meaningful; withhold judgement until ≥100 (ideally several hundred) samples accumulate.
                </div>
            )}
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2 mb-6">
                <Metric label="Samples" value={metrics?.n} testid="scalp-metric-n" />
                <Metric label="Target-first %" value={a.target_before_stop_rate != null ? `${(a.target_before_stop_rate * 100).toFixed(1)}%` : "—"} testid="scalp-metric-tbs" />
                <Metric label="Net exp (pips)" value={fmt(a.net_expectancy_pips, 3)} testid="scalp-metric-netexp" />
                <Metric label="Profit factor" value={fmt(a.profit_factor)} testid="scalp-metric-pf" />
                <Metric label="Avg win / loss" value={a.avg_winner_pips != null ? `${fmt(a.avg_winner_pips)} / ${fmt(a.avg_loser_pips)}` : "—"} testid="scalp-metric-winloss" />
                <Metric label="Timeout rate" value={a.timeout_rate != null ? `${(a.timeout_rate * 100).toFixed(0)}%` : "—"} testid="scalp-metric-timeout" />
                <Metric label="Model OOS AUC" value={metrics?.model ? `${fmt(metrics.model.oos_auc, 3)}${metrics.model.usable ? " ✓" : " (shelved)"}` : "not trained"} testid="scalp-metric-auc" />
            </div>

            {/* Decisions */}
            <h2 className="text-sm text-[#A1A1AA] uppercase tracking-widest mb-2">Recent Decisions <span className="text-[#52525B] normal-case tracking-normal">· click a row for the full pipeline trace</span></h2>
            <ScalpExecutions accountId={accountId} />

            <div className="border border-[#1F1F1F] bg-[#0A0A0A] overflow-x-auto" data-testid="scalp-decisions-table">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="text-[#52525B] uppercase tracking-wider border-b border-[#1F1F1F]">
                            <th className="text-left p-2">Time</th>
                            <th className="text-left p-2">Dir</th>
                            <th className="text-left p-2">Verdict</th>
                            <th className="text-left p-2">Stage</th>
                            <th className="text-right p-2">Edge (p)</th>
                            <th className="text-right p-2">p(target)</th>
                            <th className="text-right p-2">Cost (p)</th>
                            <th className="text-right p-2">EV ($)</th>
                            <th className="text-right p-2">Quality</th>
                            <th className="text-left p-2">Outcome</th>
                            <th className="text-right p-2">Net (p)</th>
                            <th className="text-left p-2">Reject reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        {decisions.map((d) => (
                            <Fragment key={d.id}>
                            <tr className="border-b border-[#141414] text-[#A1A1AA] cursor-pointer hover:bg-[#0D0D0D]"
                                onClick={() => setExpanded(expanded === d.id ? null : d.id)}
                                data-testid={`scalp-decision-row-${d.id}`}>
                                <td className="p-2 font-mono">{new Date(d.ts_ms).toLocaleTimeString()}</td>
                                <td className={`p-2 font-mono ${d.direction === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{d.direction}</td>
                                <td className="p-2">{d.verdict}</td>
                                <td className="p-2 text-[#FFB000]" data-testid={`scalp-decision-stage-${d.id}`}>
                                    {d.verdict === "rejected" ? rejectStage(d) : "—"}
                                </td>
                                <td className="p-2 text-right font-mono">{fmt(d.net_edge_pips, 2)}</td>
                                <td className="p-2 text-right font-mono">{fmt(d.forecast?.p_target_before_stop, 2)}</td>
                                <td className="p-2 text-right font-mono">{fmt(d.cost_pips, 2)}</td>
                                <td className={`p-2 text-right font-mono ${(d.ev?.ev_usd || 0) > 0 ? "text-[#00FF41]" : (d.ev?.ev_usd || 0) < 0 ? "text-[#FF3B30]" : ""}`}
                                    title={d.ev ? `move ${fmt(d.ev.expected_move_pips, 2)}p − cost ${fmt(d.ev.cost_pips, 2)}p` : ""}>
                                    {d.ev?.ev_usd != null ? `$${fmt(d.ev.ev_usd, 2)}` : "—"}
                                </td>
                                <td className="p-2 text-right font-mono"
                                    title={d.quality ? Object.entries(d.quality.breakdown || {}).map(([k, v]) => `${k} +${v}`).join("  ") : ""}
                                    data-testid={`scalp-decision-quality-${d.id}`}>
                                    {d.quality ? `${d.quality.score}/100` : "—"}
                                </td>
                                <td className="p-2">{d.outcome?.result || "pending"}</td>
                                <td className={`p-2 text-right font-mono ${(d.outcome?.net_pips || 0) > 0 ? "text-[#00FF41]" : (d.outcome?.net_pips || 0) < 0 ? "text-[#FF3B30]" : ""}`}>
                                    {fmt(d.outcome?.net_pips, 1)}
                                </td>
                                <td className="p-2 text-[#52525B]">
                                    {d.verdict === "rejected"
                                        ? (d.gates?.edge?.reason || d.gates?.risk?.reason
                                           || d.gates?.final?.reason
                                           || (d.gates?.permission?.reasons || []).join(",") || "—")
                                        : "—"}
                                </td>
                            </tr>
                            {expanded === d.id && (
                                <tr className="border-b border-[#141414] bg-[#050505]"
                                    data-testid={`scalp-decision-pipeline-${d.id}`}>
                                    <td colSpan={12} className="p-3">
                                        <div className="flex flex-wrap items-stretch gap-1.5">
                                            {pipelineStages(d).map((s, i, arr) => (
                                                <Fragment key={s.name}>
                                                    <div className={`px-2.5 py-1.5 border min-w-[130px] ${
                                                        s.status === "pass" ? "border-[#00FF41]/30"
                                                        : s.status === "fail" ? "border-[#FF3B30]/50 bg-[#FF3B30]/5"
                                                        : "border-[#1F1F1F]"}`}>
                                                        <div className="flex items-center gap-1.5">
                                                            <span className={s.status === "pass" ? "text-[#00FF41]" : s.status === "fail" ? "text-[#FF3B30]" : "text-[#52525B]"}>
                                                                {s.status === "pass" ? "✓" : s.status === "fail" ? "✕" : "—"}
                                                            </span>
                                                            <span className="text-[10px] uppercase tracking-wider text-[#A1A1AA]">{s.name}</span>
                                                        </div>
                                                        <div className="text-[10px] text-[#52525B] mt-0.5 max-w-[220px]">{s.detail}</div>
                                                    </div>
                                                    {i < arr.length - 1 && <span className="self-center text-[#333]">→</span>}
                                                </Fragment>
                                            ))}
                                        </div>
                                        {d.quality?.breakdown && (
                                            <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-[#A1A1AA]"
                                                 data-testid={`scalp-quality-breakdown-${d.id}`}>
                                                <span className="uppercase tracking-widest text-[#52525B]">Quality {d.quality.score}/100 =</span>
                                                {Object.entries(d.quality.breakdown).map(([k, v]) => (
                                                    <span key={k} className="font-mono">{k} <span className={v > 0 ? "text-[#00FF41]" : "text-[#52525B]"}>+{v}</span></span>
                                                ))}
                                            </div>
                                        )}
                                    </td>
                                </tr>
                            )}
                            </Fragment>
                        ))}
                        {decisions.length === 0 && (
                            <tr><td colSpan={12} className="p-4 text-center text-[#52525B]">No decisions yet</td></tr>
                        )}
                    </tbody>
                </table>
            </div>

            <ScalpReview />
            </div>
        </AppLayout>
    );
}
