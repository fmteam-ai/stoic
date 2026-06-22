import { useEffect, useState, useCallback } from "react";
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ArrowUp, ArrowDown, RefreshCw as ArrowsClockwise, LineChart as ChartLineUp, Newspaper, ShieldAlert, CalendarClock, Bot, Pause, CheckCircle2, AlertCircle, Clock } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";
import { toast } from "sonner";

const PRIMARY_SYMBOLS = ["XAUUSD", "BTCUSD"];

// Recharts inline styles hoisted to module scope — stable references prevent
// child re-renders triggered by new object identities on every Dashboard render.
const CHART_TICK = { fill: "#52525B", fontSize: 10, fontFamily: "JetBrains Mono" };
const CHART_AXIS_LINE = { stroke: "#1F1F1F" };
const CHART_TOOLTIP_CONTENT = {
    background: "#0A0A0A", border: "1px solid #1F1F1F", borderRadius: 0,
    fontFamily: "JetBrains Mono", fontSize: 11,
};
const CHART_TOOLTIP_LABEL = { color: "#A1A1AA" };

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

function formatAgo(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    return `${Math.floor(seconds / 3600)}h ago`;
}

function BotStatusStrip({ status }) {
    const active = status.active;
    const isHold = status.last_signal?.action === "HOLD";
    const hasVeto = !!status.last_signal?.veto_reason;
    const lastAction = status.last_signal?.action ?? "—";
    const conf = status.last_signal?.confidence;
    const minConf = status.min_confidence;

    let stateIcon, stateColor, stateLabel;
    if (!active) { stateIcon = Pause; stateColor = "#52525B"; stateLabel = "STOPPED"; }
    else if (hasVeto) { stateIcon = AlertCircle; stateColor = "#FFB000"; stateLabel = "VETOED"; }
    else if (isHold) { stateIcon = Clock; stateColor = "#FFB000"; stateLabel = "HOLDING"; }
    else { stateIcon = CheckCircle2; stateColor = "#00FF41"; stateLabel = "TRADING"; }
    const StateIcon = stateIcon;

    const actionColor = lastAction === "BUY" ? "#00FF41" : lastAction === "SELL" ? "#FF3B30" : "#A1A1AA";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="bot-status-strip">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-3">
                    <Bot className="w-4 h-4" style={{ color: stateColor }} />
                    <span className="font-display font-bold text-sm tracking-tight">BOT STATUS</span>
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border"
                        style={{ color: stateColor, borderColor: `${stateColor}66`, backgroundColor: `${stateColor}11` }}
                        data-testid="bot-state-label">
                        <StateIcon className="w-2.5 h-2.5 inline mr-1" style={{ verticalAlign: "-1px" }} />
                        {stateLabel}
                    </span>
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    {status.risk_level?.toUpperCase()} · {status.symbols?.join(" · ")} · min {minConf}%
                </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 divide-x divide-[#1F1F1F]">
                <div className="p-4">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">LAST TICK</div>
                    <div className="font-mono text-sm" data-testid="bot-last-tick">{formatAgo(status.seconds_since_last_tick)}</div>
                </div>
                <div className="p-4">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">LAST ACTION</div>
                    <div className="font-mono text-sm flex items-center gap-1.5" style={{ color: actionColor }} data-testid="bot-last-action">
                        {lastAction}
                        {conf != null && <span className="text-[#52525B] text-xs">· {conf}%</span>}
                    </div>
                </div>
                <div className="p-4">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">NEXT TICK</div>
                    <div className="font-mono text-sm" data-testid="bot-next-tick">
                        {active ? (status.next_tick_in_seconds != null ? `in ~${status.next_tick_in_seconds}s` : "—") : "paused"}
                    </div>
                </div>
                <div className="p-4">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">AUTO-EXECUTE</div>
                    <div className={`font-mono text-sm ${status.auto_execute ? "text-[#00FF41]" : "text-[#A1A1AA]"}`} data-testid="bot-autoexec">
                        {status.auto_execute ? "ON" : "OFF (manual)"}
                    </div>
                </div>
            </div>

            {status.why_no_trade && (
                <div className="px-5 py-3 border-t border-[#1F1F1F] flex items-start gap-2" data-testid="bot-why-no-trade">
                    <AlertCircle className="w-3.5 h-3.5 text-[#FFB000] shrink-0 mt-0.5" />
                    <div className="text-xs text-[#A1A1AA] leading-relaxed">
                        <span className="font-mono text-[10px] text-[#FFB000] tracking-widest mr-1">WHY NO TRADE ·</span>
                        {status.why_no_trade}
                    </div>
                </div>
            )}

            {status.last_signal?.reasoning && (
                <div className="px-5 py-3 border-t border-[#1F1F1F]">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">AI REASONING · {status.last_signal.symbol}</div>
                    <div className="text-xs text-[#A1A1AA] leading-relaxed line-clamp-2">{status.last_signal.reasoning}</div>
                </div>
            )}
        </div>
    );
}

export default function Dashboard() {
    const [quotes, setQuotes] = useState({});
    const [selected, setSelected] = useState("XAUUSD");
    const [history, setHistory] = useState([]);
    const [indicators, setIndicators] = useState({});
    const [sentiment, setSentiment] = useState({});
    const [macro, setMacro] = useState({ events: [], freeze: null });
    const [stats, setStats] = useState(null);
    const [botStatus, setBotStatus] = useState(null);
    const [loading, setLoading] = useState(true);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [err, setErr] = useState("");
    const { lastEvent, connected: wsConnected } = useLiveStream();

    const loadQuotes = useCallback(async () => {
        try {
            const { data } = await api.get(`/market/quotes?symbols=${PRIMARY_SYMBOLS.join(",")}`);
            const map = {};
            for (const q of data.quotes) { if (q.symbol) map[q.symbol] = q; }
            setQuotes(map);
            setErr("");
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []); // deps: stable imports + setters only

    const loadHistory = useCallback(async (sym) => {
        setHistoryLoading(true);
        try {
            const { data } = await api.get(`/market/history/${sym}`);
            setHistory((data.history || []).map(h => ({ date: h.date, close: h.close })));
            setIndicators(data.indicators || {});
        } catch (e) { setErr(formatApiError(e)); }
        finally { setHistoryLoading(false); }
    }, []); // deps: stable imports + setters only

    const loadSentiment = useCallback(async (sym) => {
        try {
            const { data } = await api.get(`/sentiment/${sym}`);
            setSentiment(s => ({ ...s, [sym]: data }));
        } catch (e) {
            console.warn("[dashboard] sentiment load failed", sym, e?.message);
        }
    }, []);

    const loadMacro = useCallback(async (sym) => {
        try {
            const [upcoming, freeze] = await Promise.all([
                api.get(`/calendar/upcoming/${sym}?hours=48`),
                api.get(`/calendar/freeze/${sym}`),
            ]);
            setMacro({ events: upcoming.data.events || [], freeze: freeze.data });
        } catch (e) {
            console.warn("[dashboard] macro load failed", sym, e?.message);
        }
    }, []);

    const loadStats = useCallback(async () => {
        try {
            const { data } = await api.get("/trades/stats");
            setStats(data);
        } catch (e) {
            console.warn("[dashboard] stats load failed", e?.message);
        }
    }, []);

    const loadBotStatus = useCallback(async () => {
        try {
            const { data } = await api.get("/bot/status");
            setBotStatus(data);
        } catch (e) {
            console.warn("[dashboard] bot status load failed", e?.message);
        }
    }, []);

    useEffect(() => {
        loadQuotes(); loadStats(); loadBotStatus();
        const id = setInterval(() => { loadQuotes(); loadBotStatus(); }, 60_000);
        const tickId = setInterval(loadBotStatus, 10_000); // refresh status every 10s
        return () => { clearInterval(id); clearInterval(tickId); };
    }, [loadQuotes, loadStats, loadBotStatus]);

    useEffect(() => { loadHistory(selected); loadSentiment(selected); loadMacro(selected); }, [selected, loadHistory, loadSentiment, loadMacro]);

    // Live stream reactions
    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "circuit_breaker_tripped") {
            toast.error("Circuit breaker tripped", {
                description: lastEvent.payload?.reason || "Bot disabled — daily drawdown limit exceeded.",
            });
        } else if (lastEvent.type === "signal_created") {
            const p = lastEvent.payload || {};
            toast(`New ${p.action} signal · ${p.symbol}`, {
                description: `Confidence ${p.confidence}% · auto-generated`,
            });
        } else if (lastEvent.type === "trade_created") {
            const p = lastEvent.payload || {};
            toast.success(`Trade queued · ${p.symbol} ${p.action}`, { description: `Lots: ${p.lot_size}` });
        } else if (lastEvent.type === "trade_updated" && lastEvent.payload?.status === "closed") {
            const p = lastEvent.payload;
            const pnl = p.pnl ?? 0;
            toast(`Trade closed · P&L ${pnl >= 0 ? "+" : ""}${pnl}`, { description: `Ticket ${p.trade_id}` });
        }
    }, [lastEvent]);

    return (
        <AppLayout>
            <PageHeader
                title="Dashboard"
                subtitle="Live markets, AI insights and portfolio snapshot."
                testid="dashboard-header"
                action={
                    <div className="flex items-center gap-3">
                        <span className={`flex items-center gap-1.5 font-mono text-[10px] tracking-widest ${wsConnected ? "text-[#00FF41]" : "text-[#52525B]"}`} data-testid="ws-status">
                            <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? "bg-[#00FF41] pulse-dot" : "bg-[#52525B]"}`} />
                            {wsConnected ? "LIVE" : "OFFLINE"}
                        </span>
                        <button onClick={() => { loadQuotes(); loadHistory(selected); }}
                            data-testid="dashboard-refresh-button"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors duration-150">
                            <ArrowsClockwise className="w-3.5 h-3.5" /> REFRESH
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="dashboard-error">{err}</div>}

                {/* Portfolio performance — pinned at top */}
                {stats && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">PORTFOLIO PERFORMANCE</div>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="portfolio-stats">
                            <StatCell label="OPEN TRADES" value={stats.open_trades} accent="text-[#FFD700]" />
                            <StatCell label="TOTAL TRADES" value={stats.total_trades} />
                            <StatCell
                                label="WIN RATE"
                                value={`${stats.win_rate}%`}
                                accent={stats.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FFB000]"}
                            />
                            <StatCell
                                label="TOTAL P&L"
                                value={`${stats.total_pnl >= 0 ? "+" : ""}${stats.total_pnl}`}
                                accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}
                            />
                        </div>
                    </div>
                )}

                {/* Bot Status Strip */}
                {botStatus && <BotStatusStrip status={botStatus} />}

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
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">CHART · 12 MONTHS · DAILY</div>
                            <div className="flex items-center gap-2">
                                <ChartLineUp className="w-4 h-4 text-[#00FF41]" />
                                <span className="font-display font-bold text-lg tracking-tight" data-testid="chart-symbol">{selected}</span>
                            </div>
                        </div>
                        {indicators?.six_month_return_pct != null && (
                            <div className={`font-mono text-sm ${indicators.six_month_return_pct >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {indicators.six_month_return_pct >= 0 ? "+" : ""}{indicators.six_month_return_pct}% / 12M
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
                                    <XAxis dataKey="date" stroke="#52525B" tick={CHART_TICK} tickLine={false} axisLine={CHART_AXIS_LINE} minTickGap={32} />
                                    <YAxis stroke="#52525B" tick={CHART_TICK} tickLine={false} axisLine={CHART_AXIS_LINE} domain={["auto", "auto"]} width={70} />
                                    <Tooltip contentStyle={CHART_TOOLTIP_CONTENT} labelStyle={CHART_TOOLTIP_LABEL} />
                                    <Area type="monotone" dataKey="close" stroke="#00FF41" strokeWidth={1.5} fill="url(#priceFill)" />
                                </AreaChart>
                            </ResponsiveContainer>
                        )}
                    </div>
                </div>

                {/* Macro freeze banner */}
                {macro.freeze?.frozen && (
                    <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 p-4 flex items-start gap-3" data-testid="macro-freeze-banner">
                        <ShieldAlert className="w-5 h-5 text-[#FF3B30] shrink-0 mt-0.5" />
                        <div>
                            <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest mb-1">MACRO FREEZE · BOT WILL NOT TRADE</div>
                            <div className="text-sm">{macro.freeze.reason}</div>
                        </div>
                    </div>
                )}

                {/* Upcoming macro events */}
                {macro.events?.length > 0 && (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="macro-events-panel">
                        <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                            <CalendarClock className="w-4 h-4 text-[#FFB000]" />
                            <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                UPCOMING ECONOMIC EVENTS · {selected} · NEXT 48H · {macro.events.length} TOTAL
                            </span>
                        </div>
                        <div className="divide-y divide-[#1F1F1F]">
                            {macro.events.slice(0, 6).map((e, i) => {
                                const impact = e.impact?.toLowerCase();
                                const impactClass = impact === "high"
                                    ? "text-[#FF3B30] border-[#FF3B30]/40"
                                    : impact === "medium"
                                        ? "text-[#FFB000] border-[#FFB000]/40"
                                        : "text-[#A1A1AA] border-[#1F1F1F]";
                                const when = new Date(e.when);
                                const hoursAway = Math.max(0, Math.round((when.getTime() - Date.now()) / 3600000));
                                return (
                                    <div key={`${e.country}-${e.when}-${e.title}`} className="px-5 py-2.5 flex items-center gap-3 hover:bg-[#121212] transition-colors">
                                        <span className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${impactClass}`}>
                                            {impact?.toUpperCase()}
                                        </span>
                                        <span className="font-mono text-xs text-[#A1A1AA] w-12">{e.country}</span>
                                        <span className="font-mono text-xs flex-1 truncate">{e.title}</span>
                                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest whitespace-nowrap">
                                            {hoursAway < 1 ? "<1h" : `${hoursAway}h`}
                                        </span>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                )}

                {/* News Sentiment for selected symbol */}
                {sentiment[selected] && (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid="sentiment-card">
                        <div className="flex items-start gap-3 mb-3">
                            <Newspaper className="w-4 h-4 text-[#FFB000] mt-0.5" />
                            <div className="flex-1">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                                    NEWS SENTIMENT · {selected} · {sentiment[selected].article_count} HEADLINES (24H)
                                </div>
                                <div className="flex items-center gap-3">
                                    <span className="font-display font-bold text-xl tracking-tight">
                                        {sentiment[selected].label?.replace("_", " ").toUpperCase()}
                                    </span>
                                    <span className={`font-mono text-sm ${
                                        sentiment[selected].score > 0.2 ? "text-[#00FF41]" :
                                        sentiment[selected].score < -0.2 ? "text-[#FF3B30]" : "text-[#A1A1AA]"
                                    }`}>
                                        SCORE {sentiment[selected].score >= 0 ? "+" : ""}{sentiment[selected].score}
                                    </span>
                                </div>
                            </div>
                        </div>
                        {sentiment[selected].summary && (
                            <p className="text-sm text-[#A1A1AA] leading-relaxed mb-2">{sentiment[selected].summary}</p>
                        )}
                        {sentiment[selected].key_drivers?.length > 0 && (
                            <ul className="flex flex-wrap gap-1.5 mt-2">
                                {sentiment[selected].key_drivers.map((d, i) => (
                                    <li key={i} className="font-mono text-[10px] text-[#A1A1AA] bg-[#121212] border border-[#1F1F1F] px-2 py-0.5">{d}</li>
                                ))}
                            </ul>
                        )}
                    </div>
                )}

                {/* Indicators */}
                {indicators && Object.keys(indicators).length > 0 && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">TECHNICAL INDICATORS · 12M ANALYSIS</div>
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="indicators-grid">
                            <StatCell label="RSI (14)" value={indicators.rsi_14 ?? "—"} accent={indicators.rsi_14 > 70 ? "text-[#FF3B30]" : indicators.rsi_14 < 30 ? "text-[#00FF41]" : "text-white"} />
                            <StatCell label="SMA 20" value={indicators.sma_20 ?? "—"} />
                            <StatCell label="SMA 50" value={indicators.sma_50 ?? "—"} />
                            <StatCell label="SMA 200" value={indicators.sma_200 ?? "—"} />
                            <StatCell label="12M HIGH" value={indicators.high_180d ?? "—"} />
                            <StatCell label="12M LOW" value={indicators.low_180d ?? "—"} />
                            <StatCell label="% from High" value={`${indicators.pct_from_high ?? 0}%`} accent="text-[#FFB000]" />
                            <StatCell label="30D Volatility" value={`${indicators.volatility_30d_pct ?? 0}%`} />
                        </div>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
