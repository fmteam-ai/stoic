import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { Scale } from "lucide-react";

const fmtUsd = (v) => `$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

export function VerdictEffectivenessPanel() {
    const [data, setData] = useState(null);
    const [days, setDays] = useState(30);
    const load = useCallback(async () => {
        try {
            const r = await api.get(`/verdicts/effectiveness?days=${days}`);
            setData(r.data);
        } catch { /* non-fatal */ }
    }, [days]);
    useEffect(() => { load(); }, [load]);
    if (!data) return null;
    const t = data.totals || {};
    const net = t.net_benefit_usd || 0;
    const factors = Object.entries(data.by_limiting_factor || {})
        .map(([k, v]) => ({ key: k, ...v }))
        .sort((a, b) => b.net_benefit_usd - a.net_benefit_usd);
    const maxAbs = Math.max(1, ...factors.map(f => Math.abs(f.net_benefit_usd)));
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="verdict-effectiveness-panel">
            <div className="px-4 py-3 border-b border-[#141414] flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Scale className="w-4 h-4 text-[#FFB000]" />
                    <div className="font-display font-bold text-sm">Verdict Effectiveness</div>
                    <span className="font-mono text-[10px] text-[#52525B]">every risk reduction scored against its real outcome</span>
                </div>
                <div className="flex items-center gap-1">
                    {[7, 30, 90].map(d => (
                        <button key={d} onClick={() => setDays(d)} data-testid={`verdict-days-${d}`}
                            className={`font-mono text-[10px] px-2 py-0.5 border ${days === d ? "border-[#FFB000] text-[#FFB000]" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>
                            {d}D
                        </button>
                    ))}
                </div>
            </div>
            <div className="px-4 py-3 grid grid-cols-2 md:grid-cols-5 gap-3 border-b border-[#141414]">
                <div><div className="font-mono text-[9px] text-[#52525B]">VERDICTS</div>
                    <div className="font-mono text-sm text-white" data-testid="verdict-total">{t.verdicts ?? 0}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">RESOLVED / PENDING</div>
                    <div className="font-mono text-sm text-white" data-testid="verdict-resolved">{t.resolved ?? 0} / {t.pending ?? 0}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">MONEY SAVED</div>
                    <div className="font-mono text-sm text-[#00FF41]" data-testid="verdict-saved">{fmtUsd(t.saved_usd || 0)}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">OPPORTUNITY COST</div>
                    <div className="font-mono text-sm text-[#FF8C00]" data-testid="verdict-cost">{fmtUsd(t.opportunity_cost_usd || 0)}</div></div>
                <div><div className="font-mono text-[9px] text-[#52525B]">NET BENEFIT</div>
                    <div className="font-mono text-sm" data-testid="verdict-net"
                        style={{ color: net >= 0 ? "#00FF41" : "#FF3B30" }}>
                        {net >= 0 ? "+" : "−"}{fmtUsd(net)}
                    </div></div>
            </div>
            {!t.verdicts ? (
                <div className="px-4 py-6 text-center font-mono text-xs text-[#52525B]" data-testid="verdict-empty">
                    No risk reductions recorded yet — every REDUCE/REJECT verdict is now tracked and scored when its outcome is known.
                </div>
            ) : (
                <div className="px-4 py-3 space-y-1.5">
                    {factors.map(f => (
                        <div key={f.key} className="flex items-center gap-2" data-testid={`verdict-factor-${f.key}`}>
                            <div className="font-mono text-[9px] text-[#A1A1AA] w-40 shrink-0 truncate">{f.key.replace(/_/g, " ").toUpperCase()}</div>
                            <div className="flex-1 h-2 bg-[#141414]">
                                <div className="h-2" style={{
                                    width: `${Math.min(100, (Math.abs(f.net_benefit_usd) / maxAbs) * 100)}%`,
                                    backgroundColor: f.net_benefit_usd >= 0 ? "#00FF41" : "#FF3B30",
                                }} />
                            </div>
                            <div className="font-mono text-[10px] w-20 text-right"
                                style={{ color: f.net_benefit_usd >= 0 ? "#00FF41" : "#FF3B30" }}>
                                {f.net_benefit_usd >= 0 ? "+" : "−"}{fmtUsd(f.net_benefit_usd)}
                            </div>
                            <div className="font-mono text-[9px] text-[#52525B] w-24 text-right">{f.resolved}/{f.count} scored</div>
                        </div>
                    ))}
                </div>
            )}
            {data.lesson && (
                <div className="px-4 pb-3 font-mono text-[10px] text-[#FFB000]" data-testid="verdict-lesson">{data.lesson}</div>
            )}
        </div>
    );
}
