import { useEffect, useState, useCallback } from "react";
import { ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ArrowUp, ArrowDown, RefreshCw as ArrowsClockwise, LineChart as ChartLineUp, Newspaper, ShieldAlert, CalendarClock, Bot, Pause, CheckCircle2, AlertCircle, Clock, Target, TrendingUp, TrendingDown } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";
import { IntegrityWidget } from "@/components/IntegrityWidget";
import { PerAccountComparison } from "@/components/PerAccountComparison";
import { EaVersionStrip } from "@/components/EaVersionStrip";
import { BotHealthScore } from "@/components/BotHealthScore";
import DataFreshnessStrip from "@/components/DataFreshnessStrip";
import { MacroClimate } from "@/components/MacroClimate";
import { DiagnosticModal } from "@/components/DiagnosticModal";
import BotPulsePanel from "@/components/BotPulsePanel";
import BotWatching from "@/components/BotWatching";
import CooldownPanel from "@/components/CooldownPanel";
import RiskGaugePanel from "@/components/RiskGaugePanel";
import WeeklyDigestPanel from "@/components/WeeklyDigestPanel";
import { useAuth } from "@/context/AuthContext";
import { Stethoscope } from "lucide-react";
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

// User-selectable chart timeframes — daily-bar data, sliced client-side.
const CHART_RANGES = {
    "14D": { label: "14 days",  days: 14,  shortLabel: "14D" },
    "1M":  { label: "1 month",  days: 30,  shortLabel: "1M"  },
    "6M":  { label: "6 months", days: 180, shortLabel: "6M"  },
    "1Y":  { label: "1 year",   days: 365, shortLabel: "1Y"  },
};

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

            {status.intelligence && status.intelligence.total > 0 && (
                <div className="px-5 py-3 border-t border-[#1F1F1F]" data-testid="bot-intelligence-counters">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">VETOES TODAY (24H)</div>
                    <div className="flex items-center gap-3 flex-wrap text-xs font-mono">
                        {status.intelligence.mtf_veto > 0 && (
                            <span className="px-2 py-1 border border-[#0099FF]/40 bg-[#0099FF]/10 text-[#0099FF]" data-testid="intel-mtf">
                                MTF · {status.intelligence.mtf_veto}
                            </span>
                        )}
                        {status.intelligence.auto_tune_block > 0 && (
                            <span className="px-2 py-1 border border-[#FFB000]/40 bg-[#FFB000]/10 text-[#FFB000]" data-testid="intel-autotune">
                                AUTO-TUNE · {status.intelligence.auto_tune_block}
                            </span>
                        )}
                        {status.intelligence.spread_block > 0 && (
                            <span className="px-2 py-1 border border-[#A1A1AA]/40 bg-[#A1A1AA]/10 text-[#A1A1AA]" data-testid="intel-spread">
                                SPREAD · {status.intelligence.spread_block}
                            </span>
                        )}
                        {status.intelligence.slippage_veto > 0 && (
                            <span className="px-2 py-1 border border-[#FF3B30]/40 bg-[#FF3B30]/10 text-[#FF3B30]" data-testid="intel-slippage">
                                SLIPPAGE · {status.intelligence.slippage_veto}
                            </span>
                        )}
                        {status.intelligence.learned_meta_veto > 0 && (
                            <span className="px-2 py-1 border border-[#9B59B6]/40 bg-[#9B59B6]/10 text-[#9B59B6]" data-testid="intel-learned">
                                LEARNED · {status.intelligence.learned_meta_veto}
                            </span>
                        )}
                        {status.intelligence.aplus_veto > 0 && (
                            <span className="px-2 py-1 border border-[#FFD700]/40 bg-[#FFD700]/10 text-[#FFD700]" data-testid="intel-aplus">
                                A+ · {status.intelligence.aplus_veto}
                            </span>
                        )}
                        {status.intelligence.rr_veto > 0 && (
                            <span className="px-2 py-1 border border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]" data-testid="intel-rr">
                                R:R · {status.intelligence.rr_veto}
                            </span>
                        )}
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

function TimeToTargetPanel({ trades }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="time-to-target-panel">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <Target className="w-4 h-4 text-[#FFD700]" />
                    <span className="font-display font-bold text-sm tracking-tight">TIME TO TARGET</span>
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{trades.length} OPEN</span>
                </div>
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">REFRESH 5s · LIVE P&L</span>
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                {trades.map(t => <TradeProgressRow key={t.id} t={t} />)}
            </div>
        </div>
    );
}

function ProgressBar({ label, pct, hit, bigPips, smallPips }) {
    return (
        <div className="flex-1 min-w-0">
            <div className="flex items-center justify-between text-[10px] font-mono mb-1">
                <span className={`tracking-widest ${hit ? "text-[#00FF41]" : "text-[#52525B]"}`}>
                    {label} {hit && "✓"}
                </span>
                <span className={`${hit ? "text-[#00FF41]" : "text-[#A1A1AA]"}`}>
                    {bigPips != null ? `${bigPips}p` : "—"}
                    {smallPips != null && <span className="text-[#52525B] ml-1">({smallPips}p left)</span>}
                </span>
            </div>
            <div className="h-1.5 bg-[#121212] border border-[#1F1F1F] overflow-hidden">
                <div className={hit ? "bg-[#00FF41]" : "bg-[#FFD700]"}
                    style={{ width: `${Math.min(100, pct ?? 0)}%`, height: "100%", transition: "width 0.5s ease" }} />
            </div>
        </div>
    );
}

function TradeProgressRow({ t }) {
    const pnl = t.unrealised_pnl;
    const pnlColor = pnl == null ? "text-[#A1A1AA]" : (pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]");
    const pnlText = pnl == null ? "—" : `${pnl >= 0 ? "+$" : "-$"}${Math.abs(pnl).toFixed(2)}`;
    const inProfit = (t.pips_in_profit ?? 0) > 0;
    const ActionIcon = t.action === "BUY" ? TrendingUp : TrendingDown;
    const actionColor = t.action === "BUY" ? "#00FF41" : "#FF3B30";

    return (
        <div className="px-5 py-4 hover:bg-[#121212]" data-testid={`tt-row-${t.id}`}>
            <div className="flex items-center justify-between flex-wrap gap-3 mb-3">
                <div className="flex items-center gap-3 min-w-0">
                    <ActionIcon className="w-4 h-4 shrink-0" style={{ color: actionColor }} />
                    <div>
                        <div className="font-mono text-sm">
                            <span style={{ color: actionColor }}>{t.action}</span>
                            <span className="text-[#A1A1AA] mx-2">·</span>
                            <span>{t.symbol}</span>
                            <span className="text-[#A1A1AA] mx-2">·</span>
                            <span className="text-[#A1A1AA]">{t.lot_size} lot</span>
                            {t.tp1_closed && <span className="ml-2 font-mono text-[9px] tracking-widest text-[#00FF41] border border-[#00FF41]/40 bg-[#00FF41]/10 px-1">TP1✓</span>}
                            {t.tp2_closed && <span className="ml-1 font-mono text-[9px] tracking-widest text-[#00FF41] border border-[#00FF41]/40 bg-[#00FF41]/10 px-1">TP2✓</span>}
                            {t.breakeven_set && <span className="ml-1 font-mono text-[9px] tracking-widest text-[#FFD700] border border-[#FFD700]/40 bg-[#FFD700]/10 px-1">BE</span>}
                        </div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                            Entry {t.entry_price} · Now {t.current_price || "—"} · {t.pips_in_profit != null ? `${inProfit ? "+" : ""}${t.pips_in_profit} pips` : "no quote"}
                        </div>
                    </div>
                </div>
                <div className="text-right">
                    <div className={`font-mono text-lg font-medium ${pnlColor}`} data-testid={`tt-pnl-${t.id}`}>
                        {pnlText}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">UNREALISED P&L</div>
                </div>
            </div>
            <div className="flex gap-3">
                <ProgressBar label="SL"
                    pct={t.pips_in_profit < 0 ? Math.min(100, Math.abs(t.pips_in_profit) / Math.max(1, t.pips_to_sl + Math.abs(t.pips_in_profit)) * 100) : 0}
                    bigPips={t.pips_to_sl}
                    hit={false} />
                <ProgressBar label="TP1" pct={t.progress_to_tp1_pct} hit={t.tp1_closed} bigPips={t.pips_to_tp1} />
                <ProgressBar label="TP2" pct={t.progress_to_tp2_pct} hit={t.tp2_closed} bigPips={t.pips_to_tp2} />
                <ProgressBar label="TP3" pct={t.progress_to_tp3_pct} hit={t.tp3_closed} bigPips={t.pips_to_tp3} />
            </div>
        </div>
    );
}

export default function Dashboard() {
    const [quotes, setQuotes] = useState({});
    const [selected, setSelected] = useState("XAUUSD");
    const [history, setHistory] = useState([]);
    const [chartRange, setChartRange] = useState("1Y");  // 14D | 1M | 6M | 1Y
    const [indicators, setIndicators] = useState({});
    const [sentiment, setSentiment] = useState({});
    const [macro, setMacro] = useState({ events: [], freeze: null });
    const [stats, setStats] = useState(null);
    const [botStatus, setBotStatus] = useState(null);
    const [liveTrades, setLiveTrades] = useState([]);
    const [loading, setLoading] = useState(true);
    const [historyLoading, setHistoryLoading] = useState(false);
    const [err, setErr] = useState("");
    const { lastEvent, connected: wsConnected } = useLiveStream();
    const { user } = useAuth();
    const isAdmin = user?.role === "admin";
    const [diagOpen, setDiagOpen] = useState(false);
    const [diagSummary, setDiagSummary] = useState(null);  // { status, fails, warns }

    // Admin-only passive diagnostic — runs on mount + every 2min so the button
    // can badge red/amber when something needs attention, without forcing the
    // admin to click + wait.
    const loadDiag = useCallback(async () => {
        if (!isAdmin) return;
        try {
            const { data } = await api.get("/diagnostic/run");
            let fails = 0, warns = 0;
            for (const sec of data.sections || []) {
                for (const c of sec.checks || []) {
                    if (c.status === "fail") fails += 1;
                    else if (c.status === "warn") warns += 1;
                }
            }
            setDiagSummary({ status: data.status, fails, warns });
        } catch { /* stay silent on error */ }
    }, [isAdmin]);

    useEffect(() => {
        if (!isAdmin) return;
        loadDiag();
        const t = setInterval(loadDiag, 120_000);
        return () => clearInterval(t);
    }, [isAdmin, loadDiag, diagOpen]);  // refresh when modal closes too

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

    const loadLiveTrades = useCallback(async () => {
        try {
            const { data } = await api.get("/trades/live");
            setLiveTrades(data || []);
        } catch (e) {
            console.warn("[dashboard] live trades load failed", e?.message);
        }
    }, []);

    // Manual REFRESH button: fan out to every panel loader so the user gets
    // a fresh view of EVERYTHING (quotes, chart, stats, bot status, sentiment,
    // macro, live trades). `Promise.all` so the spinner reflects the slowest call.
    const [refreshing, setRefreshing] = useState(false);
    const refreshAll = useCallback(async () => {
        setRefreshing(true);
        try {
            await Promise.all([
                loadQuotes(),
                loadHistory(selected),
                loadStats(),
                loadBotStatus(),
                loadLiveTrades(),
                loadSentiment(selected),
                loadMacro(selected),
            ]);
        } finally {
            setRefreshing(false);
        }
    }, [selected, loadQuotes, loadHistory, loadStats, loadBotStatus, loadLiveTrades, loadSentiment, loadMacro]);

    useEffect(() => {
        loadQuotes(); loadStats(); loadBotStatus(); loadLiveTrades();
        // 10s for live prices (was 60s) — keeps price tiles flickering near real-time
        const quotesId = setInterval(loadQuotes, 10_000);
        const id = setInterval(() => { loadStats(); }, 60_000);
        const tickId = setInterval(loadBotStatus, 10_000);
        const liveId = setInterval(loadLiveTrades, 5_000);
        return () => { clearInterval(quotesId); clearInterval(id); clearInterval(tickId); clearInterval(liveId); };
    }, [loadQuotes, loadStats, loadBotStatus, loadLiveTrades]);

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
        } else if (lastEvent.type === "trade_management") {
            const p = lastEvent.payload || {};
            const labels = { BREAKEVEN: "🛡 Break-Even Set", TRAIL: "📈 SL Trailed", PARTIAL_CLOSE: "✂️ Partial Close" };
            toast(labels[p.action] || p.action, {
                description: p.action === "PARTIAL_CLOSE"
                    ? `Closing ${(p.from_lot - p.to_lot).toFixed(2)} lots @ +${p.r_multiple}R · ${p.to_lot} remaining`
                    : `New SL ${p.new_sl} · +${p.r_multiple}R`,
            });
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
                        <button onClick={refreshAll} disabled={refreshing}
                            data-testid="dashboard-refresh-button"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] disabled:opacity-50 text-xs font-mono tracking-widest transition-colors duration-150">
                            <ArrowsClockwise className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} />
                            {refreshing ? "REFRESHING…" : "REFRESH"}
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="dashboard-error">{err}</div>}

                <BotPulsePanel />

                <BotWatching />

                <CooldownPanel />

                <RiskGaugePanel />

                <WeeklyDigestPanel />

                <IntegrityWidget refreshSignal={lastEvent?.ts} />

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
                                value={`${stats.total_pnl >= 0 ? "+$" : "-$"}${Math.abs(stats.total_pnl).toFixed(2)}`}
                                accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}
                            />
                        </div>
                    </div>
                )}

                {/* Top of dashboard — single 0-100 score replaces "scan 7 strips" cognitive load. */}
                <BotHealthScore refreshSignal={lastEvent?.ts} />

                <MacroClimate />

                {isAdmin && (
                    <div className="flex items-center justify-between px-1">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">ADMIN TOOLS</div>
                        <DiagButton summary={diagSummary} onClick={() => setDiagOpen(true)} />
                    </div>
                )}
                <DiagnosticModal open={diagOpen} onClose={() => setDiagOpen(false)} />

                {/* EA Version Strip — surfaces stale builds across terminals */}
                <EaVersionStrip refreshSignal={lastEvent?.ts} />

                {/* iter-39 — Data Freshness: surfaces stale FRED/news/EA/etc. */}
                <DataFreshnessStrip />

                {/* Bot Status Strip */}
                {botStatus && <BotStatusStrip status={botStatus} />}

                {/* Per-account A/B comparison — only renders when user has 2+ accounts */}
                <PerAccountComparison refreshSignal={lastEvent?.ts} />

                {/* Time-to-Target — open positions live */}
                {liveTrades.length > 0 && <TimeToTargetPanel trades={liveTrades} />}

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
                    <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center justify-between gap-3 flex-wrap">
                        <div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">CHART · {CHART_RANGES[chartRange].label.toUpperCase()} · DAILY</div>
                            <div className="flex items-center gap-2">
                                <ChartLineUp className="w-4 h-4 text-[#00FF41]" />
                                <span className="font-display font-bold text-lg tracking-tight" data-testid="chart-symbol">{selected}</span>
                            </div>
                        </div>
                        <div className="flex items-center gap-2">
                            {/* Timeframe pills */}
                            <div className="flex items-center gap-1 border border-[#1F1F1F] p-0.5" data-testid="chart-range-selector">
                                {Object.entries(CHART_RANGES).map(([key, cfg]) => (
                                    <button key={key}
                                        onClick={() => setChartRange(key)}
                                        data-testid={`chart-range-${key}`}
                                        className={`px-2.5 py-1 font-mono text-[10px] tracking-widest transition-colors ${
                                            chartRange === key
                                                ? "bg-[#00FF41]/10 text-[#00FF41] border border-[#00FF41]/40"
                                                : "text-[#52525B] hover:text-[#A1A1AA] border border-transparent"
                                        }`}>
                                        {cfg.label}
                                    </button>
                                ))}
                            </div>
                            {(() => {
                                const sliced = history.slice(-CHART_RANGES[chartRange].days);
                                if (sliced.length < 2) return null;
                                const first = sliced[0].close;
                                const last = sliced[sliced.length - 1].close;
                                if (!first) return null;
                                const pct = ((last - first) / first) * 100;
                                const positive = pct >= 0;
                                return (
                                    <div className={`font-mono text-sm ml-2 ${positive ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                                        data-testid="chart-range-return">
                                        {positive ? "+" : ""}{pct.toFixed(2)}% / {CHART_RANGES[chartRange].shortLabel}
                                    </div>
                                );
                            })()}
                        </div>
                    </div>
                    <div className="h-64 md:h-80 p-2" data-testid="price-chart">
                        {historyLoading ? (
                            <div className="h-full flex items-center justify-center font-mono text-xs text-[#52525B] tracking-widest">LOADING DATA...</div>
                        ) : history.length === 0 ? (
                            <div className="h-full flex items-center justify-center font-mono text-xs text-[#52525B] tracking-widest">NO DATA AVAILABLE</div>
                        ) : (
                            <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1} debounce={50}>
                                <AreaChart data={history.slice(-CHART_RANGES[chartRange].days)}>
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
                                {sentiment[selected].key_drivers.map((d) => (
                                    <li key={d} className="font-mono text-[10px] text-[#A1A1AA] bg-[#121212] border border-[#1F1F1F] px-2 py-0.5">{d}</li>
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

function DiagButton({ summary, onClick }) {
    const fails = summary?.fails || 0;
    const warns = summary?.warns || 0;
    const total = fails + warns;
    let color, bg, border, label, pulse = false;
    if (fails > 0) {
        color = "#FF3B30"; bg = "bg-[#FF3B30]/10"; border = "border-[#FF3B30]/50";
        label = `RUN AUTO-DIAGNOSTIC · ${total} ISSUE${total !== 1 ? "S" : ""}`;
        pulse = true;
    } else if (warns > 0) {
        color = "#FFB000"; bg = "bg-[#FFB000]/10"; border = "border-[#FFB000]/40";
        label = `RUN AUTO-DIAGNOSTIC · ${warns} ADVISOR${warns !== 1 ? "IES" : "Y"}`;
    } else if (summary) {
        color = "#00FF41"; bg = "bg-transparent hover:bg-[#00FF41]/10"; border = "border-[#00FF41]/40";
        label = "RUN AUTO-DIAGNOSTIC · ALL OK";
    } else {
        color = "#FFB000"; bg = "hover:bg-[#FFB000]/10"; border = "border-[#FFB000]/40";
        label = "RUN AUTO-DIAGNOSTIC";
    }
    return (
        <button onClick={onClick}
                className={`px-3 py-1.5 border ${border} ${bg} font-mono text-[10px] tracking-widest inline-flex items-center gap-2 ${pulse ? "animate-pulse" : ""}`}
                style={{ color }}
                data-testid="open-diagnostic-btn"
                aria-label={label}>
            <Stethoscope className="w-3.5 h-3.5" />
            {label}
        </button>
    );
}
