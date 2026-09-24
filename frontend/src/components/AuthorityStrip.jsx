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
    const [open, setOpen] = useState(false);
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
    const dec = data.decision || null;
    const STATE_TONE = { READY: TONE.FULL, DEGRADED: TONE.REDUCED, CLOSE_ONLY: TONE.CLOSE_ONLY, BLOCKED: TONE.LOCKED, EMERGENCY: TONE.EMERGENCY };
    return (
        <div className={`border-b ${unknown ? "border-[#FF3B30]/40 bg-[#FF3B30]/10" : "border-[#141414] bg-[#0A0A0A]"}`} data-testid="authority-strip">
            <div className="px-4 md:px-8 py-1.5 flex items-center gap-4 overflow-x-auto">
                {dec && (
                    <span className="flex items-center gap-1.5 shrink-0" data-testid="authority-decision">
                        <span className="font-mono text-[9px] text-[#52525B] tracking-widest">DECISION</span>
                        <span className={`font-mono text-[9px] px-1.5 py-0.5 border ${STATE_TONE[dec.state] || TONE.CLOSE_ONLY}`} data-testid="authority-decision-state" data-state={dec.state}>
                            {dec.state.replace(/_/g, " ")}
                        </span>
                        <span className="font-mono text-[9px] text-[#52525B]" data-testid="authority-decision-code">{dec.dominant_code}</span>
                        <button type="button" onClick={() => setOpen(o => !o)} data-testid="authority-drilldown-toggle"
                            className="font-mono text-[9px] text-[#A1A1AA] underline hover:text-white">
                            {open ? "hide" : "why"} · {dec.reason_codes.length}
                        </button>
                    </span>
                )}
                <Pill name="TRADING AUTHORITY" level={data.level} testid="authority-level" />
                <Pill name="BROKER" level={d.broker?.level} testid="authority-broker" />
                <Pill name="EXECUTION" level={d.execution?.level} testid="authority-execution" />
                <Pill name="POSITION TRUTH" level={d.position_truth?.level} testid="authority-position-truth" />
                <Pill name="RISK" level={d.risk?.level} testid="authority-risk" />
            </div>
            {(data.restricted || unknown || (dec && dec.state !== "READY")) && (
                <div className={`px-4 md:px-8 pb-1.5 font-mono text-[10px] ${unknown ? "text-[#FF3B30]" : "text-[#FFB000]"}`} data-testid="authority-reason">
                    {unknown ? "AUTHORITY UNKNOWN — " : ""}
                    {dec && !dec.new_exposure_allowed ? "New exposure is refused server-side. " : data.enforced_level !== "FULL" ? "New trades are reduced. " : ""}
                    {(dec?.reason_codes || []).slice(0, 4).join(" · ") || (data.reasons || []).slice(0, 3).join(" · ")}
                </div>
            )}
            {open && dec && (
                <div className="px-4 md:px-8 pb-2 grid gap-1" data-testid="authority-drilldown">
                    {dec.blockers.length === 0 && <span className="font-mono text-[10px] text-[#00FF41]">No blockers — READY (server decision {dec.decision_id}).</span>}
                    {dec.blockers.map((b, i) => (
                        <div key={`${b.domain}-${i}`} className="font-mono text-[10px] text-[#A1A1AA] flex gap-2" data-testid={`authority-blocker-${b.code}`}>
                            <span className={`px-1 border ${STATE_TONE[b.state] || TONE.CLOSE_ONLY}`}>{b.state}</span>
                            <span className="text-[#E4E4E7]">{b.code}</span>
                            <span>{b.domain}: {b.reason}</span>
                        </div>
                    ))}
                    {(dec.accounts || []).map(a => (
                        <div key={a.account_id} className="font-mono text-[10px] text-[#52525B]" data-testid={`authority-account-${a.account_id}`}>
                            {a.label || a.account_id} · {a.environment} → <span className="text-[#A1A1AA]">{a.state}</span>{a.reason_codes.length ? ` (${a.reason_codes.join(", ")})` : ""}
                        </div>
                    ))}
                    <span className="font-mono text-[9px] text-[#3F3F46]">computed {dec.computed_at} · stability window {dec.stability_window_seconds}s · dominance {dec.dominance.join(" > ")}</span>
                </div>
            )}
        </div>
    );
}
