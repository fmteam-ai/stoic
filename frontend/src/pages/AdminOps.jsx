import { useEffect, useState, useCallback } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { GlobalPanicCard } from "@/components/GlobalPanicCard";
import { toast } from "sonner";
import { Loader2, RefreshCw, Server, Plug, Cpu, AlertTriangle, Activity, Database, CreditCard, Users, ListOrdered, ShieldCheck, Receipt, FlaskConical, Play, TrendingDown, Gauge } from "lucide-react";

function StressTest() {
    const [data, setData] = useState(null);
    const [severity, setSeverity] = useState("moderate");
    const [running, setRunning] = useState(false);
    const load = useCallback(async () => {
        try { setData((await api.get("/ops/stress-test/runs")).data); }
        catch { setData({ runs: [], severities: [] }); }
    }, []);
    useEffect(() => { load(); }, [load]);
    const run = async () => {
        setRunning(true);
        try {
            const { data: r } = await api.post(`/ops/stress-test/run?severity=${severity}`);
            toast[r.verdict === "STAYED_CALM" ? "success" : "error"](
                `Stress test (${severity}): ${r.verdict === "STAYED_CALM" ? "bot stayed calm" : "bot PANICKED"} — ${r.passed}/${r.passed + r.failed} layers held`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setRunning(false); }
    };
    const last = data?.runs?.[0];
    return (
        <Panel title="Stress Test — flash crash" icon={TrendingDown} testid="ops-panel-stress-test">
            <div className="flex items-center justify-between mb-2 gap-2">
                <select value={severity} onChange={e => setSeverity(e.target.value)}
                    data-testid="stress-test-severity"
                    className="bg-[#0A0A0A] border border-[#1F1F1F] text-xs text-[#A1A1AA] px-2 py-1.5 font-mono">
                    <option value="mild">MILD −3%</option>
                    <option value="moderate">MODERATE −8%</option>
                    <option value="severe">SEVERE −15%</option>
                </select>
                <button onClick={run} disabled={running} data-testid="stress-test-run-btn"
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 flex items-center gap-1.5 shrink-0">
                    {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                    {running ? "CRASHING…" : "SIMULATE CRASH"}
                </button>
            </div>
            <div className="text-xs text-[#71717A] mb-2">
                Replays a sudden market drop through the real defense layers —
                does the bot stay calm under pressure?
            </div>
            {!last && <div className="text-xs text-[#52525B]" data-testid="stress-test-empty">No stress tests yet.</div>}
            {last && (
                <div data-testid="stress-test-results">
                    <div className={`text-xs font-mono mb-1.5 ${last.verdict === "STAYED_CALM" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                        {last.verdict === "STAYED_CALM" ? "✓ STAYED CALM" : "✗ PANICKED"} · {last.severity} ({last.params?.drop_pct}% drop) · {new Date(last.at).toLocaleString()}
                    </div>
                    {last.checks.map(c => (
                        <div key={c.layer} className="flex items-start gap-2 py-0.5 text-xs" title={c.detail}>
                            <span className={`shrink-0 font-mono ${c.status === "pass" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {c.status === "pass" ? "CALM" : "FAIL"}
                            </span>
                            <span className="text-[#A1A1AA]">{c.layer.replaceAll("_", " ")}</span>
                        </div>
                    ))}
                </div>
            )}
        </Panel>
    );
}

function SloPanel() {
    const [slos, setSlos] = useState(null);
    useEffect(() => {
        api.get("/ops/slo").then(r => setSlos(r.data.slos)).catch(() => setSlos({}));
        const t = setInterval(() => {
            api.get("/ops/slo").then(r => setSlos(r.data.slos)).catch(() => {});
        }, 30000);
        return () => clearInterval(t);
    }, []);
    const toneFor = (s) => s?.status === "ok" ? "text-[#00FF41]"
        : s?.status === "at_risk" ? "text-[#FFD700]"
        : s?.status === "breached" ? "text-[#FF3B30]" : "text-[#52525B]";
    return (
        <Panel title="SLOs & Error Budgets" icon={Gauge} testid="ops-panel-slo">
            {!slos && <Loader2 className="w-4 h-4 animate-spin text-[#52525B]" />}
            {slos && Object.entries(slos).map(([k, s]) => (
                <div key={k} className="py-1 border-b border-[#141414] last:border-0" data-testid={`slo-${k}`}>
                    <div className="flex justify-between text-xs">
                        <span className="text-[#A1A1AA]">{s.title}</span>
                        <span className={`font-mono ${toneFor(s)}`}>
                            {s.compliance_pct != null ? `${s.compliance_pct}%` : "no data"}
                        </span>
                    </div>
                    <div className="flex justify-between text-[10px] text-[#52525B] font-mono">
                        <span>target {s.target_pct}% · {s.window}</span>
                        <span className={toneFor(s)}>
                            {s.budget_consumed_pct != null ? `budget ${s.budget_consumed_pct}% used` : "—"}
                        </span>
                    </div>
                </div>
            ))}
        </Panel>
    );
}

function ReleasesPanel() {
    const [data, setData] = useState(null);
    const [busy, setBusy] = useState(false);
    const load = useCallback(async () => {
        try { setData((await api.get("/ops/releases")).data); }
        catch { setData(null); }
    }, []);
    useEffect(() => { load(); }, [load]);
    const act = async (path, okMsg) => {
        setBusy(true);
        try { await api.post(path); toast.success(okMsg); await load(); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    const st = data?.state;
    const short = (m) => Object.entries(m || {}).map(([k, v]) => `${k}@${String(v).slice(0, 10)}`).join(" · ") || "—";
    return (
        <Panel title="Releases — canary rollout" icon={ListOrdered} testid="ops-panel-releases">
            {!st && <div className="text-xs text-[#52525B]">loading…</div>}
            {st && (
                <>
                    <Row k="Stable (fleet)" v={short(st.stable)} vCls="text-[#00FF41]" />
                    <Row k="Candidate (canary)" v={st.candidate ? short(st.candidate) : "none — fleet on stable"} vCls={st.candidate ? "text-[#FFD700]" : tone.dim} />
                    <Row k="Canary agents" v={(st.canary_agents || []).join(", ") || "none set"} vCls={tone.dim} />
                    <Row k="Auto-promotion" v={st.pinned ? "PINNED (rollback active)" : `after ${st.promote_after_hours || 24}h clean soak`} vCls={st.pinned ? "text-[#FF3B30]" : tone.dim} />
                    <div className="flex gap-2 mt-2">
                        <button onClick={() => act("/ops/releases/promote", "Candidate promoted to stable")}
                            disabled={busy || !st.candidate} data-testid="release-promote-btn"
                            className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-40">
                            PROMOTE NOW
                        </button>
                        <button onClick={() => act("/ops/releases/rollback", "Rolled back to previous stable (pinned)")}
                            disabled={busy || !st.previous_stable} data-testid="release-rollback-btn"
                            className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 disabled:opacity-40">
                            ROLLBACK
                        </button>
                    </div>
                    <div className="mt-2 max-h-24 overflow-y-auto">
                        {(data.history || []).map(h => (
                            <div key={h.event_id} className="text-[10px] font-mono text-[#52525B] py-0.5">
                                {new Date(h.at).toLocaleString()} · {h.event}
                            </div>
                        ))}
                    </div>
                </>
            )}
        </Panel>
    );
}

function DeploymentHealthPanel() {
    const [data, setData] = useState(null);
    useEffect(() => {
        const load = () => api.get("/ops/deployment-health").then(r => setData(r.data)).catch(() => setData(null));
        load();
        const t = setInterval(load, 60000);
        return () => clearInterval(t);
    }, []);
    const h = data?.health;
    const scoreCls = h ? (h.score >= 80 ? tone.ok : h.score >= 60 ? tone.warn : tone.bad) : tone.dim;
    return (
        <Panel title="Deployment Health — auto-rollback" icon={Gauge} testid="ops-panel-deploy-health">
            {!data && <div className="text-xs text-[#52525B]">loading…</div>}
            {data && (
                <>
                    <Row k="Fleet health score" v={h ? `${h.score}/100` : "—"} vCls={scoreCls} />
                    <Row k="Fleet size" v={h?.fleet_size ?? "—"} vCls={tone.dim} />
                    {h?.components && (
                        <>
                            <Row k="Fresh heartbeats" v={h.components.heartbeat_fresh} vCls={tone.dim} />
                            <Row k="MT5 connected" v={h.components.mt5_connected} vCls={tone.dim} />
                            <Row k="Deploy failures (2h)" v={h.components.deployment_failed_2h}
                                vCls={h.components.deployment_failed_2h ? tone.bad : "text-white"} />
                        </>
                    )}
                    <Row k="Bake watch" v={data.watch
                        ? `BAKING since ${new Date(data.watch.started_at).toLocaleString()} (baseline ${data.watch.baseline_score})`
                        : "idle — no deployment in bake window"}
                        vCls={data.watch ? "text-[#FFD700]" : tone.dim} />
                    <Row k="Policy" v={`floor ${data.policy.min_score} · max drop ${data.policy.max_drop} · bake ${data.policy.bake_hours}h`} vCls={tone.dim} />
                    {data.last_auto_rollback && (
                        <div className="mt-1 text-[10px] font-mono text-[#FF3B30]" data-testid="deploy-health-last-auto-rollback">
                            last auto-rollback {new Date(data.last_auto_rollback.at).toLocaleString()} — {data.last_auto_rollback.detail?.reason}
                        </div>
                    )}
                </>
            )}
        </Panel>
    );
}

function TradeLookup() {
    const [tid, setTid] = useState("");
    const [tl, setTl] = useState(null);
    const [err, setErr] = useState("");
    const lookup = async () => {
        setErr(""); setTl(null);
        try { setTl((await api.get(`/trades/${encodeURIComponent(tid.trim())}/timeline`)).data); }
        catch (e) { setErr(formatApiError(e)); }
    };
    return (
        <Panel title="Trade Lifecycle Lookup" icon={ListOrdered} testid="ops-panel-trade-lookup">
            <div className="flex gap-2 mb-2">
                <input value={tid} onChange={e => setTid(e.target.value)}
                    placeholder="trade id / MT5 ticket"
                    data-testid="trade-lookup-input"
                    className="flex-1 bg-[#050505] border border-[#1F1F1F] text-xs text-white px-2 py-1.5 font-mono" />
                <button onClick={lookup} disabled={!tid.trim()} data-testid="trade-lookup-btn"
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]">
                    TRACE
                </button>
            </div>
            {err && <div className="text-xs text-[#FF3B30]" data-testid="trade-lookup-error">{err}</div>}
            {tl && (
                <div data-testid="trade-lookup-results">
                    <div className="text-xs font-mono text-white mb-1">
                        {tl.symbol} · {tl.status} · {tl.trade_id.slice(0, 10)}…
                    </div>
                    {tl.stages.map((s, i) => (
                        <div key={i} className="flex items-start gap-2 py-0.5 text-xs" title={JSON.stringify(s.detail).slice(0, 300)}>
                            <span className="shrink-0 font-mono text-[#00FF41] w-24 uppercase">{s.stage}</span>
                            <span className="text-[#A1A1AA]">{s.summary}</span>
                        </div>
                    ))}
                </div>
            )}
        </Panel>
    );
}

function FleetTable({ fleet }) {
    if (!fleet?.length) return <div className="text-xs text-[#52525B]" data-testid="fleet-empty">No host agents reporting yet.</div>;
    return (
        <div className="overflow-x-auto" data-testid="fleet-table">
            <table className="w-full text-[10px] font-mono">
                <thead><tr className="text-[#52525B] text-left">
                    <th className="pr-2">AGENT</th><th className="pr-2">VER</th><th className="pr-2">HB</th>
                    <th className="pr-2">CPU</th><th className="pr-2">RAM</th><th className="pr-2">DISK</th>
                    <th className="pr-2">MT5</th><th className="pr-2">RST</th><th>REBOOT?</th>
                </tr></thead>
                <tbody>
                    {fleet.map(a => (
                        <tr key={a.agent_id} className="text-[#A1A1AA] border-t border-[#141414]">
                            <td className="pr-2">{a.agent_id?.slice(0, 14)}</td>
                            <td className="pr-2">{a.agent_version || "—"}</td>
                            <td className={`pr-2 ${a.heartbeat_age_sec > 180 ? "text-[#FF3B30]" : "text-[#00FF41]"}`}>
                                {a.heartbeat_age_sec != null ? `${a.heartbeat_age_sec}s` : "—"}</td>
                            <td className="pr-2">{a.cpu_percent != null ? `${a.cpu_percent}%` : "—"}</td>
                            <td className="pr-2">{a.ram_percent != null ? `${a.ram_percent}%` : "—"}</td>
                            <td className={`pr-2 ${a.disk_free_pct < 10 ? "text-[#FF3B30]" : ""}`}>
                                {a.disk_free_pct != null ? `${a.disk_free_pct}%` : "—"}</td>
                            <td className={`pr-2 ${a.mt5_connected ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {a.mt5_connected ? "UP" : "DOWN"}</td>
                            <td className="pr-2">{a.mt5_restarts ?? "—"}</td>
                            <td className={a.pending_reboot ? "text-[#FFD700]" : ""}>{a.pending_reboot ? "YES" : "no"}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

function NightlyDrills() {
    const [data, setData] = useState(null);
    const [running, setRunning] = useState(false);
    const load = useCallback(async () => {
        try { setData((await api.get("/ops/scheduled-drills")).data); }
        catch { setData({ runs: [] }); }
    }, []);
    useEffect(() => { load(); }, [load]);
    const runNow = async () => {
        setRunning(true);
        try {
            const { data: r } = await api.post("/ops/scheduled-drills/run");
            toast[r.ok ? "success" : "error"](r.ok
                ? "Nightly suite: all green"
                : `Nightly suite FAILED: ${r.failures?.slice(0, 3).join(", ")}`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setRunning(false); }
    };
    return (
        <Panel title="Nightly Drills" icon={FlaskConical} testid="ops-panel-nightly-drills">
            <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-[#71717A]">
                    Chaos (12) + runtime validation (9) + severe stress test run
                    automatically every night — failures raise a CRITICAL alert.
                </span>
                <button onClick={runNow} disabled={running} data-testid="nightly-drills-run-btn"
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1.5 shrink-0">
                    {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                    {running ? "RUNNING" : "RUN NOW"}
                </button>
            </div>
            {data?.runs?.length === 0 && (
                <div className="text-xs text-[#52525B]" data-testid="nightly-drills-empty">
                    No runs recorded yet — first run happens automatically tonight.
                </div>
            )}
            {(data?.runs || []).map(r => (
                <div key={r.run_id} className="flex items-center gap-2 py-0.5 text-xs font-mono"
                    title={(r.failures || []).join(", ")}>
                    <span className={r.ok ? "text-[#00FF41]" : "text-[#FF3B30]"}>
                        {r.ok ? "GREEN" : "FAIL"}
                    </span>
                    <span className="text-[#A1A1AA]">{new Date(r.at).toLocaleString()}</span>
                    <span className="text-[#52525B]">
                        chaos {r.chaos?.passed}/{r.chaos?.total} · runtime {r.runtime?.passed}/{r.runtime?.total} · stress {r.stress?.verdict}
                    </span>
                </div>
            ))}
        </Panel>
    );
}

function RuntimeValidation() {
    const [data, setData] = useState(null);
    const [running, setRunning] = useState(false);
    const load = useCallback(async () => {
        try { setData((await api.get("/ops/validation/runtime/runs")).data); }
        catch { setData({ runs: [], scenarios: [] }); }
    }, []);
    useEffect(() => { load(); }, [load]);
    const run = async () => {
        setRunning(true);
        try {
            const { data: r } = await api.post("/ops/validation/runtime/run", {});
            toast[r.failed === 0 ? "success" : "error"](
                `Runtime validation: ${r.passed} passed, ${r.failed} failed`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setRunning(false); }
    };
    const last = data?.runs?.[0];
    return (
        <Panel title="Runtime Validation" icon={FlaskConical} testid="ops-panel-runtime-validation">
            <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-[#71717A]">
                    Proofs against live gates: lease fencing, replay protection, signed
                    artifacts, promotion rules, refund idempotency…
                </span>
                <button onClick={run} disabled={running} data-testid="runtime-validation-run-btn"
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1.5 shrink-0">
                    {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                    {running ? "RUNNING" : "RUN"}
                </button>
            </div>
            {!last && <div className="text-xs text-[#52525B]" data-testid="runtime-validation-empty">No runs yet — run the harness before each release.</div>}
            {last && (
                <div data-testid="runtime-validation-results">
                    <div className={`text-xs font-mono mb-1.5 ${last.failed === 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                        {last.run_id} · {last.passed}/{last.passed + last.failed} passed · {new Date(last.at).toLocaleString()}
                    </div>
                    {last.results.map(r => (
                        <div key={r.scenario} className="flex items-start gap-2 py-0.5 text-xs" title={r.notes}>
                            <span className={`shrink-0 font-mono ${r.status === "pass" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {r.status === "pass" ? "PASS" : "FAIL"}
                            </span>
                            <span className="text-[#A1A1AA]">{r.scenario}</span>
                        </div>
                    ))}
                </div>
            )}
        </Panel>
    );
}

const fmtAge = (s) => {
    if (s === null || s === undefined) return "never";
    if (s < 90) return `${s}s`;
    if (s < 5400) return `${Math.round(s / 60)}m`;
    if (s < 172800) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
};

const tone = {
    ok: "text-[#00FF41]",
    warn: "text-[#FFB000]",
    bad: "text-[#FF3B30]",
    dim: "text-[#52525B]",
};

function Stat({ label, value, cls = "text-white", testid }) {
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] px-3 py-2.5" data-testid={testid}>
            <div className="text-[9px] font-mono tracking-widest text-[#52525B] uppercase">{label}</div>
            <div className={`font-display text-lg leading-6 ${cls}`}>{value}</div>
        </div>
    );
}

function Panel({ title, icon: Icon, children, testid }) {
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid={testid}>
            <div className="px-4 py-2.5 border-b border-[#1F1F1F] flex items-center gap-2">
                <Icon className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">{title}</span>
            </div>
            <div className="p-4">{children}</div>
        </div>
    );
}

const Row = ({ k, v, vCls = "text-white", testid }) => (
    <div className="flex justify-between text-xs py-1 border-b border-[#141414] last:border-0" data-testid={testid}>
        <span className="text-[#71717A]">{k}</span>
        <span className={`font-mono ${vCls} text-right`}>{v}</span>
    </div>
);

function RuntimeHealth() {
    const [d, setD] = useState(null);
    const load = useCallback(async () => {
        try { setD((await api.get("/ops/runtime-stats")).data); } catch { /* ignore */ }
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);
    if (!d) return null;
    const upMin = Math.floor(d.uptime_s / 60);
    const lagBad = d.loop_lag_s > 3;
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-4 mb-4" data-testid="runtime-health-card">
            <div className="flex flex-wrap items-center gap-4 mb-2">
                <div className="font-display text-sm text-white">Runtime Health · crash forensics</div>
                <div className="font-mono text-[11px] text-[#71717A]" data-testid="runtime-health-rss">
                    UPTIME <span className="text-white">{upMin >= 60 ? `${Math.floor(upMin / 60)}h ${upMin % 60}m` : `${upMin}m`}</span>
                    {" · "}RSS <span className="text-white">{d.rss_mb} MB</span> (peak {d.max_rss_mb} MB)
                    {" · "}LOOP LAG <span className={lagBad ? "text-[#FF3B30]" : "text-[#00FF41]"}>{d.loop_lag_s}s</span>
                    {" · "}RESTARTS LOGGED <span className="text-white">{(d.restarts || []).length}</span>
                </div>
            </div>
            {d.last_blockage && (
                <details className="border border-[#FFB000]/30 bg-[#FFB000]/5 p-2 mb-2" data-testid="runtime-last-blockage">
                    <summary className="font-mono text-[11px] text-[#FFB000] cursor-pointer">
                        EVENT LOOP BLOCKED {d.last_blockage.blocked_for_s}s at {d.last_blockage.at?.slice(0, 19)} (rss {d.last_blockage.rss_mb} MB) — stack
                    </summary>
                    <pre className="text-[10px] text-[#A1A1AA] overflow-x-auto whitespace-pre-wrap mt-2">{d.last_blockage.stack}</pre>
                </details>
            )}
            {(d.restarts || []).length > 0 && (
                <div className="space-y-1" data-testid="runtime-restarts">
                    {(d.restarts || []).slice(0, 5).map((r, i) => (
                        <details key={i} className="border border-[#1F1F1F] p-2">
                            <summary className="font-mono text-[10px] text-[#71717A] cursor-pointer">
                                RUN ENDED {r.last_seen?.slice(0, 19)} · ended rss {r.ended_rss_mb} MB (peak {r.max_rss_mb} MB)
                                {r.last_blockage ? ` · LOOP WAS BLOCKED ${r.last_blockage.blocked_for_s}s before death` : " · no blockage captured"}
                            </summary>
                            {r.last_blockage && <pre className="text-[10px] text-[#A1A1AA] overflow-x-auto whitespace-pre-wrap mt-2">{r.last_blockage.stack}</pre>}
                        </details>
                    ))}
                </div>
            )}
        </div>
    );
}

export default function AdminOps() {
    const [d, setD] = useState(null);
    const [refreshing, setRefreshing] = useState(false);

    const load = useCallback(async (manual = false) => {
        if (manual) setRefreshing(true);
        try { setD((await api.get("/admin/ops-console")).data); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setRefreshing(false); }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);

    if (!d) return (
        <AppLayout>
            <PageHeader title="Ops Console" subtitle="Loading fleet telemetry…" />
            <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>
        </AppLayout>
    );

    const sev = d.alerts.unacked_by_severity || {};
    const critAlerts = (sev.critical || 0) + (sev.high || 0);

    return (
        <AppLayout>
            <PageHeader title="Ops Console" subtitle={`Single pane of glass · generated ${new Date(d.generated_at).toLocaleTimeString()} · auto-refresh 30s`}
                testid="ops-console-header"
                action={
                    <button onClick={() => load(true)} data-testid="ops-refresh-btn"
                        className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5">
                        <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} /> REFRESH
                    </button>
                } />

            <div className="mb-4"><GlobalPanicCard onDone={() => load(true)} /></div>

            {/* Top strip */}
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2 mb-4" data-testid="ops-stat-strip">
                <Stat testid="ops-stat-vps-online" label="VPS Online" value={`${d.vps.online}/${d.vps.agents_total}`}
                    cls={d.vps.agents_total === 0 ? tone.dim : d.vps.offline === 0 ? tone.ok : tone.warn} />
                <Stat testid="ops-stat-mt5" label="MT5 Connected" value={`${d.mt5_bridge.connected}/${d.mt5_bridge.accounts_total}`}
                    cls={d.mt5_bridge.connected > 0 ? tone.ok : tone.warn} />
                <Stat testid="ops-stat-hb" label="Freshest EA HB" value={fmtAge(d.mt5_bridge.freshest_heartbeat_age_sec)}
                    cls={(d.mt5_bridge.freshest_heartbeat_age_sec ?? 9e9) < 180 ? tone.ok : tone.warn} />
                <Stat testid="ops-stat-bots" label="Bots Pulsing" value={`${d.engine.bots_pulsing}/${d.engine.bots_active}`}
                    cls={d.engine.bots_silent === 0 ? tone.ok : tone.warn} />
                <Stat testid="ops-stat-queue" label="Queue Depth" value={d.command_queue.depth}
                    cls={d.command_queue.depth === 0 ? tone.ok : tone.warn} />
                <Stat testid="ops-stat-alerts" label="Unacked Alerts" value={d.alerts.unacked_total}
                    cls={critAlerts > 0 ? tone.bad : d.alerts.unacked_total > 0 ? tone.warn : tone.ok} />
                <Stat testid="ops-stat-latency" label="API p95" value={d.api.p95_ms != null ? `${d.api.p95_ms}ms` : "—"}
                    cls={(d.api.p95_ms ?? 0) < 800 ? tone.ok : tone.warn} />
                <Stat testid="ops-stat-mongo" label="Mongo Ping" value={d.mongo.ok ? `${d.mongo.ping_ms}ms` : "DOWN"}
                    cls={d.mongo.ok ? tone.ok : tone.bad} />
            </div>

            <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
                <Panel title="VPS Fleet" icon={Server} testid="ops-panel-vps">
                    <Row k="Agents registered" v={d.vps.agents_total} />
                    <Row k="Online (≤3m heartbeat)" v={d.vps.online} vCls={tone.ok} />
                    <Row k="Offline" v={d.vps.offline} vCls={d.vps.offline ? tone.warn : "text-white"} />
                    {d.vps.offline_list.slice(0, 5).map((a, i) => (
                        <Row key={i} k={`↳ ${a.agent_id || "?"}`} v={`hb ${fmtAge(a.heartbeat_age_sec)}`} vCls={tone.dim} />
                    ))}
                </Panel>

                <Panel title="MT5 / EA Bridge" icon={Plug} testid="ops-panel-mt5">
                    <Row k="Accounts" v={d.mt5_bridge.accounts_total} />
                    <Row k="Connected (≤3m)" v={d.mt5_bridge.connected} vCls={tone.ok} />
                    <Row k="Disconnected" v={d.mt5_bridge.disconnected} vCls={d.mt5_bridge.disconnected ? tone.warn : "text-white"} />
                    {Object.entries(d.mt5_bridge.ea_versions).map(([v, n]) => (
                        <Row key={v} k={`EA v${v}`} v={`${n} account${n > 1 ? "s" : ""}`} vCls={tone.dim} />
                    ))}
                    {d.mt5_bridge.stale_list.slice(0, 4).map((a, i) => (
                        <Row key={i} k={`↳ ${a.display_name || "?"}`} v={`hb ${fmtAge(a.heartbeat_age_sec)}`} vCls={tone.dim} />
                    ))}
                </Panel>

                <Panel title="Engine & Workers" icon={Cpu} testid="ops-panel-engine">
                    <Row k="Active bots" v={d.engine.bots_active} />
                    <Row k="Pulsing (≤5m)" v={d.engine.bots_pulsing} vCls={tone.ok} />
                    <Row k="Silent" v={d.engine.bots_silent} vCls={d.engine.bots_silent ? tone.warn : "text-white"} />
                    <Row k="Workers alive" v={`${d.engine.workers_alive}/${d.engine.workers.length}`}
                        vCls={d.engine.workers_alive > 0 ? tone.ok : tone.bad} />
                    {d.engine.workers.slice(0, 4).map((w, i) => (
                        <Row key={i} k={`↳ ${String(w.holder).slice(0, 22)}`}
                            v={w.alive ? `alive · renewed ${fmtAge(w.renewed_age_sec)}` : "expired"}
                            vCls={w.alive ? tone.dim : tone.bad} />
                    ))}
                </Panel>

                <Panel title="Deployments" icon={ListOrdered} testid="ops-panel-deployments">
                    <Row k="In progress" v={d.deployments.in_progress} vCls={d.deployments.in_progress ? tone.warn : "text-white"} />
                    <Row k="Failed" v={d.deployments.failed} vCls={d.deployments.failed ? tone.bad : "text-white"} />
                    {d.deployments.success_24h && (
                        <Row k="Success rate (24h)"
                            v={d.deployments.success_24h.rate_pct != null ? `${d.deployments.success_24h.rate_pct}% (${d.deployments.success_24h.succeeded}/${d.deployments.success_24h.succeeded + d.deployments.success_24h.failed})` : "no deploys"}
                            vCls={(d.deployments.success_24h.rate_pct ?? 100) >= 90 ? tone.ok : tone.warn} />
                    )}
                    {d.deployments.success_7d && (
                        <Row k="Success rate (7d)"
                            v={d.deployments.success_7d.rate_pct != null ? `${d.deployments.success_7d.rate_pct}%` : "no deploys"}
                            vCls={(d.deployments.success_7d.rate_pct ?? 100) >= 90 ? tone.ok : tone.warn} />
                    )}
                    {Object.entries(d.deployments.by_state).map(([s, n]) => (
                        <Row key={s} k={s} v={n} vCls={tone.dim} />
                    ))}
                    {d.deployments.recent_failed.map((f, i) => (
                        <Row key={i} k={`↳ ${(f.deployment_id || "?").slice(0, 16)}`} v={f.state} vCls={tone.bad} />
                    ))}
                </Panel>

                <Panel title="Command Queue" icon={ListOrdered} testid="ops-panel-queue">
                    <Row k="Queue depth" v={d.command_queue.depth} vCls={d.command_queue.depth ? tone.warn : "text-white"} />
                    <Row k="Failed (24h)" v={d.command_queue.failed_24h} vCls={d.command_queue.failed_24h ? tone.bad : "text-white"} />
                    <Row k="Oldest queued" v={fmtAge(d.command_queue.oldest_queued_age_sec)} vCls={tone.dim} />
                </Panel>

                <Panel title="Risk Alerts" icon={AlertTriangle} testid="ops-panel-alerts">
                    {Object.entries(sev).map(([s, n]) => (
                        <Row key={s} k={s} v={n} vCls={s === "critical" || s === "high" ? tone.bad : tone.warn} />
                    ))}
                    {d.alerts.latest.slice(0, 6).map((a, i) => (
                        <div key={i} className="py-1.5 border-b border-[#141414] last:border-0">
                            <div className="flex justify-between text-[10px] font-mono">
                                <span className={a.severity === "critical" || a.severity === "high" ? tone.bad : tone.warn}>
                                    {a.severity?.toUpperCase()} · {a.kind}
                                </span>
                                <span className="text-[#52525B]">{fmtAge(a.age_sec)} · ×{a.occurrences}</span>
                            </div>
                            <div className="text-xs text-[#A1A1AA] truncate">{a.message}</div>
                        </div>
                    ))}
                    {d.alerts.unacked_total === 0 && <div className="text-xs text-[#52525B]">No unacked alerts.</div>}
                </Panel>

                <Panel title="API & MongoDB" icon={Activity} testid="ops-panel-api">
                    <Row k={`Requests (${Math.round(d.api.window_sec / 60)}m)`} v={d.api.count} />
                    <Row k="p50 / p95 / max" v={d.api.count ? `${d.api.p50_ms} / ${d.api.p95_ms} / ${d.api.max_ms} ms` : "—"} />
                    <Row k="5xx error rate" v={d.api.error_rate_pct != null ? `${d.api.error_rate_pct}%` : "—"}
                        vCls={(d.api.error_rate_pct ?? 0) > 1 ? tone.bad : tone.ok} />
                    <Row k="Mongo ping" v={d.mongo.ok ? `${d.mongo.ping_ms} ms` : "DOWN"} vCls={d.mongo.ok ? tone.ok : tone.bad} />
                    <Row k="Data / Index size" v={d.mongo.ok ? `${d.mongo.data_mb} / ${d.mongo.index_mb} MB` : "—"} vCls={tone.dim} />
                    <Row k="Collections" v={d.mongo.collections ?? "—"} vCls={tone.dim} />
                    <Row k="Unique ticket index" testid="ops-unique-ticket-index"
                        v={d.unique_ticket_index?.ok ? "OK" : d.unique_ticket_index?.stale ? "STALE — run ops/ticket_duplicates.py --build-index" : d.unique_ticket_index?.present === false ? `NOT BUILT · ${d.unique_ticket_index?.duplicates ?? "?"} duplicate group(s)` : "UNVERIFIED"}
                        vCls={d.unique_ticket_index?.ok ? tone.ok : tone.bad} />
                </Panel>

                <Panel title="Stripe Webhooks" icon={CreditCard} testid="ops-panel-stripe">
                    <Row k="Last webhook" v={fmtAge(d.stripe.last_webhook_age_sec)}
                        vCls={d.stripe.last_webhook_age_sec == null ? tone.dim : tone.ok} />
                    <Row k="Paid (24h)" v={d.stripe.paid_24h} vCls={tone.ok} />
                    <Row k="Stale initiated sessions" v={d.stripe.stale_initiated_sessions}
                        vCls={d.stripe.stale_initiated_sessions ? tone.warn : "text-white"} />
                    {Object.entries(d.stripe.webhooks_24h).map(([t, n]) => (
                        <Row key={t} k={t} v={n} vCls={tone.dim} />
                    ))}
                </Panel>

                <Panel title="Subscriptions" icon={Users} testid="ops-panel-subs">
                    <Row k="Active paid passes" v={d.subscriptions.active} vCls={tone.ok} />
                    <Row k="Expiring ≤7d" v={d.subscriptions.expiring_7d}
                        vCls={d.subscriptions.expiring_7d ? tone.warn : "text-white"} />
                    {Object.entries(d.subscriptions.by_plan).map(([p, n]) => (
                        <Row key={p} k={p} v={n} vCls={tone.dim} />
                    ))}
                </Panel>

                <Panel title="Billing Events" icon={Receipt} testid="ops-panel-billing">
                    {(d.billing_feed || []).map((b, i) => (
                        <div key={i} className="flex justify-between text-xs py-1 border-b border-[#141414] last:border-0">
                            <span className="text-[#71717A] truncate mr-2">{b.email} · {(b.plan_id || "").replace(/_/g, " ")}</span>
                            <span className={`font-mono flex-shrink-0 ${b.status === "paid" ? tone.ok : tone.bad}`}>
                                ${b.amount_usd} {b.status.toUpperCase()}
                            </span>
                        </div>
                    ))}
                    {(!d.billing_feed || d.billing_feed.length === 0) && <div className="text-xs text-[#52525B]">No billing events yet.</div>}
                </Panel>

                <Panel title="Security" icon={ShieldCheck} testid="ops-panel-security">
                    <Row k="Failed auth (recent windows)" v={d.security.failed_auth_recent}
                        vCls={d.security.failed_auth_recent > 50 ? tone.warn : "text-white"} />
                    <Row k="Suspended users" v={d.security.suspended_users} vCls={tone.dim} />
                    <Row k="Audit chain" v={d.security.audit_chain_ok ? `INTACT (${d.security.audit_chain_entries})` : "BROKEN"}
                        vCls={d.security.audit_chain_ok ? tone.ok : tone.bad} />
                    <Row k="Admin MFA enforced" v={d.security.admin_mfa_enforced ? "YES" : "NO (preview)"}
                        vCls={d.security.admin_mfa_enforced ? tone.ok : tone.warn} />
                    <Row k="Email OTP login" v={d.security.email_otp_login ? "ENABLED" : "DISABLED"} vCls={tone.dim} />
                    {d.host_agents && (
                        <>
                            <Row k="Agents low on disk (<10%)" v={d.host_agents.low_disk_count}
                                vCls={d.host_agents.low_disk_count ? tone.bad : "text-white"} />
                            <Row k="Avg broker latency" v={d.host_agents.avg_broker_latency_ms != null ? `${d.host_agents.avg_broker_latency_ms}ms` : "not reported"} vCls={tone.dim} />
                            <div className="mt-2"><FleetTable fleet={d.host_agents.fleet} /></div>
                        </>
                    )}
                </Panel>

                <RuntimeValidation />
                <RuntimeHealth />
                <StressTest />
                <SloPanel />
                <NightlyDrills />
                <ReleasesPanel />
                <DeploymentHealthPanel />
                <TradeLookup />
            </div>
        </AppLayout>
    );
}
