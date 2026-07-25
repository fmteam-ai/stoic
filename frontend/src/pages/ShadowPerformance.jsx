import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ShadowHealthCard, ShadowBenchmarkTable, TwinStressPanel, ValidationQuorumCard } from "@/components/ShadowReadinessPanels";
import { Eye, TrendingUp, TrendingDown, RefreshCw, Trophy, AlertTriangle, Activity } from "lucide-react";

export default function ShadowPerformance() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        setErr(""); setRefreshing(true);
        try {
            const { data: d } = await api.get("/shadow/performance");
            setData(d);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); setRefreshing(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    const fmt = (v, suffix = "") => v == null ? "—" : `${v}${suffix}`;
    const fmtR = (v) => v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}R`;
    const fmtPct = (v) => v == null ? "—" : `${(v * 100).toFixed(1)}%`;

    return (
        <AppLayout>
            <PageHeader
                title="Shadow Performance"
                subtitle="Hypothetical track record from paper-shadow signals. Turn Paper Shadow Mode ON in Bot Config to populate this report."
                action={
                    <button onClick={load} disabled={refreshing} data-testid="shadow-refresh"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#06B6D4]/40 text-xs font-mono tracking-widest disabled:opacity-60">
                        <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin text-[#06B6D4]" : ""}`} />
                        {refreshing ? "REFRESHING…" : "REFRESH"}
                    </button>
                }
            />

            <div className="space-y-4 mb-4" data-testid="shadow-readiness">
                <ShadowHealthCard />
                <ValidationQuorumCard />
                <ShadowBenchmarkTable />
                <TwinStressPanel />
            </div>

            {err && (
                <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 p-3 mb-4 text-xs text-[#FF3B30] font-mono" data-testid="shadow-error">{err}</div>
            )}

            {loading ? (
                <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="shadow-loading">LOADING…</div>
            ) : !data || data.total_signals === 0 ? (
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-10 text-center" data-testid="shadow-empty">
                    <Eye className="w-12 h-12 text-[#06B6D4] mx-auto mb-3 opacity-50" />
                    <div className="font-display font-bold text-lg mb-1">No shadow signals yet</div>
                    <div className="text-sm text-[#A1A1AA] max-w-md mx-auto">
                        Enable <span className="text-[#06B6D4] font-mono">Paper Shadow Mode</span> in Bot Config to start logging hypothetical signals. After 1-2 weeks the bot will have enough data to show a real personal backtest here — without any capital at risk.
                    </div>
                </div>
            ) : (
                <>
                    {/* Summary KPIs */}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5" data-testid="shadow-kpis">
                        <KPI label="TOTAL SIGNALS" value={data.total_signals} icon={Activity} />
                        <KPI label="WIN RATE" value={fmtPct(data.win_rate)}
                            icon={Trophy}
                            color={data.win_rate == null ? "default" :
                                   data.win_rate >= 0.55 ? "green" :
                                   data.win_rate >= 0.45 ? "yellow" : "red"} />
                        <KPI label="EXPECTANCY" value={fmtR(data.expectancy_r)}
                            icon={(data.expectancy_r || 0) >= 0 ? TrendingUp : TrendingDown}
                            color={data.expectancy_r == null ? "default" :
                                   data.expectancy_r >= 0.3 ? "green" :
                                   data.expectancy_r >= 0 ? "yellow" : "red"} />
                        <KPI label="TOTAL R" value={fmtR(data.total_r)}
                            color={data.total_r >= 0 ? "green" : "red"} />
                    </div>

                    {/* Secondary KPIs */}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
                        <KPI label="RESOLVED" value={`${data.resolved} / ${data.total_signals}`} />
                        <KPI label="STILL OPEN" value={data.open} />
                        <KPI label="AVG WIN" value={fmtR(data.avg_win_r)} color="green" />
                        <KPI label="AVG LOSS" value={fmtR(data.avg_loss_r)} color="red" />
                    </div>

                    {/* Per-symbol breakdown */}
                    {(data.by_symbol || []).length > 0 && (
                        <div className="border border-[#1F1F1F] bg-[#0A0A0A] mb-5" data-testid="shadow-by-symbol">
                            <div className="px-4 py-2 border-b border-[#1F1F1F] font-mono text-[10px] tracking-widest text-[#52525B]">
                                BY SYMBOL
                            </div>
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="border-b border-[#1F1F1F] text-left">
                                        <th className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">SYMBOL</th>
                                        <th className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">SIGNALS</th>
                                        <th className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">W/L</th>
                                        <th className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">WIN RATE</th>
                                        <th className="px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">TOTAL R</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {data.by_symbol.map(s => (
                                        <tr key={s.symbol} className="border-b border-[#1F1F1F] last:border-b-0 hover:bg-[#050505]">
                                            <td className="px-4 py-2 font-mono">{s.symbol}</td>
                                            <td className="px-4 py-2 font-mono text-right">{s.signals}</td>
                                            <td className="px-4 py-2 font-mono text-right">
                                                <span className="text-[#00FF41]">{s.wins}</span> / <span className="text-[#FF3B30]">{s.losses}</span>
                                            </td>
                                            <td className="px-4 py-2 font-mono text-right">{fmtPct(s.win_rate)}</td>
                                            <td className={`px-4 py-2 font-mono text-right ${s.total_r >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                                {fmtR(s.total_r)}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}

                    {/* Signal list */}
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="shadow-rows">
                        <div className="px-4 py-2 border-b border-[#1F1F1F] font-mono text-[10px] tracking-widest text-[#52525B] flex justify-between">
                            <span>SIGNAL LOG</span>
                            <span className="text-[#A1A1AA]">Last {data.rows.length} signals · daily candle resolution</span>
                        </div>
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm">
                                <thead>
                                    <tr className="border-b border-[#1F1F1F] text-left">
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">DATE</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">SYMBOL</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest">ACTION</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">CONF</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">ENTRY</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">SL / TP</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">OUTCOME</th>
                                        <th className="px-3 py-2 font-mono text-[10px] text-[#52525B] tracking-widest text-right">R</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {data.rows.map(r => {
                                        const o = r.outcome;
                                        const hitColor = !o ? "text-[#52525B]" :
                                            o.hit === "tp" ? "text-[#00FF41]" :
                                            o.hit === "sl" ? "text-[#FF3B30]" :
                                            "text-[#FFB000]";
                                        return (
                                            <tr key={r.id} className="border-b border-[#1F1F1F] last:border-b-0 hover:bg-[#050505]" data-testid={`shadow-row-${r.id}`}>
                                                <td className="px-3 py-2 font-mono text-xs text-[#A1A1AA] whitespace-nowrap">{(r.created_at || "").slice(0,16)}</td>
                                                <td className="px-3 py-2 font-mono text-xs">{r.symbol}</td>
                                                <td className={`px-3 py-2 font-mono text-xs ${r.action === "BUY" ? "text-[#00FF41]" : r.action === "SELL" ? "text-[#FF3B30]" : "text-[#52525B]"}`}>
                                                    {r.action}
                                                </td>
                                                <td className="px-3 py-2 font-mono text-xs text-right">{r.confidence ?? "—"}</td>
                                                <td className="px-3 py-2 font-mono text-xs text-right">{r.entry_price ?? "—"}</td>
                                                <td className="px-3 py-2 font-mono text-[10px] text-right text-[#A1A1AA] whitespace-nowrap">
                                                    {r.stop_loss && r.take_profit ? `${r.stop_loss} / ${r.take_profit}` : "—"}
                                                </td>
                                                <td className={`px-3 py-2 font-mono text-xs text-right ${hitColor}`}>
                                                    {!o ? "PENDING" :
                                                     o.hit === "tp" ? `✓ TP · ${o.days_to_outcome}d` :
                                                     o.hit === "sl" ? `✗ SL · ${o.days_to_outcome}d` :
                                                     `○ OPEN · ${o.days_to_outcome}d`}
                                                </td>
                                                <td className={`px-3 py-2 font-mono text-xs text-right ${o && o.r_multiple > 0 ? "text-[#00FF41]" : o && o.r_multiple < 0 ? "text-[#FF3B30]" : "text-[#52525B]"}`}>
                                                    {o ? fmtR(o.r_multiple) : "—"}
                                                </td>
                                            </tr>
                                        );
                                    })}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div className="mt-4 flex items-start gap-2 text-xs text-[#52525B] font-mono">
                        <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                        <span>
                            R-multiples assume the user would have risked exactly the SL distance per trade. Daily-candle resolution means TP/SL can both be hit in the same day — we conservatively assume SL fills first.
                        </span>
                    </div>
                </>
            )}
        </AppLayout>
    );
}


function KPI({ label, value, icon: Icon, color = "default" }) {
    const colorClass = {
        green: "text-[#00FF41]",
        red: "text-[#FF3B30]",
        yellow: "text-[#FFB000]",
        default: "text-white",
    }[color] || "text-white";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3">
            <div className="flex items-center gap-1.5 mb-1">
                {Icon && <Icon className={`w-3 h-3 ${colorClass}`} />}
                <span className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</span>
            </div>
            <div className={`font-display font-bold text-xl ${colorClass}`}>{value}</div>
        </div>
    );
}
