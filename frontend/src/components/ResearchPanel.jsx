import { useEffect, useState } from "react";
import { ChartProvenance } from "@/components/ChartProvenance";
import api from "@/lib/api";
import { FlaskConical } from "lucide-react";
import {
    ResponsiveContainer, ComposedChart, Bar, Line, XAxis, YAxis,
    Tooltip, CartesianGrid, Cell,
} from "recharts";

const G = "#00FF41", R = "#FF3B30", A = "#FFB000", MUT = "#52525B";

function Head({ children }) {
    return <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">{children}</div>;
}

const DECAY_TONE = {
    STABLE_OR_IMPROVING: { c: G, label: "STABLE / IMPROVING" },
    SOFTENING: { c: A, label: "SOFTENING" },
    DECAYING: { c: R, label: "DECAYING" },
    INSUFFICIENT_DATA: { c: MUT, label: "INSUFFICIENT DATA" },
};

export function ResearchPanel() {
    const [d, setD] = useState(null);

    useEffect(() => {
        api.get("/analytics/research?days=90").then(r => setD(r.data)).catch(() => {});
    }, []);

    if (!d) return null;
    const cal = (d.calibration || []).filter(c => c.n > 0);
    const decay = DECAY_TONE[d.decay?.status] || DECAY_TONE.INSUFFICIENT_DATA;
    const ex = d.execution || {};

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="research-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <FlaskConical className="w-3.5 h-3.5 text-[#BF5AF2]" />
                <span className="font-display font-bold text-sm">Research</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    CALIBRATION · CI · WALK-FORWARD · REGIME · DECAY · EXECUTION — {d.trades} TRADES / {d.window_days}D
                </span>
            </div>

            {/* decay + execution attribution strip */}
            <div className="p-4 flex flex-wrap gap-2 border-b border-[#1F1F1F]">
                <span className="font-mono text-[10px] tracking-widest px-2 py-1 border"
                    style={{ color: decay.c, borderColor: `${decay.c}66` }}
                    data-testid="research-decay">
                    STRATEGY DECAY <span className="font-bold">{decay.label}</span>
                    {d.decay?.weekly_slope != null && ` · slope ${d.decay.weekly_slope}/wk`}
                    {d.decay?.recent_30d_avg_pnl != null && ` · 30d avg $${d.decay.recent_30d_avg_pnl}`}
                </span>
                <span className="font-mono text-[10px] tracking-widest px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]"
                    data-testid="research-execution">
                    EXECUTION <span className="text-white">gross ${ex.gross_pnl ?? "—"}</span>
                    <span className="text-[#FF3B30]"> · comm ${ex.commission ?? "—"}</span>
                    <span className={Number(ex.swap) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}> · swap ${ex.swap ?? "—"}</span>
                    <span className="text-white"> · net ${ex.net_pnl ?? "—"}</span>
                    {ex.cost_drag_pct != null && <span className="text-[#FFB000]"> · drag {ex.cost_drag_pct}%</span>}
                </span>
                {d.walk_forward_stability != null && (
                    <span className="font-mono text-[10px] tracking-widest px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]"
                        data-testid="research-stability">
                        WALK-FORWARD STABILITY <span className="text-white">{d.walk_forward_stability}/100</span>
                    </span>
                )}
            </div>

            <div className="p-4 grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* calibration */}
                <div data-testid="research-calibration">
                    <Head>CONFIDENCE CALIBRATION — predicted vs realised win rate (95% Wilson CI)</Head>
                    {cal.length === 0 && <div className="font-mono text-[10px] text-[#52525B]">no confidence-tagged trades yet</div>}
                    {cal.map(c => {
                        const off = c.gap != null && Math.abs(c.gap) > 15;
                        return (
                            <div key={c.bucket} className="flex items-center gap-3 py-1 font-mono text-[10px]">
                                <span className="text-white w-14">{c.bucket}</span>
                                <div className="flex-1 h-2 bg-[#111] relative">
                                    <div className="absolute h-2" style={{
                                        left: `${c.ci_low ?? 0}%`,
                                        width: `${Math.max(1, (c.ci_high ?? 0) - (c.ci_low ?? 0))}%`,
                                        background: "#1F1F1F",
                                    }} />
                                    <div className="absolute w-0.5 h-2" style={{ left: `${c.win_rate}%`, background: off ? A : G }} />
                                    <div className="absolute w-0.5 h-2 opacity-50" style={{ left: `${c.mid}%`, background: "#0099FF" }} />
                                </div>
                                <span className="text-[#A1A1AA] w-32 text-right">
                                    {c.win_rate}% <span className="text-[#52525B]">[{c.ci_low}–{c.ci_high}]</span> n={c.n}
                                </span>
                                <span className={`w-14 text-right ${off ? "text-[#FFB000]" : "text-[#52525B]"}`}>
                                    {c.gap > 0 ? "+" : ""}{c.gap}
                                </span>
                            </div>
                        );
                    })}
                    <div className="font-mono text-[9px] text-[#52525B] mt-1">
                        blue tick = predicted · green/amber tick = realised · grey band = 95% CI · right column = calibration gap
                    </div>

                    <div className="mt-4" data-testid="research-symbol-ci">
                        <Head>PER-SYMBOL WIN RATE (95% CI)</Head>
                        {(d.symbols || []).slice(0, 5).map(s => (
                            <div key={s.symbol} className="flex items-center gap-3 py-0.5 font-mono text-[10px]">
                                <span className="text-white w-16">{s.symbol}</span>
                                <span className="text-[#A1A1AA]">{s.win_rate}% <span className="text-[#52525B]">[{s.ci_low}–{s.ci_high}]</span> · n={s.n}</span>
                                <span className={s.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}>
                                    {s.total_pnl >= 0 ? "+" : ""}${s.total_pnl}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>

                {/* walk-forward + regimes */}
                <div>
                    <Head>WALK-FORWARD STABILITY — weekly out-of-sample</Head>
                    <ChartProvenance p={d.provenance} testid="research-walkforward-provenance" />
                    <div className="h-40" data-testid="research-walkforward">
                        <ResponsiveContainer width="100%" height="100%">
                            <ComposedChart data={d.walk_forward || []} margin={{ top: 5, right: 5, bottom: 0, left: -20 }}>
                                <CartesianGrid stroke="#1F1F1F" vertical={false} />
                                <XAxis dataKey="week" tick={{ fill: MUT, fontSize: 9, fontFamily: "monospace" }} />
                                <YAxis tick={{ fill: MUT, fontSize: 9, fontFamily: "monospace" }} />
                                <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #1F1F1F", fontSize: 10, fontFamily: "monospace" }}
                                    labelStyle={{ color: "#A1A1AA" }} />
                                <Bar dataKey="total_pnl" name="weekly P&L">
                                    {(d.walk_forward || []).map((w, i) => (
                                        <Cell key={i} fill={w.total_pnl >= 0 ? G : R} />
                                    ))}
                                </Bar>
                                <Line dataKey="win_rate" name="win rate %" stroke="#0099FF" dot={false} strokeWidth={1.5} />
                            </ComposedChart>
                        </ResponsiveContainer>
                    </div>

                    <div className="mt-4" data-testid="research-regimes">
                        <Head>REGIME ATTRIBUTION</Head>
                        {(d.regimes || []).slice(0, 6).map(r => (
                            <div key={r.regime} className="flex items-center gap-3 py-0.5 font-mono text-[10px]">
                                <span className="text-white w-36 truncate">{r.regime}</span>
                                <span className="text-[#A1A1AA]">n={r.n} · {r.win_rate}%</span>
                                <span className={`ml-auto ${r.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                    {r.total_pnl >= 0 ? "+" : ""}${r.total_pnl}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}
