import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Activity, CheckCircle2, AlertTriangle, RefreshCw } from "lucide-react";

const LABELS = {
    fred:     "FRED Macro",
    news:     "News",
    calendar: "Economic Calendar",
    ea:       "MT5 EA Heartbeat",
    agents:   "Agent Pipeline",
};

function fmtAge(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
}

export default function DataFreshnessStrip() {
    const [data, setData] = useState(null);
    const [refreshing, setRefreshing] = useState(false);
    const [err, setErr] = useState("");

    const load = async () => {
        setRefreshing(true);
        try {
            const { data: d } = await api.get("/data-freshness");
            setData(d); setErr("");
        } catch (e) { setErr(formatApiError(e)); }
        finally { setRefreshing(false); }
    };

    useEffect(() => {
        load();
        const id = setInterval(load, 60_000); // refresh every minute
        return () => clearInterval(id);
    }, []);

    if (err) return null; // silent — strip is non-critical
    if (!data) return null;

    const sources = ["fred", "news", "calendar", "ea", "agents"];
    const staleCount = sources.filter(s => data[s]?.stale).length;

    return (
        <div className="border border-[#1F1F1F] bg-[#050505] p-3 mb-4" data-testid="data-freshness-strip">
            <div className="flex items-center justify-between mb-2">
                <div className="flex items-center gap-2">
                    <Activity className={`w-4 h-4 ${staleCount === 0 ? "text-[#00FF41]" : "text-[#FFB000]"}`} />
                    <span className="font-mono text-[10px] tracking-widest text-[#52525B]">DATA FRESHNESS</span>
                    {staleCount > 0 ? (
                        <span className="font-mono text-[10px] text-[#FFB000]" data-testid="freshness-stale-badge">
                            {staleCount} STALE
                        </span>
                    ) : (
                        <span className="font-mono text-[10px] text-[#00FF41]">ALL CURRENT</span>
                    )}
                </div>
                <button onClick={load} disabled={refreshing}
                    data-testid="data-freshness-refresh"
                    className="text-[#52525B] hover:text-white">
                    <RefreshCw className={`w-3 h-3 ${refreshing ? "animate-spin" : ""}`} />
                </button>
            </div>
            <div className="grid grid-cols-5 gap-2">
                {sources.map(s => {
                    const v = data[s] || {};
                    const ageKey = s === "ea" ? "last_heartbeat_at" : "last_fetched_at";
                    return (
                        <div key={s} className={`border ${v.stale ? "border-[#FFB000]/40 bg-[#FFB000]/5" : "border-[#1F1F1F]"} px-2 py-1.5`}
                            data-testid={`freshness-${s}`}>
                            <div className="flex items-center gap-1.5">
                                {v.stale ? (
                                    <AlertTriangle className="w-3 h-3 text-[#FFB000] shrink-0" />
                                ) : v[ageKey] ? (
                                    <CheckCircle2 className="w-3 h-3 text-[#00FF41] shrink-0" />
                                ) : (
                                    <span className="w-3 h-3 inline-block rounded-full border border-[#52525B] shrink-0" />
                                )}
                                <span className="font-mono text-[9px] text-[#A1A1AA] tracking-widest truncate">{LABELS[s]}</span>
                            </div>
                            <div className="font-mono text-[10px] text-white mt-0.5 truncate">
                                {fmtAge(v.age_seconds)}
                            </div>
                            {s === "ea" && v.account_count > 0 && (
                                <div className="font-mono text-[8px] text-[#52525B] mt-0.5">
                                    {v.connected_count}/{v.account_count} connected
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
