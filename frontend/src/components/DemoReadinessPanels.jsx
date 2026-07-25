import { useEffect, useState } from "react";
import api from "@/lib/api";

const TIER_CLS = {
    CERTIFIED: "text-[#00FF41] border-[#00FF41]/40",
    ACCEPTABLE: "text-[#0099FF] border-[#0099FF]/40",
    PROVISIONAL: "text-[#FFD700] border-[#FFD700]/40",
    DEGRADED: "text-[#FF3B30] border-[#FF3B30]/40",
};

export function BrokerQualificationMatrix() {
    const [q, setQ] = useState(null);
    useEffect(() => {
        api.get("/broker-intel/qualification").then(({ data }) => setQ(data)).catch(() => {});
    }, []);
    if (!q?.accounts?.length) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 mb-4" data-testid="broker-qualification">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                BROKER QUALIFICATION MATRIX · EVIDENCE-BASED CERTIFICATION
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-3">{q.note}</div>
            {q.accounts.map((a) => (
                <div key={a.account_id} className="py-2 border-t border-[#141414]"
                    data-testid={`qualification-${a.account_id}`}>
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className={`font-mono text-[9px] px-2 py-0.5 border ${TIER_CLS[a.tier] || TIER_CLS.PROVISIONAL}`}
                            data-testid={`qualification-tier-${a.account_id}`}>{a.tier}</span>
                        <span className="font-mono text-[10px] text-white">{a.label || a.broker}</span>
                        <span className="font-mono text-[9px] text-[#52525B]">{a.server}</span>
                        <span className="ml-auto font-mono text-[9px] text-[#52525B]">{a.detail}</span>
                    </div>
                    <div className="flex flex-wrap gap-1.5 mt-1.5">
                        {Object.entries(a.checks || {}).map(([k, c]) => (
                            <span key={k} title={c.note || ""}
                                className={`font-mono text-[8px] px-1.5 py-0.5 border ${c.status === "observed" ? "text-[#A1A1AA] border-[#27272A]" : "text-[#3F3F46] border-[#141414]"}`}>
                                {k.replace(/_/g, " ")}: {c.status === "observed" ? String(c.value) : "n/r"}
                            </span>
                        ))}
                    </div>
                </div>
            ))}
        </div>
    );
}

export function SoakReportCard() {
    const [s, setS] = useState(null);
    const [forbidden, setForbidden] = useState(false);
    useEffect(() => {
        api.get("/ops/soak?days=14").then(({ data }) => setS(data))
            .catch((e) => { if (e?.response?.status === 403) setForbidden(true); });
    }, []);
    if (forbidden || !s) return null;
    const m = s.memory || {};
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="soak-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                LONG SOAK · {s.days}D CONTINUOUS-RUN TELEMETRY
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">MEMORY (RSS)</div>
                    <div className="font-mono text-xs text-white" data-testid="soak-memory">
                        {m.last_mb ?? "—"} MB {m.growth_pct != null && (
                            <span className={m.growth_pct > 20 ? "text-[#FF3B30]" : "text-[#00FF41]"}> ({m.growth_pct >= 0 ? "+" : ""}{m.growth_pct}%)</span>
                        )}
                    </div>
                </div>
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">WORKER RESTART EVENTS</div>
                    <div className={`font-mono text-xs ${s.worker_restart_events ? "text-[#FFD700]" : "text-[#00FF41]"}`} data-testid="soak-restarts">{s.worker_restart_events}</div>
                </div>
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">MISSED HEARTBEATS</div>
                    <div className={`font-mono text-xs ${s.missed_heartbeat_samples ? "text-[#FFD700]" : "text-[#00FF41]"}`} data-testid="soak-heartbeats">{s.missed_heartbeat_samples}</div>
                </div>
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">RECON BACKLOG</div>
                    <div className={`font-mono text-xs ${s.reconciliation_backlog ? "text-[#FF3B30]" : "text-[#00FF41]"}`} data-testid="soak-recon">{s.reconciliation_backlog}</div>
                </div>
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mt-2">
                {s.samples} samples · chaos {s.last_chaos?.passed ?? "—"}/{s.last_chaos?.total ?? "—"} · suppressed failures: {Object.keys(s.suppressed_failures || {}).length} components
            </div>
        </div>
    );
}
