import { useEffect, useState, useCallback } from "react";
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ArrowUp, ArrowDown, RefreshCw as ArrowsClockwise, LineChart as ChartLineUp } from "lucide-react";

const PRIMARY_SYMBOLS = ["XAUUSD", "BTCUSD"];

function PriceTile({ quote, selected, onClick }) {
    if (!quote) return null;
    const positive = (quote.change_pct || 0) >= 0;
    return (
        <button
            onClick={onClick}
            data-testid={`price-tile-${quote.symbol}`}
            className={`text-left p-5 border transition-colors duration-150 ${
                selected ? "border-[#00FF41] bg-[#0A0A0A]" : "border-[#1F1F1F] hover:border-[#333333] bg-[#0A0A0A]"
            }`}
        >
            <div className="flex items-center justify-between mb-3">
                <span className="font-mono text-xs text-[#52525B] tracking-widest">{quote.symbol}</span>
                <span className="flex items-center gap-1.5 font-mono text-[10px] text-[#00FF41] tracking-widest">
                    <span className="w-1.5 h-1.5 bg-[#00FF41] rounded-full pulse-dot" /> LIVE
                </span>
            </div>
            <div className="font-mono font-medium text-3xl tracking-tight" data-testid={`price-value-${quote.symbol}`}>
                {quote.price?.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 5 })}
            </div>
            <div className={`mt-2 flex items-center gap-1 text-xs font-mono ${positive ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                {positive ? <ArrowUp className="w-3 h-3" /> : <ArrowDown className="w-3 h-3" />}
                <span>{positive ? "+" : ""}{(quote.change_pct || 0).toFixed(2)}%</span>
                <span className="text-[#52525B] ml-1">24H</span>
            </div>
        </button>
    );
}

function StatCell({ label, value, accent }) {
    return (
        <div className="p-4 border border-[#1F1F1F] bg-[#0A0A0A]">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className={`font-mono font-medium text-base ${accent || "text-white"}`}>{value}</div>
        </div>
    );
}

export default function Dashboard() {
    const [quotes, setQuotes] = useState({});
    const [selected, setSelected] = useState("XAUUSD");
    const [history, setHistory] = useState([]);
    const [indicators, setIndicators] = useState({});
    const [stats, setStats] = useState(null);
    const [loading, setLoading] = useState(true);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [err, setErr] = useState("");

    const loadQuotes = useCallback(async () => {
        try {
            const { data } = await api.get(`/market/quotes?symbols=${PRIMARY_SYMBOLS.join(",")}`);
            const map = {};
            for (const q of data.quotes) { if (q.symbol) map[q.symbol] = q; }
            setQuotes(map);
            setErr("");
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    const loadHistory = useCallback(async (sym) => {
        setHistoryLoading(true);
        try {
            const { data } = await api.get(`/market/history/${sym}`);
            setHistory((data.history || []).map(h => ({ date: h.date, close: h.close })));
            setIndicators(data.indicators || {});
        } catch (e) { setErr(formatApiError(e)); }
        finally { setHistoryLoading(false); }
    }, []);

    const loadStats = useCallback(async () => {
        try { const { data } = await api.get("/trades/stats"); setStats(data); } catch {}
    }, []);

    useEffect(() => {
        loadQuotes(); loadStats();
        const id = setInterval(loadQuotes, 60_000);
        return () => clearInterval(id);
    }, [loadQuotes, loadStats]);

    useEffect(() => { loadHistory(selected); }, [selected, loadHistory]);

    return (
        <AppLayout>
            <PageHeader
                title="Dashboard"
                subtitle="Live markets, AI insights and portfolio snapshot."
                testid="dashboard-header"
                action={
                    <button onClick={() => { loadQuotes(); loadHistory(selected); }}
                        data-testid="dashboard-refresh-button"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors duration-150">
                        <ArrowsClockwise className="w-3.5 h-3.5" /> REFRESH
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="dashboard-error">{err}</div>}

                {/* Quote tiles */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {loading ? (
                        <>
                            <div className="p-5 border border-[#1F1F1F] bg-[#0A0A0A] h-32 animate-pulse" />
                            <div className="p-5 border border-[#1F1F1F] bg-[#0A0A0A] h-32 animate-pulse" />
                        </>
                    ) : (
                        PRIMARY_SYMBOLS.map(s => (
                            <PriceTile key={s} quote={quotes[s]} selected={selected === s} onClick={() => setSelected(s)} />
                        ))
                    )}
                </div>

                {/* Chart */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center justify-between">
                        <div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">CHART · 6 MONTHS · DAILY</div>
                            <div className="flex items-center gap-2">
                                <ChartLineUp className="w-4 h-4 text-[#00FF41]" />
                                <span className="font-display font-bold text-lg tracking-tight" data-testid="chart-symbol">{selected}</span>
                            </div>
                        </div>
                        {indicators?.six_month_return_pct != null && (
                            <div className={`font-mono text-sm ${indicators.six_month_return_pct >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {indicators.six_month_return_pct >= 0 ? "+" : ""}{indicators.six_month_return_pct}% / 6M
                            </div>
                        )}
                    </div>
                    <div className="h-64 md:h-80 p-2" data-testid="price-chart">
                        {historyLoading ? (
                            <div className="h-full flex items-center justify-center font-mono text-xs text-[#52525B] tracking-widest">LOADING DATA...</div>
                        ) : history.length === 0 ? (
                            <div className="h-full flex items-center justify-center font-mono text-xs text-[#52525B] tracking-widest">NO DATA AVAILABLE</div>
                        ) : (
                            <ResponsiveContainer width="100%" height="100%">
                                <AreaChart data={history}>
                                    <defs>
                                        <linearGradient id="priceFill" x1="0" y1="0" x2="0" y2="1">
                                            <stop offset="0%" stopColor="#00FF41" stopOpacity={0.3} />
                                            <stop offset="100%" stopColor="#00FF41" stopOpacity={0} />
                                        </linearGradient>
                                    </defs>
                                    <CartesianGrid stroke="#1F1F1F" strokeDasharray="0" vertical={false} />
                                    <XAxis dataKey="date" stroke="#52525B" tick={{ fill: "#52525B", fontSize: 10, fontFamily: "JetBrains Mono" }} tickLine={false} axisLine={{ stroke: "#1F1F1F" }} minTickGap={32} />
                                    <YAxis stroke="#52525B" tick={{ fill: "#52525B", fontSize: 10, fontFamily: "JetBrains Mono" }} tickLine={false} axisLine={{ stroke: "#1F1F1F" }} domain={["auto", "auto"]} width={70} />
                                    <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #1F1F1F", borderRadius: 0, fontFamily: "JetBrains Mono", fontSize: 11 }} labelStyle={{ color: "#A1A1AA" }} />
                                    <Area type="monotone" dataKey="close" stroke="#00FF41" strokeWidth={1.5} fill="url(#priceFill)" />
                                </AreaChart>
                            </ResponsiveContainer>
                        )}
                    </div>
                </div>

                {/* Indicators */}
                {indicators && Object.keys(indicators).length > 0 && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">TECHNICAL INDICATORS · 6M ANALYSIS</div>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="indicators-grid">
                            <StatCell label="RSI (14)" value={indicators.rsi_14 ?? "—"} accent={indicators.rsi_14 > 70 ? "text-[#FF3B30]" : indicators.rsi_14 < 30 ? "text-[#00FF41]" : "text-white"} />
                            <StatCell label="SMA 20" value={indicators.sma_20 ?? "—"} />
                            <StatCell label="SMA 50" value={indicators.sma_50 ?? "—"} />
                            <StatCell label="SMA 200" value={indicators.sma_200 ?? "—"} />
                            <StatCell label="6M HIGH" value={indicators.high_180d ?? "—"} />
                            <StatCell label="6M LOW" value={indicators.low_180d ?? "—"} />
                            <StatCell label="% from High" value={`${indicators.pct_from_high ?? 0}%`} accent="text-[#FFB000]" />
                            <StatCell label="30D Volatility" value={`${indicators.volatility_30d_pct ?? 0}%`} />
                        </div>
                    </div>
                )}

                {/* Trading stats */}
                {stats && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">PORTFOLIO PERFORMANCE</div>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="portfolio-stats">
                            <StatCell label="OPEN TRADES" value={stats.open_trades} />
                            <StatCell label="TOTAL TRADES" value={stats.total_trades} />
                            <StatCell label="WIN RATE" value={`${stats.win_rate}%`} accent="text-[#00FF41]" />
                            <StatCell label="TOTAL P&L" value={`${stats.total_pnl >= 0 ? "+" : ""}${stats.total_pnl}`} accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
                        </div>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
