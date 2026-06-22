import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { RefreshCw as ArrowsClockwise, X } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";

const STATUS_STYLE = {
    pending: "border-[#FFB000]/40 text-[#FFB000]",
    open: "border-[#00FF41]/40 text-[#00FF41]",
    closed: "border-[#1F1F1F] text-[#A1A1AA]",
    cancelled: "border-[#1F1F1F] text-[#52525B]",
    failed: "border-[#FF3B30]/40 text-[#FF3B30]",
};

// MT5 standard contract sizes — used to derive live $-P&L per open trade.
// XAUUSD: 1 lot = 100 oz   → $1 move = $100 P&L per 1.00 lot
// BTCUSD: 1 lot = 1 BTC    → $1 move = $1 P&L
// XAGUSD: 1 lot = 5000 oz
// FX majors: 1 lot = 100k units → 1 pip ≈ $10 (handled per-symbol if added)
const CONTRACT_SIZE = {
    XAUUSD: 100,
    BTCUSD: 1,
    ETHUSD: 1,
    XAGUSD: 5000,
};

function priceDecimals(symbol) {
    if (!symbol) return 2;
    if (symbol === "BTCUSD" || symbol === "ETHUSD") return 2;
    if (symbol === "XAUUSD" || symbol === "XAGUSD") return 2;
    if (symbol.includes("JPY")) return 3;
    return 5;
}

function fmtPrice(symbol, p) {
    if (p == null || Number.isNaN(parseFloat(p))) return "—";
    return parseFloat(p).toFixed(priceDecimals(symbol));
}

function fmtPnl(v) {
    if (v == null || Number.isNaN(v)) return "—";
    const sign = v >= 0 ? "+$" : "-$";
    return `${sign}${Math.abs(v).toFixed(2)}`;
}

function computeLivePnl(trade, currentPrice) {
    if (!currentPrice || trade.status !== "open") return null;
    const entry = parseFloat(trade.entry_price);
    const lot = parseFloat(trade.lot_size);
    const cs = CONTRACT_SIZE[trade.symbol] ?? 1;
    if (!entry || !lot || Number.isNaN(entry) || Number.isNaN(lot)) return null;
    const diff = trade.action === "BUY"
        ? (currentPrice - entry)
        : (entry - currentPrice);
    return diff * lot * cs;
}

const CLOSE_REASON_BADGE = {
    take_profit:     { label: "TP",       cls: "border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]",         icon: "🎯" },
    stop_loss:       { label: "SL",       cls: "border-[#FF3B30]/40 bg-[#FF3B30]/10 text-[#FF3B30]",         icon: "🛑" },
    manual:          { label: "MANUAL",   cls: "border-[#FFD700]/40 bg-[#FFD700]/10 text-[#FFD700]",         icon: "✋" },
    manual_telegram: { label: "TELEGRAM", cls: "border-[#FFD700]/40 bg-[#FFD700]/10 text-[#FFD700]",         icon: "✋" },
    nl_command:      { label: "NL CMD",   cls: "border-[#FFD700]/40 bg-[#FFD700]/10 text-[#FFD700]",         icon: "✋" },
    panic:           { label: "PANIC",    cls: "border-[#FF3B30]/40 bg-[#FF3B30]/10 text-[#FF3B30]",         icon: "🚨" },
    circuit_breaker: { label: "BREAKER",  cls: "border-[#FFB000]/40 bg-[#FFB000]/10 text-[#FFB000]",         icon: "🚧" },
    broker:          { label: "BROKER",   cls: "border-[#1F1F1F] bg-[#0A0A0A] text-[#A1A1AA]",               icon: "·" },
};

function fmtDateTime(iso) {
    if (!iso) return "—";
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return "—";
        // e.g. "22 Jun · 16:23"
        return d.toLocaleString(undefined, {
            day: "2-digit", month: "short",
            hour: "2-digit", minute: "2-digit",
            hour12: false,
        }).replace(",", " ·");
    } catch {
        return "—";
    }
}

function Stat({ label, value, accent }) {
    return (
        <div className="p-4 border border-[#1F1F1F] bg-[#0A0A0A]">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className={`font-mono font-medium text-base ${accent || "text-white"}`}>{value}</div>
        </div>
    );
}

export default function Trades() {
    const [trades, setTrades] = useState([]);
    const [stats, setStats] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");
    const [filter, setFilter] = useState("");
    const [quotes, setQuotes] = useState({}); // {SYMBOL: price}

    const load = useCallback(async () => {
        try {
            const [t, s] = await Promise.all([
                api.get(`/trades${filter ? `?status=${filter}` : ""}`),
                api.get("/trades/stats"),
            ]);
            setTrades(t.data); setStats(s.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, [filter]);

    useEffect(() => { load(); }, [load]);

    // Live: refresh on any trade event
    const { lastEvent } = useLiveStream();
    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "trade_created" || lastEvent.type === "trade_updated") load();
    }, [lastEvent, load]);

    // Poll quotes for any open-trade symbols every 5s — drives live price + P&L.
    const openSymbols = useMemo(() => {
        const set = new Set();
        for (const t of trades) {
            if (t.status === "open" && t.symbol) set.add(t.symbol);
        }
        return Array.from(set);
    }, [trades]);

    const symbolsKey = openSymbols.join(",");
    const inFlightRef = useRef(false);
    useEffect(() => {
        if (!symbolsKey) {
            setQuotes({});
            return undefined;
        }
        let cancelled = false;
        const fetchQuotes = async () => {
            if (inFlightRef.current) return;
            inFlightRef.current = true;
            try {
                const { data } = await api.get(`/market/quotes?symbols=${symbolsKey}`);
                if (cancelled) return;
                const next = {};
                for (const q of data.quotes || []) {
                    if (q.symbol && q.price != null && !q.error) {
                        next[q.symbol] = parseFloat(q.price);
                    }
                }
                setQuotes(next);
            } catch {
                /* keep stale quote on transient errors */
            } finally {
                inFlightRef.current = false;
            }
        };
        fetchQuotes();
        const id = setInterval(fetchQuotes, 5000);
        return () => { cancelled = true; clearInterval(id); };
    }, [symbolsKey]);

    const close = async (id) => {
        if (!window.confirm("Send close instruction to MT5 EA?")) return;
        try { await api.post(`/trades/${id}/close`); await load(); } catch (e) { setErr(formatApiError(e)); }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Trades"
                subtitle="Open positions and historical trades synced from MT5 EA."
                testid="trades-header"
                action={
                    <button onClick={load} data-testid="trades-refresh-button"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                        <ArrowsClockwise className="w-3.5 h-3.5" /> REFRESH
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-4">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                {stats && (
                    <div className="grid grid-cols-2 md:grid-cols-6 gap-3" data-testid="trades-stats">
                        <Stat label="OPEN" value={stats.open_trades} />
                        <Stat label="OPEN LIVE P&L" value={(() => {
                            const live = trades.reduce((acc, t) => {
                                if (t.status !== "open") return acc;
                                const p = computeLivePnl(t, quotes[t.symbol]);
                                return p == null ? acc : acc + p;
                            }, 0);
                            const hasAny = trades.some(t => t.status === "open" && quotes[t.symbol] != null);
                            return hasAny ? fmtPnl(live) : "—";
                        })()} accent={(() => {
                            const live = trades.reduce((acc, t) => {
                                if (t.status !== "open") return acc;
                                const p = computeLivePnl(t, quotes[t.symbol]);
                                return p == null ? acc : acc + p;
                            }, 0);
                            return live >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]";
                        })()} />
                        <Stat label="TOTAL" value={stats.total_trades} />
                        <Stat label="WIN RATE" value={`${stats.win_rate}%`} accent="text-[#00FF41]" />
                        <Stat label="WINS / LOSSES" value={`${stats.wins} / ${stats.losses}`} />
                        <Stat label="TOTAL P&L" value={`${stats.total_pnl >= 0 ? "+$" : "-$"}${Math.abs(stats.total_pnl).toFixed(2)}`} accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
                    </div>
                )}

                <div className="flex gap-2 flex-wrap">
                    {["", "pending", "open", "closed", "failed"].map(f => (
                        <button key={f || "all"} onClick={() => setFilter(f)}
                            data-testid={`filter-${f || "all"}`}
                            className={`px-3 py-1.5 text-xs font-mono tracking-widest border transition-colors ${
                                filter === f ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                            }`}>
                            {(f || "ALL").toUpperCase()}
                        </button>
                    ))}
                </div>

                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING TRADES…</div>
                ) : trades.length === 0 ? (
                    <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="trades-empty">
                        <div className="font-display font-bold text-lg mb-1">No trades yet</div>
                        <div className="text-sm text-[#A1A1AA]">Generate an AI signal and execute it from the Signals page.</div>
                    </div>
                ) : (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] overflow-x-auto">
                        <table className="w-full text-sm" data-testid="trades-table">
                            <thead>
                                <tr className="border-b border-[#1F1F1F]">
                                    {["SYMBOL", "SIDE", "LOTS", "ENTRY", "CURRENT", "SL", "TP", "EXIT", "LIVE P&L", "P&L", "OPENED", "CLOSED", "STATUS", ""].map(h => (
                                        <th key={h} className="px-3 py-2 text-left font-mono text-[10px] text-[#52525B] tracking-widest whitespace-nowrap">{h}</th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {trades.map(t => (
                                    <tr key={t.id} className="border-b border-[#1F1F1F] hover:bg-[#121212] transition-colors" data-testid={`trade-row-${t.id}`}>
                                        <td className="px-3 py-2 font-mono">
                                            <div className="flex items-center gap-1.5">
                                                <span>{t.symbol}</span>
                                                {t.partial_closed && <span title="Partial close at TP1 executed" className="font-mono text-[9px] tracking-widest text-[#00FF41] border border-[#00FF41]/40 bg-[#00FF41]/10 px-1" data-testid={`badge-pc-${t.id}`}>PC</span>}
                                                {t.breakeven_set && <span title="SL moved to break-even" className="font-mono text-[9px] tracking-widest text-[#FFD700] border border-[#FFD700]/40 bg-[#FFD700]/10 px-1" data-testid={`badge-be-${t.id}`}>BE</span>}
                                                {t.trail_active && <span title="Trailing stop active" className="font-mono text-[9px] tracking-widest text-[#00FF41] border border-[#00FF41]/40 bg-[#00FF41]/10 px-1" data-testid={`badge-trail-${t.id}`}>TRAIL</span>}
                                                {t.pending_modification && <span title={`Pending: ${t.pending_modification.type}`} className="font-mono text-[9px] tracking-widest text-[#FFB000] border border-[#FFB000]/40 bg-[#FFB000]/10 px-1 animate-pulse" data-testid={`badge-pending-${t.id}`}>SYNC</span>}
                                            </div>
                                        </td>
                                        <td className={`px-3 py-2 font-mono ${t.action === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{t.action}</td>
                                        <td className="px-3 py-2 font-mono">{t.lot_size}</td>
                                        <td className="px-3 py-2 font-mono">{fmtPrice(t.symbol, t.entry_price)}</td>
                                        <td className="px-3 py-2 font-mono" data-testid={`current-${t.id}`}>
                                            {t.status === "open" && quotes[t.symbol] != null ? (
                                                <span className="text-white">{fmtPrice(t.symbol, quotes[t.symbol])}</span>
                                            ) : (
                                                <span className="text-[#52525B]">—</span>
                                            )}
                                        </td>
                                        <td className="px-3 py-2 font-mono text-[#FF3B30]">{fmtPrice(t.symbol, t.stop_loss)}</td>
                                        <td className="px-3 py-2 font-mono text-[#00FF41]">{fmtPrice(t.symbol, t.take_profit)}</td>
                                        <td className="px-3 py-2 font-mono">{t.exit_price != null ? fmtPrice(t.symbol, t.exit_price) : "—"}</td>
                                        <td className="px-3 py-2 font-mono" data-testid={`live-pnl-${t.id}`}>
                                            {(() => {
                                                if (t.status !== "open") return <span className="text-[#52525B]">—</span>;
                                                const live = computeLivePnl(t, quotes[t.symbol]);
                                                if (live == null) return <span className="text-[#52525B]">…</span>;
                                                const cls = live >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]";
                                                return <span className={cls}>{fmtPnl(live)}</span>;
                                            })()}
                                        </td>
                                        <td className={`px-3 py-2 font-mono ${(t.pnl ?? 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                            {t.pnl != null && t.pnl !== 0
                                                ? `${t.pnl >= 0 ? "+$" : "-$"}${Math.abs(t.pnl).toFixed(2)}`
                                                : "—"}
                                        </td>
                                        <td className="px-3 py-2 font-mono text-[#A1A1AA] whitespace-nowrap" data-testid={`opened-${t.id}`}>
                                            {fmtDateTime(t.opened_at)}
                                        </td>
                                        <td className="px-3 py-2 font-mono text-[#A1A1AA] whitespace-nowrap" data-testid={`closed-${t.id}`}>
                                            {fmtDateTime(t.closed_at)}
                                        </td>
                                        <td className="px-3 py-2">
                                            <div className="flex items-center gap-1.5 flex-wrap">
                                                <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border inline-block ${STATUS_STYLE[t.status] || "border-[#1F1F1F]"}`}>
                                                    {t.status?.toUpperCase()}
                                                </span>
                                                {t.status === "closed" && t.close_reason && CLOSE_REASON_BADGE[t.close_reason] && (
                                                    <span title={`Closed by: ${t.close_reason}`}
                                                        data-testid={`close-reason-${t.id}`}
                                                        className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border inline-flex items-center gap-1 ${CLOSE_REASON_BADGE[t.close_reason].cls}`}>
                                                        <span>{CLOSE_REASON_BADGE[t.close_reason].icon}</span>
                                                        {CLOSE_REASON_BADGE[t.close_reason].label}
                                                    </span>
                                                )}
                                            </div>
                                        </td>
                                        <td className="px-3 py-2">
                                            {t.status === "open" && (
                                                <button onClick={() => close(t.id)} data-testid={`close-trade-${t.id}`}
                                                    className="text-[#FF3B30] hover:text-[#FF6B61] text-xs font-mono tracking-widest flex items-center gap-1">
                                                    <X className="w-3 h-3" /> CLOSE
                                                </button>
                                            )}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
