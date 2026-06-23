import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import api from "@/lib/api";
import { ShieldCheck, ShieldAlert, Shield, GitMerge } from "lucide-react";

/**
 * IntegrityWidget — passive broker↔DB drift monitor on the Dashboard.
 *
 * States:
 *   ok          — green dot, "in sync"
 *   drift       — amber dot, X mismatches detected → CTA to Trades / SYNC
 *   stale       — amber dot, no heartbeat in 60s+ (EA disconnected)
 *   no_account  — neutral dot, no MT5 account configured yet
 *
 * Polls /api/integrity/snapshot every 20s. WebSocket account_heartbeat events
 * trigger an immediate refetch via parent.
 */

const STATE_STYLE = {
    ok:         { dot: "#00FF41", label: "IN SYNC",      icon: ShieldCheck, hint: "DB matches the broker." },
    drift:      { dot: "#FFB000", label: "DRIFT",        icon: ShieldAlert, hint: "DB and broker disagree on at least one trade." },
    stale:      { dot: "#FFB000", label: "STALE",        icon: ShieldAlert, hint: "No EA heartbeat in over a minute." },
    no_account: { dot: "#52525B", label: "NO ACCOUNT",   icon: Shield,      hint: "Connect an MT5 account to monitor sync." },
};

function fmtAge(s) {
    if (s == null) return "—";
    if (s < 60)    return `${s}s`;
    if (s < 3600)  return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
}

export function IntegrityWidget({ refreshSignal }) {
    const [snap, setSnap] = useState(null);
    const [error, setError] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/integrity/snapshot");
            setSnap(data);
            setError(false);
        } catch {
            setError(true);
        }
    }, []);

    useEffect(() => { load(); }, [load, refreshSignal]);
    useEffect(() => {
        const id = setInterval(load, 20_000);
        return () => clearInterval(id);
    }, [load]);

    if (error || !snap) return null;

    const cfg = STATE_STYLE[snap.state] || STATE_STYLE.no_account;
    const Icon = cfg.icon;
    // Pick the most recently-synced account for the headline age
    const minAge = (snap.accounts || [])
        .map(a => a.last_sync_age_seconds)
        .filter(s => s != null)
        .reduce((a, b) => Math.min(a, b), Infinity);
    const ageDisplay = isFinite(minAge) ? fmtAge(minAge) : "—";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] px-4 py-3 flex items-center gap-4 flex-wrap"
            data-testid="integrity-widget">
            <div className="flex items-center gap-2.5">
                <span className={`w-2 h-2 rounded-full ${snap.state === "ok" ? "pulse-dot" : ""}`}
                    style={{ backgroundColor: cfg.dot }} />
                <Icon className="w-4 h-4" style={{ color: cfg.dot }} />
                <span className="font-mono text-[10px] tracking-widest" style={{ color: cfg.dot }}>
                    {cfg.label}
                </span>
            </div>

            <div className="flex items-center gap-5 font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                <span data-testid="integrity-age">
                    LAST SYNC · <span className="text-white">{ageDisplay} ago</span>
                </span>
                <span data-testid="integrity-mismatches">
                    {snap.total_mismatches === 0
                        ? <span><span className="text-[#00FF41]">0</span> MISMATCHES</span>
                        : <span><span className="text-[#FFB000]">{snap.total_mismatches}</span> MISMATCH{snap.total_mismatches === 1 ? "" : "ES"}</span>}
                </span>
                {snap.deleted_account_orphans > 0 && (
                    <span className="text-[#FF3B30]" data-testid="integrity-orphans">
                        {snap.deleted_account_orphans} ORPHAN{snap.deleted_account_orphans === 1 ? "" : "S"}
                    </span>
                )}
            </div>

            <div className="ml-auto flex items-center gap-3">
                <span className="font-mono text-[10px] text-[#52525B] tracking-wide hidden md:inline">{cfg.hint}</span>
                {(snap.total_mismatches > 0 || snap.deleted_account_orphans > 0) && (
                    <Link to="/trades"
                        data-testid="integrity-cta"
                        className="font-mono text-[10px] tracking-widest px-3 py-1.5 border border-[#FFB000]/40 text-[#FFB000] hover:bg-[#FFB000]/10 flex items-center gap-1.5">
                        <GitMerge className="w-3 h-3" /> SYNC NOW
                    </Link>
                )}
            </div>
        </div>
    );
}
