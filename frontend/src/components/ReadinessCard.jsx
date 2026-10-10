import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Server, RefreshCw } from "lucide-react";

const CHECK_LABELS = {
    mongo_roundtrip: "MONGO ROUND TRIP",
    workers: "WORKER LEASES",
    reconciliation: "RECONCILIATION",
    outbox: "OUTBOX BACKLOG",
    schema: "SCHEMA COMPAT",
    unique_ticket_index: "TICKET INDEX",
    host_suitability: "HOST SUITABILITY",
    deploy_jam: "TRADING POSTURE",
};

function Pill({ ok, label, detail }) {
    const c = ok ? "border-[#00FF41]/30 bg-[#00FF41]/5 text-[#00FF41]"
        : "border-[#FF3B30]/30 bg-[#FF3B30]/5 text-[#FF3B30]";
    return (
        <div className={`border ${c} px-3 py-2`} data-testid={`readiness-check-${label.toLowerCase().replace(/ /g, "-")}`}>
            <div className="font-mono text-[9px] tracking-widest">{label}</div>
            <div className="font-mono text-xs mt-0.5">{ok ? "PASS" : "FAIL"}{detail ? ` · ${detail}` : ""}</div>
        </div>
    );
}

export const ReadinessCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await api.get("/ops/release-readiness");
            setD(data);
        } catch (e) {
            if (e.response?.status === 503 && e.response.data?.checks) {
                setD(e.response.data);       // not ready — still show detail
            } else {
                setHidden(true);             // 403 non-admin / unavailable
            }
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 30000);
        return () => clearInterval(id);
    }, [load]);

    if (hidden || !d) return null;
    const checks = d.checks || {};
    const workers = checks.workers?.detail || {};
    const detailFor = (k) => {
        if (k === "mongo_roundtrip") return checks[k]?.ms != null ? `${checks[k].ms}ms` : "";
        if (k === "workers") return `${Object.values(workers).filter(w => w.alive).length}/${Object.keys(workers).length || 6}`;
        if (k === "reconciliation") return checks[k]?.stuck_unresolved_gt_5m ? `${checks[k].stuck_unresolved_gt_5m} stuck` : "";
        if (k === "outbox") return `${checks[k]?.pending ?? 0} pending`;
        if (k === "schema") return `v${checks[k]?.code_version ?? "?"}`;
        if (k === "unique_ticket_index") return checks[k]?.ok ? "" : checks[k]?.stale ? "STALE" : checks[k]?.present === false ? "NOT BUILT" : "UNVERIFIED";
        if (k === "host_suitability") {
            const h = checks[k] || {};
            const base = h.shared_web_host ? "SHARED HOST" : h.profile === "dedicated" ? "DEDICATED" : "UNKNOWN";
            return `${base}${h.verified === false ? " · UNVERIFIED" : ""}${h.detected_at ? ` · ${new Date(h.detected_at).toLocaleDateString()}` : ""}`;
        }
        if (k === "deploy_jam") return checks[k]?.unknown ? "UNKNOWN" : checks[k]?.trading_paused ? "TRADING PAUSED" : "MANAGED";
        return "";
    };

    return (
        <div className={`border p-4 ${d.ready ? "border-[#00FF41]/30 bg-[#00FF41]/5" : "border-[#FF3B30]/30 bg-[#FF3B30]/5"}`}
            data-testid="readiness-card">
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <Server className={`w-4 h-4 ${d.ready ? "text-[#00FF41]" : "text-[#FF3B30]"}`} />
                    <span className="font-display font-bold text-sm text-white">Release Readiness</span>
                    <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${d.ready ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}
                        data-testid="readiness-verdict">
                        {d.ready ? "READY" : "NOT READY"}
                    </span>
                </div>
                <button onClick={load} disabled={loading} data-testid="readiness-refresh"
                    className="text-[#A1A1AA] hover:text-white">
                    <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                </button>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 mt-3">
                {Object.keys(CHECK_LABELS).filter(k => k !== "deploy_jam" || checks.deploy_jam).map(k => (
                    <Pill key={k} ok={!!checks[k]?.ok} label={CHECK_LABELS[k]} detail={detailFor(k)} />
                ))}
            </div>
            {checks.ea_release && checks.ea_release.release_key_pinned === false && (
                <div className="border border-[#FFB020]/40 bg-[#FFB020]/5 px-3 py-2 mt-3" data-testid="readiness-release-key-unpinned">
                    <div className="font-mono text-[9px] tracking-widest text-[#FFB020]">RELEASE KEY NOT PINNED</div>
                    <div className="font-mono text-xs text-[#E4E4E7] mt-0.5">
                        RELEASE_PUBLIC_KEY_B64 is empty on this host — CI-signed EA records cannot be verified.
                    </div>
                    {checks.ea_release.fix && (
                        <code className="block font-mono text-[11px] text-[#00FF41] mt-1" data-testid="readiness-release-key-fix">{checks.ea_release.fix}</code>
                    )}
                </div>
            )}
            {checks.release_key_rotation && (checks.release_key_rotation.in_transition || !checks.release_key_rotation.ok) && (
                <div className={`border px-3 py-2 mt-3 ${checks.release_key_rotation.ok ? "border-[#FFB020]/40 bg-[#FFB020]/5" : "border-[#FF3B30]/40 bg-[#FF3B30]/5"}`}
                    data-testid="readiness-release-key-rotation">
                    <div className={`font-mono text-[9px] tracking-widest ${checks.release_key_rotation.ok ? "text-[#FFB020]" : "text-[#FF3B30]"}`}>
                        RELEASE KEY ROTATION · {checks.release_key_rotation.ok ? "IN TRANSITION" : "MISCONFIGURED"}
                    </div>
                    <div className="font-mono text-xs text-[#E4E4E7] mt-0.5">{checks.release_key_rotation.detail}</div>
                    {checks.release_key_rotation.fix && (
                        <code className="block font-mono text-[11px] text-[#00FF41] mt-1" data-testid="readiness-release-key-rotation-fix">{checks.release_key_rotation.fix}</code>
                    )}
                </div>
            )}
            {checks.key_ages && checks.key_ages.due && checks.key_ages.due.length > 0 && (
                <div className="border border-[#FFB020]/40 bg-[#FFB020]/5 px-3 py-2 mt-3" data-testid="readiness-key-ages">
                    <div className="font-mono text-[9px] tracking-widest text-[#FFB020]">KEY / CERTIFICATE ROTATION DUE</div>
                    <div className="font-mono text-xs text-[#E4E4E7] mt-0.5">{checks.key_ages.detail}</div>
                    {checks.key_ages.fix && (
                        <code className="block font-mono text-[11px] text-[#00FF41] mt-1" data-testid="readiness-key-ages-fix">{checks.key_ages.fix}</code>
                    )}
                </div>
            )}
            {checks.host_suitability && checks.host_suitability.shared_web_host && (
                <div className={`border px-3 py-2 mt-3 ${checks.host_suitability.ok ? "border-[#FFB020]/40 bg-[#FFB020]/5" : "border-[#FF3B30]/40 bg-[#FF3B30]/5"}`}
                    data-testid="readiness-host-suitability">
                    <div className={`font-mono text-[9px] tracking-widest ${checks.host_suitability.ok ? "text-[#FFB020]" : "text-[#FF3B30]"}`}>
                        HOST SUITABILITY · {checks.host_suitability.ok ? "WARN (DEMO-ONLY)" : "BLOCKING"}
                    </div>
                    <div className="font-mono text-xs text-[#E4E4E7] mt-0.5">
                        {checks.host_suitability.detail}{checks.host_suitability.markers ? ` · detected: ${checks.host_suitability.markers}` : ""}
                        {checks.host_suitability.detected_at ? ` · recorded ${new Date(checks.host_suitability.detected_at).toLocaleString()}${checks.host_suitability.verified ? " (signed)" : " (UNVERIFIED)"}` : ""}
                    </div>
                </div>
            )}
            {checks.deploy_jam && checks.deploy_jam.trading_paused && (
                <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/5 px-3 py-2 mt-3" data-testid="readiness-trading-paused">
                    <div className="font-mono text-[9px] tracking-widest text-[#FF3B30]">TRADING PAUSED · DEPLOY JAM</div>
                    <div className="font-mono text-xs text-[#E4E4E7] mt-0.5">{checks.deploy_jam.detail}{checks.deploy_jam.since ? ` · since ${new Date(checks.deploy_jam.since).toLocaleString()}` : ""}</div>
                </div>
            )}
            {!d.ready && (
                <div className="font-mono text-[10px] tracking-widest text-[#A1A1AA] mt-3">
                    IN PREVIEW / SINGLE-PROCESS MODE, DEDICATED WORKER LEASES ARE EXPECTED TO BE ABSENT — FULL TOPOLOGY IS VERIFIED ON THE DOCKER DEPLOYMENT.
                </div>
            )}
        </div>
    );
};
