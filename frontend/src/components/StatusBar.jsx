import { useEffect, useState } from "react";
import { ShieldCheck, Lock, Server } from "lucide-react";
import api from "@/lib/api";

/**
 * StatusBar — slim trust strip pinned above the main app area.
 *
 * Review P1-10: no static "OPERATIONAL / 24/7 UPTIME" claims — each
 * dimension is LIVE from GET /api/status (api, database, bot engine,
 * EA bridge) and the overall state is the honest worst-of.
 */
const DIM_LABEL = {
    api: "API", database: "DB", bot_engine: "BOT", ea_bridge: "EA",
};
const TONE = {
    operational: "text-[#00FF41]",
    idle: "text-[#A1A1AA]",
    degraded: "text-[#FFB000]",
    major_outage: "text-[#FF3B30]",
};

export function StatusBar() {
    const [status, setStatus] = useState(null);
    const [readiness, setReadiness] = useState(null);
    const [inv, setInv] = useState(null);

    useEffect(() => {
        let alive = true;
        const load = () => {
            api.get("/status").then(r => { if (alive) setStatus(r.data); })
                .catch(() => { if (alive) setStatus({ overall: "unreachable" }); });
            api.get("/state/readiness").then(r => { if (alive) setReadiness(r.data); })
                .catch(() => { if (alive) setReadiness(null); });
            api.get("/state/inventory").then(r => { if (alive) setInv(r.data); })
                .catch(() => { if (alive) setInv(null); });
        };
        load();
        const t = setInterval(load, 60000);
        return () => { alive = false; clearInterval(t); };
    }, []);

    const overall = status?.overall;
    const overallTone = overall === "operational" ? "text-[#00FF41]"
        : overall == null ? "text-[#52525B]"
        : overall === "degraded" ? "text-[#FFB000]" : "text-[#FF3B30]";
    const dotTone = overall === "operational" ? "bg-[#00FF41] pulse-dot"
        : overall == null ? "bg-[#52525B]"
        : overall === "degraded" ? "bg-[#FFB000]" : "bg-[#FF3B30]";
    const comps = status?.components || {};
    const READY_TONE = {
        READY: "text-[#00FF41]", DEGRADED: "text-[#FFB000]",
        CLOSE_ONLY: "text-[#FFB000]", BLOCKED: "text-[#FF3B30]",
        EMERGENCY: "text-[#FF3B30]",
    };
    const ea = inv?.ea_connection;
    const eaLabel = ea
        ? `EA ${ea.fresh}/${inv.accounts_configured} FRESH`
          + (ea.stale ? ` · ${ea.stale} STALE` : "")
          + (ea.offline ? ` · ${ea.offline} OFF` : "")
          + (ea.paper ? ` · ${ea.paper} PAPER` : "")
        : null;

    return (
        <div
            className="hidden md:flex items-center justify-between gap-4 border-b border-[#1F1F1F] bg-[#0A0A0A] px-6 py-1.5"
            data-testid="status-bar">
            <div className="flex items-center gap-5 font-mono text-[10px] tracking-widest text-[#52525B]">
                {/* audit v3 P0-3 — trading readiness DOMINATES uptime wording */}
                {readiness?.level && (
                    <span className={`flex items-center gap-1.5 font-bold ${READY_TONE[readiness.level] || "text-[#A1A1AA]"}`}
                        data-testid="status-trading-readiness"
                        title={(readiness.reasons || []).map(r => `${r.code}: ${r.message}`).join(" · ")
                            || "trading readiness — independent of infrastructure uptime"}>
                        <span className={`w-1.5 h-1.5 rounded-full ${readiness.level === "READY" ? "bg-[#00FF41]" : readiness.level === "DEGRADED" || readiness.level === "CLOSE_ONLY" ? "bg-[#FFB000]" : "bg-[#FF3B30]"}`} />
                        TRADING · {readiness.level.replaceAll("_", " ")}
                    </span>
                )}
                <span className="flex items-center gap-1.5" data-testid="status-operational"
                    title={status?.checked_at ? `infrastructure uptime only — NOT trading readiness · checked ${new Date(status.checked_at).toLocaleTimeString()}` : "infrastructure uptime only"}>
                    <span className={`w-1.5 h-1.5 rounded-full ${dotTone}`} />
                    <span className={overallTone}>
                        {overall ? `INFRA · ${String(overall).replaceAll("_", " ").toUpperCase()}` : "INFRA · …"}
                    </span>
                </span>
                <span className="hidden lg:flex items-center gap-2.5" data-testid="status-dimensions">
                    {Object.entries(DIM_LABEL).map(([key, label]) => {
                        if (key === "ea_bridge" && eaLabel) {
                            return (
                                <span key={key} className="flex items-center gap-1"
                                    title={`exact EA connectivity (threshold ${ea.threshold_seconds}s) — as of ${inv.as_of}`}
                                    data-testid="status-dim-ea_bridge">
                                    <span className={ea.offline || ea.stale ? "text-[#FFB000]" : "text-[#00FF41]"}>{eaLabel}</span>
                                </span>
                            );
                        }
                        const st = comps[key]?.status;
                        return (
                            <span key={key} className="flex items-center gap-1"
                                title={`${label}: ${st || "unknown"}${comps[key]?.note ? ` — ${comps[key].note}` : ""}`}
                                data-testid={`status-dim-${key}`}>
                                <span className={TONE[st] || "text-[#52525B]"}>{label}</span>
                                <span className={`w-1 h-1 rounded-full ${st === "operational" ? "bg-[#00FF41]" : st === "idle" ? "bg-[#52525B]" : st ? "bg-[#FFB000]" : "bg-[#1F1F1F]"}`} />
                            </span>
                        );
                    })}
                </span>
                <span className="hidden xl:flex items-center gap-1.5">
                    <Lock className="w-2.5 h-2.5" />
                    ENCRYPTED LOGIN
                </span>
                {status?.deployment?.region && (
                    <span className="hidden xl:flex items-center gap-1.5" data-testid="statusbar-region">
                        <Server className="w-2.5 h-2.5" />
                        {status.deployment.region.toUpperCase()} SERVERS
                    </span>
                )}
            </div>
            <div className="flex items-center gap-2 font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                <ShieldCheck className="w-3 h-3 text-[#00FF41]" />
                <span>YOUR FUNDS STAY WITH YOUR BROKER</span>
                <span className="hidden xl:inline text-[#52525B]">· STOIC IS SOFTWARE, NOT A BROKER</span>
            </div>
        </div>
    );
}
