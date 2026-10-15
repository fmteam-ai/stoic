import { useEffect, useState } from "react";
import { FlaskConical } from "lucide-react";
import api from "@/lib/api";

const pct = (v) => (v == null ? "—" : `${v}%`);
const usd = (v) => (v == null ? "—" : `${v < 0 ? "−" : ""}$${Math.abs(v).toFixed(0)}`);

function Bands({ title, rows, testid, showPredicted }) {
    return (
        <div data-testid={testid}>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{title}</div>
            <table className="w-full font-mono text-[11px]">
                <thead>
                    <tr className="text-[#52525B]">
                        <th className="text-left font-normal py-0.5">band</th>
                        <th className="text-right font-normal">n</th>
                        {showPredicted && <th className="text-right font-normal">pred.</th>}
                        <th className="text-right font-normal">actual (95% CI)</th>
                        <th className="text-right font-normal">avg $</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.length === 0 && <tr><td colSpan={5} className="text-[#52525B] py-1">no decisions</td></tr>}
                    {rows.map((r) => (
                        <tr key={r.band} className="border-t border-[#141414]">
                            <td className="py-0.5 text-[#A1A1AA]">{r.band}</td>
                            <td className="text-right text-[#E4E4E7]" title={`${r.trades ?? r.n} trades across accounts`}>
                                {r.n}{r.trades > r.n && <span className="text-[#52525B]"> ×{r.trades}</span>}
                            </td>
                            {showPredicted && <td className="text-right text-[#71717A]">{pct(r.predicted_win_rate)}</td>}
                            <td className={`text-right ${r.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {pct(r.win_rate)}
                                {Array.isArray(r.win_rate_ci) && <span className="text-[#52525B]"> {r.win_rate_ci[0]}–{r.win_rate_ci[1]}%</span>}
                            </td>
                            <td className={`text-right ${r.avg_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{usd(r.avg_pnl)}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

// v1.60.38 — deferred signals (off-hours window / spread guard): would TP or SL have hit first?
function DeferredBands({ rows, testid }) {
    return (
        <div data-testid={testid}>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">DEFERRED · SHADOW OUTCOME (TP FIRST?)</div>
            <table className="w-full font-mono text-[11px]">
                <thead>
                    <tr className="text-[#52525B]">
                        <th className="text-left font-normal py-0.5">band</th>
                        <th className="text-right font-normal">deferred</th>
                        <th className="text-right font-normal">labelled</th>
                        <th className="text-right font-normal">TP-first (95% CI)</th>
                        <th className="text-right font-normal">avg R</th>
                    </tr>
                </thead>
                <tbody>
                    {rows.length === 0 && <tr><td colSpan={5} className="text-[#52525B] py-1">no deferred signals</td></tr>}
                    {rows.map((r) => (
                        <tr key={r.band} className="border-t border-[#141414]">
                            <td className="py-0.5 text-[#A1A1AA]">{r.band}</td>
                            <td className="text-right text-[#E4E4E7]">{r.n}</td>
                            <td className="text-right text-[#71717A]">{r.labelled}</td>
                            <td className={`text-right ${r.win_rate == null ? "text-[#52525B]" : r.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {pct(r.win_rate)}{Array.isArray(r.win_rate_ci) && <span className="text-[#52525B]"> {r.win_rate_ci[0]}–{r.win_rate_ci[1]}%</span>}
                            </td>
                            <td className={`text-right ${(r.avg_r ?? 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{r.avg_r == null ? "—" : r.avg_r}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

// v1.60.36 — per-engine evidence table (GET /postmortem/strategy-report), rendered verbatim.
export function StrategyReport() {
    const [d, setD] = useState(null);
    const [days, setDays] = useState(30);
    useEffect(() => {
        api.get(`/postmortem/strategy-report?days=${days}`).then((r) => setD(r.data)).catch(() => setD({ engines: [], error: true }));
    }, [days]);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="strategy-report">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2 flex-wrap">
                <FlaskConical className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Strategy Report</span>
                <span className="font-mono text-[9px] tracking-widest text-[#52525B]">
                    PREDICTED VS ACTUAL · ENTRY IN DAY RANGE · TREND-RIDE · ONE DECISION PER SYMBOL+SIDE+ENTRY (±2 MIN) · WILSON 95% CI
                </span>
                <div className="ml-auto flex gap-1">
                    {[7, 30, 90].map((n) => (
                        <button key={n} onClick={() => setDays(n)} data-testid={`strategy-report-days-${n}`}
                            className={`px-2 py-0.5 font-mono text-[10px] border ${days === n ? "border-[#00FF41]/50 text-[#00FF41]" : "border-[#27272A] text-[#71717A]"}`}>
                            {n}d
                        </button>
                    ))}
                </div>
            </div>
            <div className="p-4 space-y-5">
                {d && d.engines?.length === 0 && (
                    <div className="font-mono text-[11px] text-[#52525B]" data-testid="strategy-report-empty">
                        {d.error ? "report unavailable" : "No closed auto trades with exact P&L in this window."}
                    </div>
                )}
                {(d?.engines || []).map((e) => (
                    <div key={e.engine} data-testid={`strategy-report-engine-${e.engine}`}>
                        <div className="flex items-baseline gap-3 mb-2 flex-wrap">
                            <span className="font-display font-bold text-sm text-[#FAFAFA]">{e.label}</span>
                            <span className="font-mono text-[10px] text-[#71717A]">
                                {e.decisions} decisions · {e.trades} trades · {(e.deferred_shadow || []).reduce((a, r) => a + r.n, 0)} deferred · win {pct(e.win_rate)}
                                {Array.isArray(e.win_rate_ci) && <span className="text-[#52525B]"> ({e.win_rate_ci[0]}–{e.win_rate_ci[1]}%, n={e.decisions})</span>} · {usd(e.pnl)}
                            </span>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                            <Bands title="MONTE CARLO PREDICTED → ACTUAL" rows={e.mc_bands} showPredicted testid={`strategy-report-mc-${e.engine}`} />
                            <Bands title="ENTRY POSITION IN DAY RANGE" rows={e.range_position} testid={`strategy-report-range-${e.engine}`} />
                            <Bands title="TREND-RIDE" rows={e.trend_ride} testid={`strategy-report-ride-${e.engine}`} />
                            <Bands title="ENTRY SESSION (UTC) · EXECUTED" rows={e.sessions || []} testid={`strategy-report-session-${e.engine}`} />
                            <DeferredBands rows={e.deferred_shadow || []} testid={`strategy-report-deferred-${e.engine}`} />
                        </div>
                    </div>
                ))}
                {d && <div className="font-mono text-[9px] text-[#52525B]">{d.dedupe}</div>}
            </div>
        </div>
    );
}

export default StrategyReport;
