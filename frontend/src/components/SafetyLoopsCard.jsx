import { useCallback, useEffect, useState } from "react";
import { Activity, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const ago = (sec) => {
    if (sec == null) return "never";
    if (sec < 90) return `${sec}s ago`;
    if (sec < 5400) return `${Math.round(sec / 60)}m ago`;
    if (sec < 172800) return `${Math.round(sec / 3600)}h ago`;
    return `${Math.round(sec / 86400)}d ago`;
};

// v1.60.39 — every background loop's last completed iteration (GET /ops/loops = loop_progress), so a loop that
// silently stops (13 Oct intent sweep, 14 Oct deploy watch) is visible here.
export function SafetyLoopsCard() {
    const [d, setD] = useState(null);
    const load = useCallback(() => api.get("/ops/loops").then((r) => setD(r.data)).catch(() => setD({ loops: [], error: true })), []);
    useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
    const stale = (d?.loops || []).filter((l) => !l.fresh);
    return (
        <div className={`border ${stale.length ? "border-[#FFB000]/40" : "border-[#1F1F1F]"} bg-[#0A0A0A]`} data-testid="safety-loops-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Activity className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Background Safety Loops</span>
                <span className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${stale.length ? "border-[#FFB000]/40 text-[#FFB000]" : "border-[#00FF41]/40 text-[#00FF41]"}`}
                    data-testid="safety-loops-verdict">
                    {d ? (stale.length ? `${stale.length} STALE` : `${(d.loops || []).length} RUNNING`) : "…"}
                </span>
                {d?.deploy_watch_pending && <span className="font-mono text-[9px] tracking-widest text-[#FFB000]">DEPLOY WATCH PENDING</span>}
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">{d?.in_process_workers ? "IN-PROCESS" : "EXTERNAL WORKERS"}</span>
                <button onClick={load} className="text-[#52525B] hover:text-white" data-testid="safety-loops-refresh" aria-label="Refresh"><RefreshCw className="w-3 h-3" /></button>
            </div>
            <div className="px-4 py-2 max-h-72 overflow-y-auto">
                {d?.error && <div className="font-mono text-[11px] text-[#FF3B30]">unavailable</div>}
                {d && !d.error && d.loops.length === 0 && <div className="font-mono text-[11px] text-[#52525B]">no loop progress recorded yet</div>}
                {(d?.loops || []).map((l) => (
                    <div key={l.loop} className="flex justify-between gap-3 py-1 border-b border-[#141414] last:border-0 font-mono text-[11px]"
                        data-testid={`safety-loop-${l.loop}`}>
                        <span className={l.fresh ? "text-[#A1A1AA]" : "text-[#FFB000]"}>{l.loop}</span>
                        <span className="text-[#71717A] text-right">
                            {ago(l.age_sec)}<span className="text-[#52525B]"> · every {l.expected_interval_sec}s</span>
                        </span>
                    </div>
                ))}
            </div>
        </div>
    );
}

export default SafetyLoopsCard;
