import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";

const TONE = {
    FULL: "text-[#00FF41] border-[#00FF41]/40",
    REDUCED: "text-[#FFB000] border-[#FFB000]/40",
    CLOSE_ONLY: "text-[#FF8C00] border-[#FF8C00]/50",
    PAUSED: "text-[#FF8C00] border-[#FF8C00]/50",
    EMERGENCY: "text-[#FF3B30] border-[#FF3B30]/50",
    LOCKED: "text-[#FF3B30] border-[#FF3B30]/50",
    STALE: "text-[#FF3B30] border-[#FF3B30]/50",
    UNKNOWN: "text-[#FF3B30] border-[#FF3B30]/50",
    CONFLICTED: "text-[#FF3B30] border-[#FF3B30]/50",
};

const Pill = ({ name, level, testid }) => (
    <span className="flex items-center gap-1.5 shrink-0" data-testid={testid}>
        <span className="font-mono text-[9px] text-[#52525B] tracking-widest">{name}</span>
        <span className={`font-mono text-[9px] px-1.5 py-0.5 border ${TONE[level] || TONE.CLOSE_ONLY}`}>
            {(level || "?").replace(/_/g, " ")}
        </span>
    </span>
);

export function AuthorityStrip() {
    const [data, setData] = useState(null);
    const [failure, setFailure] = useState(null); // { since, status }
    const load = useCallback(async () => {
        try {
            const r = await api.get("/authority");
            setData(r.data);
            setFailure(null);
        } catch (e) {
            const status = e?.response?.status;
            if (status === 401 || status === 403) { setFailure(null); return; } // unauthenticated — no strip
            // audit P1-1 — never hide uncertainty: keep the LAST verdict visible with its age
            // and show a persistent UNKNOWN banner. A cached FULL is never shown as FULL.
            setFailure(prev => prev || { since: Date.now(), status: status || "network" });
        }
    }, []);
    useEffect(() => {
        load();
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);
    if (failure) {
        const ageS = Math.round((Date.now() - failure.since) / 1000);
        return (
            <div className="border-b border-[#FF3B30]/40 bg-[#FF3B30]/10" data-testid="authority-strip">
                <div className="px-4 md:px-8 py-1.5 flex items-center gap-4 overflow-x-auto">
                    <Pill name="TRADING AUTHORITY" level="UNKNOWN" testid="authority-level" />
                    <span className="font-mono text-[10px] text-[#FF3B30]" data-testid="authority-unknown-banner">
                        Authority state UNAVAILABLE ({failure.status}) for {ageS}s — new trades are refused until it can be confirmed.
                        {data?.level ? ` Last known verdict: ${data.level === "FULL" ? "not shown (may be stale)" : data.level}.` : ""}
                    </span>
                </div>
            </div>
        );
    }
    if (!data) return null;
    const d = data.domains || {};
    const unknown = data.authority_state === "UNKNOWN";
    return (
        <div className={`border-b ${unknown ? "border-[#FF3B30]/40 bg-[#FF3B30]/10" : "border-[#141414] bg-[#0A0A0A]"}`} data-testid="authority-strip">
            <div className="px-4 md:px-8 py-1.5 flex items-center gap-4 overflow-x-auto">
                <Pill name="TRADING AUTHORITY" level={data.level} testid="authority-level" />
                <Pill name="BROKER" level={d.broker?.level} testid="authority-broker" />
                <Pill name="EXECUTION" level={d.execution?.level} testid="authority-execution" />
                <Pill name="POSITION TRUTH" level={d.position_truth?.level} testid="authority-position-truth" />
                <Pill name="RISK" level={d.risk?.level} testid="authority-risk" />
            </div>
            {(data.restricted || unknown) && (
                <div className={`px-4 md:px-8 pb-1.5 font-mono text-[10px] ${unknown ? "text-[#FF3B30]" : "text-[#FFB000]"}`} data-testid="authority-reason">
                    {unknown ? "AUTHORITY UNKNOWN — " : ""}
                    {data.enforced_level !== "FULL" ? "New trades are temporarily blocked. " : ""}
                    {(data.reasons || []).slice(0, 3).join(" · ")}
                </div>
            )}
        </div>
    );
}
