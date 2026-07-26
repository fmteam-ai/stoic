import { useEffect, useState, useCallback } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, RefreshCw, Server, Plug, Cpu, AlertTriangle, Activity, Database, CreditCard, Users, ListOrdered, ShieldCheck, Receipt, FlaskConical, Play } from "lucide-react";

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

const Row = ({ k, v, vCls = "text-white" }) => (
    <div className="flex justify-between text-xs py-1 border-b border-[#141414] last:border-0">
        <span className="text-[#71717A]">{k}</span>
        <span className={`font-mono ${vCls}`}>{v}</span>
    </div>
);

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
                        </>
                    )}
                </Panel>

                <RuntimeValidation />
            </div>
        </AppLayout>
    );
}
