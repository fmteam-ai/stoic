import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Cpu, ShieldAlert, ShieldCheck, AlertTriangle } from "lucide-react";

// Fallback for the latest EA build expected in production. The component
// also reads the live value from /api/bot/health-score so this constant
// only matters if that endpoint is unreachable. iter-76: 1.34.
export const LATEST_EA_VERSION = "1.40";

// Lightweight semver compare — handles dotted numeric strings only (1.25, 1.26).
// Returns -1 if a<b, 0 if equal, 1 if a>b. Non-numeric segments return 0.
function compareVersions(a, b) {
    if (!a || !b) return -1;
    const pa = String(a).split(".").map(n => parseInt(n, 10));
    const pb = String(b).split(".").map(n => parseInt(n, 10));
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
        const x = Number.isFinite(pa[i]) ? pa[i] : 0;
        const y = Number.isFinite(pb[i]) ? pb[i] : 0;
        if (x < y) return -1;
        if (x > y) return 1;
    }
    return 0;
}

function classifyEa(account, latestVersion = LATEST_EA_VERSION) {
    // Paper accounts never run an EA — show as N/A.
    if ((account.mode || "live") === "paper") {
        return { kind: "paper", label: "PAPER · NO EA", icon: ShieldCheck, color: "#A1A1AA" };
    }
    if (account.status !== "connected") {
        return { kind: "offline", label: "OFFLINE", icon: ShieldAlert, color: "#52525B" };
    }
    const v = account.ea_version;
    if (!v) {
        return {
            kind: "unknown",
            label: "OLD EA · UPGRADE",
            icon: AlertTriangle,
            color: "#FF3B30",
            detail: `Pre-v${latestVersion} EA detected (no version reported). Recompile EmergentTradingBridge.mq5 in MetaEditor (F7) to enable broker-real-time tick streaming, broker-symbol auto-detect, and auto suffix discovery.`,
        };
    }
    const cmp = compareVersions(v, latestVersion);
    if (cmp >= 0) {
        return {
            kind: "current",
            label: `v${v} · CURRENT`,
            icon: ShieldCheck,
            color: "#00FF41",
        };
    }
    return {
        kind: "outdated",
        label: `v${v} · OUTDATED`,
        icon: AlertTriangle,
        color: "#FFB000",
        detail: `Latest is v${latestVersion}. Recompile in MetaEditor → press F7.`,
    };
}

export function EaVersionStrip({ refreshSignal }) {
    const [accounts, setAccounts] = useState([]);
    const [loading, setLoading] = useState(true);
    // iter-76 · Live-fetched latest version from the backend so this component
    // can never drift from the actual deployed EA. Falls back to LATEST_EA_VERSION
    // if /api/bot/health-score is unreachable.
    const [latestEa, setLatestEa] = useState(LATEST_EA_VERSION);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [accountsRes, healthRes] = await Promise.all([
                api.get("/accounts"),
                api.get("/bot/health-score").catch(() => null),
            ]);
            setAccounts(Array.isArray(accountsRes.data) ? accountsRes.data : []);
            const serverLatest = healthRes?.data?.context?.ea_latest_version;
            if (serverLatest) setLatestEa(String(serverLatest));
        } catch {
            setAccounts([]);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load, refreshSignal]);

    // Hide entirely if user has no accounts yet (clean dashboard for first-run).
    if (!loading && accounts.length === 0) return null;

    // Only highlight live accounts; collapse paper ones into a small footnote.
    const liveAccounts = accounts.filter(a => (a.mode || "live") !== "paper");
    if (liveAccounts.length === 0) return null;

    const outdatedCount = liveAccounts.filter(a => {
        const c = classifyEa(a, latestEa);
        return c.kind === "outdated" || c.kind === "unknown";
    }).length;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="ea-version-strip">
            <div className="border-b border-[#1F1F1F] px-5 py-3 flex items-center gap-3 flex-wrap">
                <Cpu className="w-4 h-4 text-[#00FF41]" />
                <div className="font-display text-base">EA Version</div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    LATEST · v{latestEa}
                </div>
                {outdatedCount > 0 && (
                    <div className="ml-auto font-mono text-[10px] tracking-widest text-[#FFB000] border border-[#FFB000]/40 bg-[#FFB000]/10 px-2.5 py-1"
                         data-testid="ea-outdated-warning">
                        {outdatedCount} TERMINAL{outdatedCount === 1 ? "" : "S"} NEED UPGRADE
                    </div>
                )}
            </div>
            <div className="divide-y divide-[#1F1F1F]" data-testid="ea-version-list">
                {liveAccounts.map(a => {
                    const c = classifyEa(a, latestEa);
                    const Icon = c.icon;
                    return (
                        <div key={a.id} data-testid={`ea-row-${a.id}`}
                             className="px-5 py-3 flex items-center gap-3 flex-wrap">
                            <Icon className="w-3.5 h-3.5 shrink-0" style={{ color: c.color }} />
                            <div className="font-display text-sm tracking-tight truncate min-w-0 flex-shrink-0">
                                {a.label}
                            </div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">
                                {a.broker}{a.account_type ? ` · ${a.account_type.toUpperCase()}` : ""}
                            </div>
                            <div className="ml-auto font-mono text-[10px] tracking-widest px-2.5 py-1 border"
                                 style={{
                                     color: c.color,
                                     borderColor: `${c.color}40`,
                                     background: `${c.color}10`,
                                 }}
                                 data-testid={`ea-badge-${a.id}`}>
                                {c.label}
                            </div>
                        </div>
                    );
                })}
            </div>
            {outdatedCount > 0 && (
                <div className="border-t border-[#1F1F1F] px-5 py-3 font-mono text-[10px] text-[#A1A1AA] tracking-widest">
                    <span className="text-[#FFD700]">UPGRADE STEP:</span>{" "}
                    Open MetaEditor → load <span className="text-white">EmergentTradingBridge.mq5</span> →
                    press <span className="text-white">F7</span> to compile → refresh the MT5 Navigator panel →
                    re-drag the EA onto each chart.
                </div>
            )}
        </div>
    );
}
