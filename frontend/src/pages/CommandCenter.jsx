import { useCallback, useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import {
    Activity, AlertTriangle, Award, Bird, Download, Loader2, Mail,
    RefreshCw, ShieldCheck, Timer, Server,
} from "lucide-react";

const TONE = {
    GREEN: { text: "text-[#00FF41]", border: "border-[#00FF41]/40", bg: "bg-[#00FF41]/5", dot: "bg-[#00FF41]" },
    YELLOW: { text: "text-[#FFD700]", border: "border-[#FFD700]/40", bg: "bg-[#FFD700]/5", dot: "bg-[#FFD700]" },
    RED: { text: "text-[#FF3B30]", border: "border-[#FF3B30]/40", bg: "bg-[#FF3B30]/5", dot: "bg-[#FF3B30]" },
};
const tone = (s) => TONE[s] || TONE.YELLOW;

function StatusDot({ status, size = "w-2.5 h-2.5" }) {
    return <span className={`inline-block rounded-full ${size} ${tone(status).dot} ${status !== "GREEN" ? "animate-pulse" : ""}`} />;
}

function SectionCard({ title, icon: Icon, section, testid, children }) {
    const t = tone(section?.status);
    return (
        <div className={`border ${t.border} ${t.bg} p-4`} data-testid={testid}>
            <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                    <Icon className={`w-4 h-4 ${t.text}`} />
                    <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">{title}</span>
                </div>
                <div className="flex items-center gap-1.5" data-testid={`${testid}-status`}>
                    <StatusDot status={section?.status} />
                    <span className={`text-xs font-mono font-bold ${t.text}`}>{section?.status || "…"}</span>
                </div>
            </div>
            <div className="text-xs text-[#71717A] mb-3" data-testid={`${testid}-detail`}>{section?.detail}</div>
            {children}
        </div>
    );
}

const Metric = ({ label, value, testid }) => (
    <div className="flex items-center justify-between py-0.5 text-xs" data-testid={testid}>
        <span className="text-[#52525B]">{label}</span>
        <span className="font-mono text-[#FAFAFA]">{value ?? "—"}</span>
    </div>
);

const ActionBtn = ({ onClick, disabled, testid, danger, children }) => (
    <button onClick={onClick} disabled={disabled} data-testid={testid}
        className={`px-2.5 py-1 text-[11px] font-mono tracking-widest border flex items-center gap-1.5 disabled:opacity-40 ${
            danger
                ? "border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10"
                : "border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10"}`}>
        {children}
    </button>
);

export default function CommandCenter() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [exporting, setExporting] = useState(false);
    const [refreshing, setRefreshing] = useState(false);
    const [acting, setActing] = useState(null);
    const [canaryAccount, setCanaryAccount] = useState("");

    const load = useCallback(async (manual = false) => {
        if (manual) setRefreshing(true);
        try {
            setData((await api.get("/command-center/status")).data);
            setError(null);
        } catch (e) {
            setError(formatApiError(e));
        } finally {
            if (manual) setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);

    const act = async (path, body, okMsg) => {
        setActing(path);
        try {
            const { data: res } = await api.post(path, body || {});
            if (res?.error) toast.error(String(res.error));
            else toast.success(okMsg || "Done");
            await load();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setActing(null);
        }
    };

    const exportEvidence = async () => {
        setExporting(true);
        try {
            const { data: report } = await api.get("/command-center/evidence-export");
            const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `stoic-evidence-${new Date().toISOString().slice(0, 19).replaceAll(":", "")}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            toast.success(`Evidence report downloaded — report_hash ${String(report.report_hash || "").slice(0, 12)}…`);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setExporting(false);
        }
    };

    const s = data?.sections || {};
    const overallTone = tone(data?.overall);
    const soakRunning = s.soak?.campaign_status === "RUNNING";

    return (
        <AppLayout>
            <div data-testid="command-center-page">
                <PageHeader
                    title="Command Center"
                    subtitle="Soak · certifications · guard health · release canary — one glance"
                    testid="command-center-header"
                    action={
                        <div className="flex items-center gap-2">
                            <button onClick={() => load(true)} disabled={refreshing}
                                data-testid="cc-refresh-btn"
                                className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/40 flex items-center gap-1.5">
                                <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} /> REFRESH
                            </button>
                            <button onClick={exportEvidence} disabled={exporting || !data}
                                data-testid="cc-export-btn"
                                className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1.5">
                                {exporting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                                EVIDENCE REPORT
                            </button>
                        </div>
                    }
                />

                {error && (
                    <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/5 p-3 text-xs text-[#FF3B30] mb-4" data-testid="cc-error">
                        {error}
                    </div>
                )}

                {/* Overall banner */}
                <div className={`border ${overallTone.border} ${overallTone.bg} p-5 mb-4 flex items-center justify-between`}
                    data-testid="cc-overall-banner">
                    <div className="flex items-center gap-4">
                        <StatusDot status={data?.overall} size="w-5 h-5" />
                        <div>
                            <div className={`text-2xl font-mono font-bold tracking-widest ${overallTone.text}`}
                                data-testid="cc-overall-status">
                                {data ? data.overall : "LOADING…"}
                            </div>
                            <div className="text-xs text-[#71717A]">
                                {data?.at ? `as of ${new Date(data.at).toLocaleString()} · auto-refresh 30s` : ""}
                            </div>
                        </div>
                    </div>
                    <div className="text-right text-xs font-mono text-[#52525B]" data-testid="cc-provenance">
                        <div>commit {String(data?.provenance?.git_commit || "").slice(0, 12) || "—"}</div>
                        <div className="flex items-center gap-1.5 justify-end mt-1" data-testid="cc-email-config">
                            <Mail className="w-3 h-3" />
                            {data?.email_alerts_configured
                                ? <span className="text-[#00FF41]">email alerts ON</span>
                                : <span className="text-[#FFD700]">email alerts NOT CONFIGURED</span>}
                        </div>
                    </div>
                </div>

                {/* Section grid */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                    <SectionCard title="Soak Campaign" icon={Timer} section={s.soak} testid="cc-section-soak">
                        <Metric label="Day" testid="cc-soak-day"
                            value={s.soak?.day != null ? `${s.soak.day} / ${s.soak.days_target}` : "no campaign"} />
                        <Metric label="Days remaining" testid="cc-soak-remaining"
                            value={s.soak?.days_remaining != null
                                ? `${s.soak.days_remaining.toFixed(1)}${s.soak.ends_at ? ` · ends ${new Date(s.soak.ends_at).toLocaleDateString()}` : ""}`
                                : "—"} />
                        <div className="flex items-center justify-between py-0.5 text-xs" data-testid="cc-soak-today">
                            <span className="text-[#52525B]">Today's checkpoint</span>
                            <span className={`font-mono font-bold ${s.soak?.today_checkpoint_done ? "text-[#00FF41]" : "text-[#FFD700]"}`}>
                                {s.soak?.today_checkpoint_done == null ? "—" : (s.soak.today_checkpoint_done ? "RECORDED" : "DUE")}
                            </span>
                        </div>
                        <Metric label="Verdict" value={s.soak?.verdict || "—"} testid="cc-soak-verdict" />
                        <Metric label="Checkpoint coverage" testid="cc-soak-coverage"
                            value={s.soak?.coverage != null ? `${Math.round(s.soak.coverage * 100)}%` : "—"} />
                        <Metric label="Incidents" value={s.soak?.incidents} testid="cc-soak-incidents" />
                        {s.soak?.day != null && (
                            <div className="mt-2 h-1.5 bg-[#1F1F1F]" data-testid="cc-soak-progress">
                                <div className={`h-full ${tone(s.soak.status).dot}`}
                                    style={{ width: `${Math.min(100, (s.soak.day / (s.soak.days_target || 14)) * 100)}%` }} />
                            </div>
                        )}
                        <div className="flex gap-2 mt-3">
                            {s.soak?.day == null ? (
                                <ActionBtn testid="cc-soak-start-btn" disabled={!!acting}
                                    onClick={() => act("/ops/soak/start", {}, "14-day soak campaign started")}>
                                    START 14-DAY SOAK
                                </ActionBtn>
                            ) : (
                                <>
                                    <ActionBtn testid="cc-soak-checkpoint-btn"
                                        disabled={!!acting || !soakRunning || s.soak?.today_checkpoint_done === true}
                                        onClick={() => act("/ops/soak/checkpoint", {}, "Today's checkpoint recorded")}>
                                        RECORD CHECKPOINT
                                    </ActionBtn>
                                    <ActionBtn danger testid="cc-soak-reset-btn" disabled={!!acting}
                                        onClick={() => {
                                            if (window.confirm("Abort the current campaign and restart a fresh 14-day soak frozen at the CURRENT release?"))
                                                act("/ops/soak/reset", {}, "Soak campaign reset — fresh 14 days");
                                        }}>
                                        RESET
                                    </ActionBtn>
                                </>
                            )}
                        </div>
                        <div className="text-[10px] text-[#52525B] mt-2">
                            Auto-starts on new release · reminder at 12h · safety-net auto-checkpoint at 20h
                        </div>
                    </SectionCard>

                    <SectionCard title="Release Canary" icon={Bird} section={s.canary} testid="cc-section-canary">
                        <Metric label="Canary account" testid="cc-canary-account"
                            value={s.canary?.enabled
                                ? (s.canary.account_name || `…${String(s.canary.account_id || "").slice(-6)}`)
                                : "not set"} />
                        <Metric label="Canary block-rate" testid="cc-canary-rate"
                            value={s.canary?.canary_rate != null
                                ? `${Math.round(s.canary.canary_rate * 100)}% (${s.canary.canary_decisions} decisions)` : "—"} />
                        <Metric label="Fleet block-rate" testid="cc-canary-fleet-rate"
                            value={s.canary?.fleet_rate != null
                                ? `${Math.round(s.canary.fleet_rate * 100)}% (${s.canary.fleet_decisions} decisions)` : "—"} />
                        <Metric label="Window" testid="cc-canary-window"
                            value={s.canary?.window_hours != null ? `${s.canary.window_hours}h` : "—"} />
                        <Metric label="Release" testid="cc-canary-release"
                            value={s.canary?.release ? String(s.canary.release).slice(0, 12) : "—"} />
                        <div className="flex gap-2 mt-3 flex-wrap items-center">
                            {!s.canary?.enabled ? (
                                <>
                                    <input value={canaryAccount} onChange={(e) => setCanaryAccount(e.target.value)}
                                        placeholder="demo account ID"
                                        data-testid="cc-canary-account-input"
                                        className="bg-transparent border border-[#1F1F1F] px-2 py-1 text-[11px] font-mono text-[#FAFAFA] w-44 focus:border-[#00FF41]/40 outline-none" />
                                    <ActionBtn testid="cc-canary-enable-btn"
                                        disabled={!!acting || !canaryAccount.trim()}
                                        onClick={() => act("/ops/canary/enable", { account_id: canaryAccount.trim() }, "Release canary enabled")}>
                                        ENABLE
                                    </ActionBtn>
                                </>
                            ) : s.canary?.halted ? (
                                <>
                                    <ActionBtn testid="cc-canary-resume-btn" disabled={!!acting}
                                        onClick={() => {
                                            if (window.confirm("Reactivate the canary account's bots? Only resume after investigating the divergence."))
                                                act("/ops/canary/resume", {}, "Canary resumed — bots reactivated");
                                        }}>
                                        RESUME
                                    </ActionBtn>
                                    <ActionBtn danger testid="cc-canary-disable-btn" disabled={!!acting}
                                        onClick={() => act("/ops/canary/disable", {}, "Release canary disabled")}>
                                        DISABLE
                                    </ActionBtn>
                                </>
                            ) : (
                                <>
                                    <ActionBtn testid="cc-canary-evaluate-btn" disabled={!!acting}
                                        onClick={() => act("/ops/canary/evaluate", {}, "Canary evaluated")}>
                                        EVALUATE NOW
                                    </ActionBtn>
                                    <ActionBtn danger testid="cc-canary-disable-btn" disabled={!!acting}
                                        onClick={() => act("/ops/canary/disable", {}, "Release canary disabled")}>
                                        DISABLE
                                    </ActionBtn>
                                </>
                            )}
                        </div>
                        <div className="text-[10px] text-[#52525B] mt-2">
                            One demo account runs each new release ahead of the fleet — auto-halts if its guard-block rate diverges (≥20 decisions, +25pp / 3× fleet)
                        </div>
                    </SectionCard>

                    <SectionCard title="Certifications" icon={Award} section={s.certifications} testid="cc-section-certs">
                        <Metric label="Valid" value={s.certifications?.valid} testid="cc-certs-valid" />
                        <Metric label="Total issued" value={s.certifications?.total} testid="cc-certs-total" />
                        <Metric label="Expiring in 48h" value={s.certifications?.expiring_48h} testid="cc-certs-expiring" />
                        {(s.certifications?.latest || []).slice(0, 3).map(c => (
                            <div key={c.cert_id} className="flex items-center justify-between py-0.5 text-xs border-t border-[#1F1F1F] mt-1 pt-1">
                                <span className="text-[#52525B] font-mono">{c.kind} · {c.subject || "—"}</span>
                                <span className={`font-mono ${c.valid ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                    {c.valid ? "VALID" : (c.revoked ? "REVOKED" : "INVALID")}
                                </span>
                            </div>
                        ))}
                    </SectionCard>

                    <SectionCard title="Guard Health" icon={ShieldCheck} section={s.guard} testid="cc-section-guard">
                        <Metric label="Decisions · 24h" value={s.guard?.decisions_24h} testid="cc-guard-decisions" />
                        <Metric label="Blocked · 24h" value={s.guard?.blocked_24h} testid="cc-guard-blocked" />
                        <Metric label="Blocked · last hour" value={s.guard?.blocked_1h} testid="cc-guard-blocked-1h" />
                        <Metric label="RISK_UNKNOWN · last hour" value={s.guard?.risk_unknown_1h} testid="cc-guard-unknown" />
                        <Metric label="Stale telemetry alerts" value={s.guard?.stale_telemetry_alerts} testid="cc-guard-stale" />
                        {(s.guard?.top_block_reasons || []).map(r => (
                            <div key={r.reason} className="flex items-center justify-between py-0.5 text-xs border-t border-[#1F1F1F] mt-1 pt-1">
                                <span className="text-[#52525B] font-mono truncate mr-2">{r.reason}</span>
                                <span className="font-mono text-[#FAFAFA]">{r.count}</span>
                            </div>
                        ))}
                    </SectionCard>

                    <SectionCard title="Workers & Alerts" icon={Server} section={s.workers} testid="cc-section-workers">
                        <Metric label="Workers alive" testid="cc-workers-alive"
                            value={s.workers ? `${s.workers.workers_alive} / ${s.workers.workers_total}` : "—"} />
                        <Metric label="Crashloops" value={s.workers?.crashloops} testid="cc-workers-crashloops" />
                        <Metric label="Open critical alerts" value={s.workers?.open_critical_alerts} testid="cc-workers-critical" />
                    </SectionCard>
                </div>

                {/* Recent alerts + emails */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="border border-[#1F1F1F] p-4" data-testid="cc-recent-alerts">
                        <div className="flex items-center gap-2 mb-2">
                            <AlertTriangle className="w-4 h-4 text-[#FFD700]" />
                            <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">Recent ops alerts</span>
                        </div>
                        {(data?.recent_alerts || []).length === 0 && (
                            <div className="text-xs text-[#52525B]" data-testid="cc-alerts-empty">No alerts recorded.</div>
                        )}
                        {(data?.recent_alerts || []).map((a, i) => (
                            <div key={i} className="py-1 text-xs border-b border-[#141414] last:border-0">
                                <span className={`font-mono mr-2 ${a.severity === "critical" ? "text-[#FF3B30]" : "text-[#FFD700]"}`}>
                                    {String(a.severity || "").toUpperCase()}
                                </span>
                                <span className="text-[#A1A1AA]">{a.message}</span>
                                <span className="text-[#52525B] ml-2">
                                    ×{a.occurrences}{a.acked_at ? " · acked" : ""}
                                </span>
                            </div>
                        ))}
                    </div>
                    <div className="border border-[#1F1F1F] p-4" data-testid="cc-recent-emails">
                        <div className="flex items-center gap-2 mb-2">
                            <Mail className="w-4 h-4 text-[#00FF41]" />
                            <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">Guard alert emails</span>
                        </div>
                        {(data?.recent_alert_emails || []).length === 0 && (
                            <div className="text-xs text-[#52525B]" data-testid="cc-emails-empty">No guard alert emails yet.</div>
                        )}
                        {(data?.recent_alert_emails || []).map((e) => (
                            <div key={e._id} className="py-1 text-xs border-b border-[#141414] last:border-0">
                                <div className="text-[#A1A1AA] truncate">{e.subject || e._id}</div>
                                <div className="text-[#52525B] font-mono">
                                    {e.last_sent_at ? new Date(e.last_sent_at).toLocaleString() : ""}
                                    {" · "}sends ×{e.sends || 0}
                                    {e.last_error ? <span className="text-[#FFD700]"> · {e.last_error}</span>
                                        : (e.last_delivered != null ? ` · delivered ${e.last_delivered}` : "")}
                                </div>
                            </div>
                        ))}
                    </div>
                </div>

                <div className="flex items-center gap-2 mt-4 text-xs text-[#52525B]" data-testid="cc-footer">
                    <Activity className="w-3.5 h-3.5" />
                    Evidence report is hash-chained (sha256 per section + report hash) — any edit breaks verification.
                </div>
            </div>
        </AppLayout>
    );
}
