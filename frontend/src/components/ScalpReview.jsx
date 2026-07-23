import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Microscope } from "lucide-react";

const DOW = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

function heatColor(cell) {
    if (!cell) return "#0D0D0D";
    if (!cell.labeled) return "#16202B";
    const v = cell.avg_net_pips;
    if (v == null) return "#16202B";
    if (v > 0.5) return "#0E4020";
    if (v > 0) return "#123020";
    if (v > -0.5) return "#3A2A10";
    return "#401515";
}

export function ScalpReview() {
    const [d, setD] = useState(null);

    useEffect(() => {
        api.get("/scalp/review").then(r => setD(r.data)).catch(() => {});
    }, []);

    if (!d) return null;
    const byKey = {};
    (d.heatmap || []).forEach(c => { byKey[`${c.dow}:${c.hour}`] = c; });
    const hours = [...new Set((d.heatmap || []).map(c => c.hour))].sort((a, b) => a - b);
    const costs = d.cost_attribution || {};
    const lat = d.latency || {};

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] mt-6" data-testid="scalp-review-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Microscope className="w-3.5 h-3.5 text-[#BF5AF2]" />
                <span className="font-display font-bold text-sm">Scalp Review</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    HEATMAP · GATE EFFECTIVENESS · BROKER CALIBRATION · COST ATTRIBUTION
                </span>
            </div>

            <div className="p-4 grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div>
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">
                        EXECUTION HEATMAP — avg shadow net pips by day × hour (UTC)
                    </div>
                    {hours.length === 0 && (
                        <div className="font-mono text-[10px] text-[#52525B]" data-testid="scalp-heatmap-empty">
                            no decisions recorded yet
                        </div>
                    )}
                    {hours.length > 0 && (
                        <div className="overflow-x-auto" data-testid="scalp-heatmap">
                            <table className="border-collapse">
                                <thead>
                                    <tr>
                                        <th className="font-mono text-[8px] text-[#52525B] pr-1 text-left"> </th>
                                        {hours.map(h => (
                                            <th key={h} className="font-mono text-[8px] text-[#52525B] px-0.5">{h}</th>
                                        ))}
                                    </tr>
                                </thead>
                                <tbody>
                                    {DOW.map((label, dow) => {
                                        const has = hours.some(h => byKey[`${dow}:${h}`]);
                                        if (!has) return null;
                                        return (
                                            <tr key={dow}>
                                                <td className="font-mono text-[8px] text-[#52525B] pr-1">{label}</td>
                                                {hours.map(h => {
                                                    const c = byKey[`${dow}:${h}`];
                                                    return (
                                                        <td key={h} className="p-0.5">
                                                            <div className="w-7 h-6 flex items-center justify-center font-mono text-[8px] text-[#A1A1AA]"
                                                                style={{ background: heatColor(c) }}
                                                                title={c ? `${c.n} decisions · ${c.labeled} labeled · avg ${c.avg_net_pips ?? "—"}p` : ""}>
                                                                {c ? c.n : ""}
                                                            </div>
                                                        </td>
                                                    );
                                                })}
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    )}

                    <div className="mt-4" data-testid="scalp-cost-attribution">
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">
                            COST / LATENCY ATTRIBUTION ({costs.labeled ?? 0} LABELED)
                        </div>
                        <div className="flex flex-wrap gap-2 font-mono text-[10px]">
                            <span className="px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]">GROSS <span className="text-white">{costs.gross_pips ?? "—"}p</span></span>
                            <span className="px-2 py-1 border border-[#FFB000]/40 text-[#FFB000]">SPREAD −{costs.spread_pips ?? "—"}p</span>
                            <span className="px-2 py-1 border border-[#FFB000]/40 text-[#FFB000]">SLIPPAGE −{costs.slippage_pips ?? "—"}p</span>
                            <span className="px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]">COMM −{costs.commission_pips ?? "—"}p</span>
                            <span className={`px-2 py-1 border ${Number(costs.net_pips) >= 0 ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}>
                                NET {costs.net_pips ?? "—"}p
                            </span>
                            <span className="px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]">
                                EXIT p50 <span className="text-white">{lat.time_to_exit_p50_s ?? "—"}s</span> · p95 <span className="text-white">{lat.time_to_exit_p95_s ?? "—"}s</span>
                            </span>
                        </div>
                    </div>
                </div>

                <div>
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">
                        GATE EFFECTIVENESS — shadow outcome of vetoed candidates
                    </div>
                    <div data-testid="scalp-gate-effectiveness">
                        {(d.gate_effectiveness || []).length === 0 && (
                            <div className="font-mono text-[10px] text-[#52525B]">no rejected candidates labeled yet</div>
                        )}
                        {(d.gate_effectiveness || []).map(g => (
                            <div key={g.stage} className="flex items-center gap-3 py-1 font-mono text-[10px]">
                                <span className="text-white w-40 truncate">{g.stage}</span>
                                <span className="text-[#A1A1AA]">×{g.n} · {g.labeled} labeled</span>
                                {g.avoided_pips != null && (
                                    <span className={`ml-auto ${g.avoided_pips >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                                        title="pips the veto avoided (positive = gate saved money)">
                                        {g.avoided_pips >= 0 ? "saved " : "cost "}{Math.abs(g.avoided_pips)}p
                                    </span>
                                )}
                            </div>
                        ))}
                    </div>

                    <div className="mt-4" data-testid="scalp-broker-calibration">
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">
                            PER-BROKER CALIBRATION (learned from real fills)
                        </div>
                        {(d.brokers || []).length === 0 && (
                            <div className="font-mono text-[10px] text-[#52525B]">no broker stats yet</div>
                        )}
                        {(d.brokers || []).map(b => (
                            <div key={b.broker} className="py-1 font-mono text-[10px]">
                                <span className="text-white">{b.broker}</span>
                                <span className="text-[#A1A1AA] ml-2">
                                    subs {b.totals?.submissions ?? 0} · rejects {b.totals?.rejects ?? 0} · fills {b.totals?.fills ?? 0}
                                </span>
                                {Object.entries(b.sessions || {}).slice(0, 3).map(([sess, s]) => (
                                    <div key={sess} className="text-[#52525B] ml-3">
                                        {sess}: {s.avg_fill_delay_ms != null ? `fill ${Math.round(s.avg_fill_delay_ms)}ms` : "no fill data"}
                                        {s.avg_slippage_pips != null ? ` · slip ${s.avg_slippage_pips}p` : ""}
                                    </div>
                                ))}
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}
