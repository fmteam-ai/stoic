import { useEffect, useState, useCallback } from "react";
import { ChartProvenance } from "@/components/ChartProvenance";
import api from "@/lib/api";
import { Layers, TrendingUp } from "lucide-react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

const ACCOUNT_COLORS = ["#00BFFF", "#FFD700", "#FF6B9D", "#B07CFF", "#FF8C42", "#4ADE80", "#F87171", "#22D3EE"];

const fmtUsd = (v, dp = 0) => {
    if (v == null) return "—";
    const sign = v < 0 ? "−" : v > 0 ? "+" : "";
    return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: dp })}`;
};
const pnlColor = (v) => (v > 0 ? "#00FF41" : v < 0 ? "#FF3B30" : "#A1A1AA");

function Stat({ label, value, color, sub, testid }) {
    return (
        <div className="px-4 py-3 border-r border-[#1F1F1F] last:border-r-0 min-w-[120px]" data-testid={testid}>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{label}</div>
            <div className="font-display font-bold text-lg tabular-nums leading-tight mt-0.5" style={{ color: color || "#E4E4E7" }}>
                {value}
            </div>
            {sub && <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-0.5">{sub}</div>}
        </div>
    );
}

/** iter-137 · Multi-account portfolio overview — aggregate stats bar +
 *  combined realized-P&L curve across every connected account. */
export default function MultiAccountOverview({ refreshKey = 0 }) {
    const [overview, setOverview] = useState(null);
    const [curve, setCurve] = useState(null);
    const [days, setDays] = useState(30);
    const [showPerAccount, setShowPerAccount] = useState(false);

    const loadOverview = useCallback(async () => {
        try { const { data } = await api.get("/accounts/overview"); setOverview(data); } catch { /* silent */ }
    }, []);

    useEffect(() => { loadOverview(); }, [loadOverview, refreshKey]);
    useEffect(() => {
        let dead = false;
        api.get(`/accounts/equity-curve?days=${days}`)
            .then(r => { if (!dead) setCurve(r.data); })
            .catch(() => {});
        return () => { dead = true; };
    }, [days, refreshKey]);

    if (!overview || (overview.totals?.accounts ?? 0) < 2) return null;
    const t = overview.totals;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="multi-account-overview">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <Layers className="w-4 h-4 text-[#00FF41]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">PORTFOLIO · ALL ACCOUNTS</div>
                        <div className="font-display font-bold text-base tracking-tight">
                            {t.accounts} accounts · {t.ea_fresh ?? t.connected} EA fresh{t.ea_paper ? ` · ${t.ea_paper} paper` : ""} · {t.trading_enabled} trading
                        </div>
                    </div>
                </div>
                <div className="flex gap-1" data-testid="equity-curve-period-pills">
                    {[7, 30, 90].map(d => (
                        <button key={d} onClick={() => setDays(d)}
                            data-testid={`equity-period-${d}`}
                            className={`px-2.5 py-1 font-mono text-[10px] tracking-widest border transition-colors ${
                                days === d ? "border-[#00FF41] text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                            }`}>
                            {d}D
                        </button>
                    ))}
                </div>
            </div>

            <div className="flex flex-wrap border-b border-[#1F1F1F]" data-testid="aggregate-stats-bar">
                <Stat label="TOTAL BALANCE" value={`$${t.balance.toLocaleString(undefined, { maximumFractionDigits: 0 })}`} testid="agg-total-balance" />
                <Stat label="TOTAL EQUITY" value={`$${t.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}`} testid="agg-total-equity" />
                <Stat label="FLOATING P&L" value={fmtUsd(t.floating)} color={pnlColor(t.floating)}
                    sub={`${t.open_positions} OPEN`} testid="agg-floating-pnl" />
                <Stat label="TODAY" value={fmtUsd(t.pnl_today)} color={pnlColor(t.pnl_today)} testid="agg-pnl-today" />
                <Stat label="7 DAYS" value={fmtUsd(t.pnl_7d)} color={pnlColor(t.pnl_7d)} testid="agg-pnl-7d" />
                <Stat label="30 DAYS" value={fmtUsd(t.pnl_30d)} color={pnlColor(t.pnl_30d)} testid="agg-pnl-30d" />
            </div>

            {curve && curve.series?.length > 1 && (
                <div className="p-4" data-testid="combined-equity-curve">
                    <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
                        <div className="flex items-center gap-2 font-mono text-[10px] text-[#52525B] tracking-widest">
                            <TrendingUp className="w-3 h-3" />
                            REALIZED P&L · CUMULATIVE · {days}D
                        </div>
                        <button onClick={() => setShowPerAccount(v => !v)}
                            data-testid="toggle-per-account-lines"
                            className={`px-2 py-0.5 font-mono text-[9px] tracking-widest border transition-colors ${
                                showPerAccount ? "border-[#00BFFF]/50 text-[#00BFFF]" : "border-[#1F1F1F] text-[#52525B] hover:border-[#333333]"
                            }`}>
                            {showPerAccount ? "◉ PER-ACCOUNT LINES" : "○ PER-ACCOUNT LINES"}
                        </button>
                    </div>
                    <ChartProvenance p={curve.provenance} testid="combined-equity-provenance" />
                    <div className="h-48">
                        <ResponsiveContainer width="100%" height="100%" minHeight={180} minWidth={200}>
                            <LineChart data={curve.series} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                                <XAxis dataKey="date" tick={{ fontSize: 9, fill: "#52525B", fontFamily: "monospace" }}
                                    tickFormatter={(d) => d.slice(5)} minTickGap={30} axisLine={{ stroke: "#1F1F1F" }} tickLine={false} />
                                <YAxis tick={{ fontSize: 9, fill: "#52525B", fontFamily: "monospace" }}
                                    tickFormatter={(v) => `$${v}`} width={55} axisLine={false} tickLine={false} />
                                <Tooltip
                                    contentStyle={{ background: "#0A0A0A", border: "1px solid #1F1F1F", fontFamily: "monospace", fontSize: 11 }}
                                    labelStyle={{ color: "#A1A1AA" }}
                                    formatter={(v, name) => [fmtUsd(v, 2), name]} />
                                <ReferenceLine y={0} stroke="#1F1F1F" />
                                <Line type="monotone" dataKey="total" name="COMBINED" stroke="#00FF41"
                                    strokeWidth={2} dot={false} isAnimationActive={false} />
                                {showPerAccount && curve.accounts.map((a, i) => (
                                    <Line key={a.id} type="monotone" dataKey={`a_${a.id}`} name={a.label || `#${i + 1}`}
                                        stroke={ACCOUNT_COLORS[i % ACCOUNT_COLORS.length]} strokeWidth={1}
                                        strokeDasharray="4 3" dot={false} isAnimationActive={false} />
                                ))}
                            </LineChart>
                        </ResponsiveContainer>
                    </div>
                    {showPerAccount && (
                        <div className="flex flex-wrap gap-3 mt-2">
                            {curve.accounts.map((a, i) => (
                                <span key={a.id} className="flex items-center gap-1.5 font-mono text-[9px] tracking-widest text-[#A1A1AA]">
                                    <span className="w-2 h-0.5 inline-block" style={{ background: ACCOUNT_COLORS[i % ACCOUNT_COLORS.length] }} />
                                    {(a.label || "").toUpperCase()}
                                </span>
                            ))}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
