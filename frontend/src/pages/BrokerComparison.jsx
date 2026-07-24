import { useEffect, useState, Fragment } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { BrokerMatrix } from "@/components/BrokerMatrix";
import { BrokerIntelCard } from "@/components/BrokerIntelCard";
import { Scale, Zap, DollarSign, Layers, Info } from "lucide-react";

const fmtUsd = (v, dp = 0) => {
    if (v == null) return "—";
    const sign = v < 0 ? "−" : v > 0 ? "+" : "";
    return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: dp })}`;
};
const pnlColor = (v) => (v > 0 ? "#00FF41" : v < 0 ? "#FF3B30" : "#A1A1AA");

// Metric definitions: [section, label, getter, formatter, better ("low"|"high"|null), tooltip]
const METRICS = [
    ["EXECUTION QUALITY", "AVG SLIPPAGE", b => b.execution.avg_slippage_pips, v => v == null ? "—" : `${v} pips`, "low", "Average absolute slippage between requested and filled price (bot trades, EA v1.40+)"],
    ["EXECUTION QUALITY", "WORST SLIPPAGE", b => b.execution.worst_slippage_pips, v => v == null ? "—" : `${v} pips`, "low", "Largest single-order slippage in the window"],
    ["EXECUTION QUALITY", "MEDIAN DISPATCH TIME", b => b.execution.median_fill_latency_s, v => v == null ? "—" : `${v}s`, "low", "Median seconds from signal creation to the EA picking the order up for execution"],
    ["EXECUTION QUALITY", "FAILED ORDERS", b => b.execution.fail_rate_pct, (v, b) => v == null ? "—" : `${v}% (${b.execution.failed_orders})`, "low", "Orders rejected or failed at the broker vs total attempts"],
    ["PROFITABILITY", "NET P&L", b => b.pnl.net_pnl, v => fmtUsd(v, 0), "high", "Realized bot P&L on this broker in the window"],
    ["PROFITABILITY", "WIN RATE", b => b.pnl.win_rate, (v, b) => v == null ? "—" : `${v}% (${b.pnl.wins}W/${b.pnl.losses}L)`, "high", "Closed winning trades / all closed trades"],
    ["PROFITABILITY", "PROFIT FACTOR", b => b.pnl.profit_factor, v => v == null ? "—" : v >= 99 ? "∞" : v.toFixed(2), "high", "Gross wins ÷ gross losses. Above 1.0 = profitable"],
    ["PROFITABILITY", "AVG WIN / LOSS", b => b.pnl.avg_win, (v, b) => (v == null && b.pnl.avg_loss == null) ? "—" : `${v == null ? "—" : `$${v.toFixed(0)}`} / ${b.pnl.avg_loss == null ? "—" : `$${Math.abs(b.pnl.avg_loss).toFixed(0)}`}`, null, "Average winning trade vs average losing trade"],
    ["ACCOUNTS", "ACCOUNTS", b => b.accounts.count, (v, b) => `${v} (${b.accounts.connected} online)`, null, "Accounts you hold with this broker and how many are connected"],
    ["ACCOUNTS", "COMBINED EQUITY", b => b.accounts.equity, v => `$${(v ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`, null, "Total equity across this broker's accounts"],
];

function bestValue(brokers, getter, better) {
    if (!better) return null;
    const vals = brokers.map(getter).filter(v => v != null);
    if (vals.length < 2) return null;
    return better === "low" ? Math.min(...vals) : Math.max(...vals);
}

export default function BrokerComparison() {
    const [data, setData] = useState(null);
    const [days, setDays] = useState(30);
    const [err, setErr] = useState("");
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let dead = false;
        setLoading(true);
        api.get(`/accounts/broker-comparison?days=${days}`)
            .then(r => { if (!dead) { setData(r.data); setErr(""); } })
            .catch(e => { if (!dead) setErr(formatApiError(e)); })
            .finally(() => { if (!dead) setLoading(false); });
        return () => { dead = true; };
    }, [days]);

    const brokers = data?.brokers || [];

    return (
        <AppLayout>
            <PageHeader
                title="Broker Comparison"
                subtitle="Execution quality, slippage and bot P&L — side by side across your brokers."
                testid="broker-comparison-header"
                action={
                    <div className="flex gap-1" data-testid="broker-period-pills">
                        {[7, 30, 90].map(d => (
                            <button key={d} onClick={() => setDays(d)}
                                data-testid={`broker-period-${d}`}
                                className={`px-3 py-1.5 font-mono text-[10px] tracking-widest border transition-colors ${
                                    days === d ? "border-[#00FF41] text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                                }`}>
                                {d}D
                            </button>
                        ))}
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-4" data-testid="broker-comparison-page">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}
                <BrokerIntelCard />

                <div className="flex items-start gap-2 border border-[#1F1F1F] bg-[#0A0A0A] px-4 py-3">
                    <Info className="w-3.5 h-3.5 text-[#00BFFF] shrink-0 mt-0.5" />
                    <p className="text-[11px] text-[#A1A1AA] font-mono leading-relaxed">
                        BOT TRADES ONLY — all brokers receive the same signals, so differences below reflect the broker,
                        not the strategy. <span className="text-[#00FF41]">● BEST</span> marks the leading broker per metric.
                    </p>
                </div>

                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING COMPARISON…</div>
                ) : brokers.length === 0 ? (
                    <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="broker-comparison-empty">
                        <Scale className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                        <div className="font-display font-bold text-lg mb-1">No broker data yet</div>
                        <div className="text-sm text-[#A1A1AA]">Connect MT5 accounts and let the bot trade — comparison builds automatically.</div>
                    </div>
                ) : (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] overflow-x-auto" data-testid="broker-comparison-table">
                        <table className="w-full min-w-[640px]">
                            <thead>
                                <tr className="border-b border-[#1F1F1F]">
                                    <th className="text-left px-4 py-3 font-mono text-[9px] text-[#52525B] tracking-widest w-44">METRIC</th>
                                    {brokers.map(b => (
                                        <th key={b.broker} className="text-left px-4 py-3" data-testid={`broker-col-${b.broker.replace(/\s+/g, "-")}`}>
                                            <div className="font-display font-bold text-sm tracking-tight">{b.broker}</div>
                                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-0.5">
                                                {b.pnl.trades} BOT TRADE{b.pnl.trades === 1 ? "" : "S"} · {days}D
                                            </div>
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {METRICS.map(([section, label, getter, fmt, better, tip], i) => {
                                    const prevSection = i > 0 ? METRICS[i - 1][0] : null;
                                    const best = bestValue(brokers, getter, better);
                                    const SectionIcon = section === "EXECUTION QUALITY" ? Zap : section === "PROFITABILITY" ? DollarSign : Layers;
                                    return (
                                        <Fragment key={label}>
                                            {section !== prevSection && (
                                                <tr className="border-b border-[#1F1F1F] bg-[#050505]">
                                                    <td colSpan={brokers.length + 1} className="px-4 py-1.5">
                                                        <span className="flex items-center gap-1.5 font-mono text-[9px] tracking-widest text-[#00BFFF]">
                                                            <SectionIcon className="w-3 h-3" /> {section}
                                                        </span>
                                                    </td>
                                                </tr>
                                            )}
                                            <tr className="border-b border-[#1F1F1F] last:border-b-0 hover:bg-[#0D0D0D] transition-colors">
                                                <td className="px-4 py-2.5 font-mono text-[10px] text-[#A1A1AA] tracking-widest" title={tip}>{label}</td>
                                                {brokers.map(b => {
                                                    const v = getter(b);
                                                    const isBest = best != null && v === best;
                                                    const color = label === "NET P&L" ? pnlColor(v)
                                                        : label === "PROFIT FACTOR" && v != null ? (v >= 1 ? "#00FF41" : "#FF3B30")
                                                        : undefined;
                                                    return (
                                                        <td key={b.broker} className="px-4 py-2.5"
                                                            data-testid={`cell-${label.replace(/[^A-Z0-9]+/gi, "-")}-${b.broker.replace(/\s+/g, "-")}`}>
                                                            <span className="font-display font-bold text-sm tabular-nums" style={{ color }}>
                                                                {fmt(v, b)}
                                                            </span>
                                                            {isBest && (
                                                                <span className="ml-2 font-mono text-[8px] tracking-widest px-1 py-px border border-[#00FF41]/40 text-[#00FF41]">
                                                                    ● BEST
                                                                </span>
                                                            )}
                                                        </td>
                                                    );
                                                })}
                                            </tr>
                                        </Fragment>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                )}

                {brokers.length > 0 && (
                    <p className="font-mono text-[10px] text-[#52525B] tracking-widest leading-relaxed">
                        SLIPPAGE REQUIRES EA v1.40+ (REQUESTED PRICE REPORTING) · DISPATCH TIME = SIGNAL → EA ORDER PICKUP ·
                        SAMPLES: {brokers.map(b => `${b.broker.toUpperCase()} ${b.execution.slippage_samples}`).join(" · ")}
                    </p>
                )}

                <BrokerMatrix />
            </div>
        </AppLayout>
    );
}
