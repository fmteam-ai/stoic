import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { Timer } from "lucide-react";

const SEGS = [
    ["strategy_ms", "STOIC CLOUD DECISION"],
    ["risk_authority_ms", "RISK + AUTHORITY"],
    ["cloud_to_ea_ms", "CLOUD → VPS/EA"],
    ["ea_processing_ms", "EA PROCESSING"],
    ["broker_ms", "BROKER EXECUTION"],
    ["total_ms", "TOTAL"],
];
const ms = (v) => (v == null ? "—" : `${v} ms`);

export function LatencyPanel() {
    const [data, setData] = useState(null);
    const [days, setDays] = useState(7);
    const load = useCallback(async () => {
        try {
            const r = await api.get(`/latency/summary?days=${days}`);
            setData(r.data);
        } catch { /* non-fatal */ }
    }, [days]);
    useEffect(() => { load(); }, [load]);
    if (!data) return null;
    const rows = (data.groups || []).slice(0, 12);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="latency-panel">
            <div className="px-4 py-3 border-b border-[#141414] flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Timer className="w-4 h-4 text-[#00FF41]" />
                    <div className="font-display font-bold text-sm">T0→T9 Latency Profiler</div>
                    <span className="font-mono text-[10px] text-[#52525B]">tick received → broker acknowledgement, independently measurable</span>
                </div>
                <div className="flex items-center gap-1">
                    {[1, 7, 30].map(d => (
                        <button key={d} onClick={() => setDays(d)} data-testid={`latency-days-${d}`}
                            className={`font-mono text-[10px] px-2 py-0.5 border ${days === d ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>
                            {d}D
                        </button>
                    ))}
                </div>
            </div>
            {rows.length === 0 ? (
                <div className="px-4 py-6 text-center font-mono text-xs text-[#52525B]" data-testid="latency-empty">
                    No fully-traced trades yet ({data.traced_trades ?? 0} traced) — every new live trade now carries T0→T9 marks from the triggering tick through broker acknowledgement.
                </div>
            ) : (
                <div className="px-4 py-3 overflow-x-auto">
                    <table className="w-full font-mono text-[10px]" data-testid="latency-table">
                        <thead>
                            <tr className="text-[#52525B] text-left">
                                <th className="py-1 pr-3">BROKER</th>
                                <th className="py-1 pr-3">SYMBOL</th>
                                <th className="py-1 pr-3">SESSION</th>
                                <th className="py-1 pr-3 text-right">TRADES</th>
                                {SEGS.map(([k, label]) => (
                                    <th key={k} className="py-1 pl-3 text-right">{label}<br />p50 / p95</th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {rows.map((r, i) => (
                                <tr key={i} className="border-t border-[#141414] text-[#A1A1AA]"
                                    data-testid={`latency-row-${i}`}>
                                    <td className="py-1.5 pr-3 text-white">{r.broker}</td>
                                    <td className="py-1.5 pr-3">{r.symbol}</td>
                                    <td className="py-1.5 pr-3 uppercase">{r.session}</td>
                                    <td className="py-1.5 pr-3 text-right">{r.trades}</td>
                                    {SEGS.map(([k]) => (
                                        <td key={k} className={`py-1.5 pl-3 text-right ${k === "total_ms" ? "text-[#00FF41]" : ""}`}>
                                            {ms(r[k]?.p50)} / {ms(r[k]?.p95)}
                                        </td>
                                    ))}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}
