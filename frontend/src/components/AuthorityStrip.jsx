import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";

const TONE = {
    FULL: "text-[#00FF41] border-[#00FF41]/40",
    REDUCED: "text-[#FFB000] border-[#FFB000]/40",
    CLOSE_ONLY: "text-[#FF8C00] border-[#FF8C00]/50",
    PAUSED: "text-[#FF8C00] border-[#FF8C00]/50",
    EMERGENCY: "text-[#FF3B30] border-[#FF3B30]/50",
    LOCKED: "text-[#FF3B30] border-[#FF3B30]/50",
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
    const load = useCallback(async () => {
        try {
            const r = await api.get("/authority");
            setData(r.data);
        } catch { /* unauthenticated or transient — hide strip */ }
    }, []);
    useEffect(() => {
        load();
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);
    if (!data) return null;
    const d = data.domains || {};
    return (
        <div className="border-b border-[#141414] bg-[#0A0A0A]" data-testid="authority-strip">
            <div className="px-4 md:px-8 py-1.5 flex items-center gap-4 overflow-x-auto">
                <Pill name="TRADING AUTHORITY" level={data.level} testid="authority-level" />
                <Pill name="BROKER" level={d.broker?.level} testid="authority-broker" />
                <Pill name="EXECUTION" level={d.execution?.level} testid="authority-execution" />
                <Pill name="POSITION TRUTH" level={d.position_truth?.level} testid="authority-position-truth" />
                <Pill name="RISK" level={d.risk?.level} testid="authority-risk" />
            </div>
            {data.restricted && (
                <div className="px-4 md:px-8 pb-1.5 font-mono text-[10px] text-[#FFB000]" data-testid="authority-reason">
                    {data.enforced_level !== "FULL" ? "New trades are temporarily blocked. " : ""}
                    {(data.reasons || []).slice(0, 3).join(" · ")}
                </div>
            )}
        </div>
    );
}
