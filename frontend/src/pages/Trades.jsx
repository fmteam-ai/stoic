import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { RefreshCw as ArrowsClockwise, X, Trash2 as Trash, ChevronDown, GitMerge, History, RotateCcw, Sparkles, FlaskConical } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";
import { toast } from "sonner";
import BotPulsePanel from "@/components/BotPulsePanel";
import { Link } from "react-router-dom";

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

// Distance (in $) from current price to a SL/TP level, in account currency,
// using MT5 contract size math. Sign agnostic — always positive.
function dollarDistance(trade, currentPrice, levelPrice) {
    if (!currentPrice || !levelPrice) return null;
    const lvl = parseFloat(levelPrice);
    const lot = parseFloat(trade.lot_size);
    const cs = CONTRACT_SIZE[trade.symbol] ?? 1;
    if (!lvl || !lot || Number.isNaN(lvl) || Number.isNaN(lot)) return null;
    return Math.abs(currentPrice - lvl) * lot * cs;
}

// Median of an array of numbers — robust to outliers (small spike won't skew ETA).
function median(arr) {
    if (!arr || arr.length === 0) return 0;
    const s = [...arr].sort((a, b) => a - b);
    const mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid] : 0.5 * (s[mid - 1] + s[mid]);
}

// Format a number of seconds into "3m", "2h 14m", "imminent", etc.
// Returns "—" for null/no data (still calibrating), "stalled" only when
// a velocity was measurable but came back as 0.
function formatEta(seconds) {
    if (seconds == null) return "—";
    if (!Number.isFinite(seconds) || seconds < 0) return "stalled";
    if (seconds < 60) return "imminent";
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    if (seconds < 86400) {
        const h = Math.floor(seconds / 3600);
        const m = Math.round((seconds % 3600) / 60);
        return m ? `${h}h ${m}m` : `${h}h`;
    }
    return `${Math.round(seconds / 86400)}d`;
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

function Stat({ label, value, accent, testid }) {
    return (
        <div className="p-4 border border-[#1F1F1F] bg-[#0A0A0A]" data-testid={testid}>
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className={`font-mono font-medium text-base ${accent || "text-white"}`}>{value}</div>
        </div>
    );
}

function RiskThermometer({ openLive, hasAnyLive, liveAccent, closestSL, closestTP }) {
    return (
        <div className="p-4 border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="stat-open-live-pnl">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">OPEN LIVE P&L</div>
            <div className={`font-mono font-medium text-base ${liveAccent || "text-white"}`}>
                {hasAnyLive ? fmtPnl(openLive) : "—"}
            </div>
            {(closestSL || closestTP) && (
                <div className="mt-2 pt-2 border-t border-[#1F1F1F] space-y-1">
                    {closestSL && (
                        <div className="flex items-center justify-between gap-2 font-mono text-[10px]" data-testid="closest-sl">
                            <span className="text-[#52525B] tracking-widest">SL ←</span>
                            <span className="text-[#FF3B30]" title={`${closestSL.symbol} @ ${closestSL.level}`}>
                                ${closestSL.dist.toFixed(2)}
                                <span className="text-[#52525B] ml-1">{closestSL.symbol}</span>
                                <span className="text-[#FF3B30]/70 ml-1" data-testid="closest-sl-eta">· {formatEta(closestSL.eta)}</span>
                            </span>
                        </div>
                    )}
                    {closestTP && (
                        <div className="flex items-center justify-between gap-2 font-mono text-[10px]" data-testid="closest-tp">
                            <span className="text-[#52525B] tracking-widest">TP →</span>
                            <span className="text-[#00FF41]" title={`${closestTP.symbol} @ ${closestTP.level}`}>
                                ${closestTP.dist.toFixed(2)}
                                <span className="text-[#52525B] ml-1">{closestTP.symbol}</span>
                                <span className="text-[#00FF41]/70 ml-1" data-testid="closest-tp-eta">· {formatEta(closestTP.eta)}</span>
                            </span>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

export default function Trades() {
    const [trades, setTrades] = useState([]);
    const [stats, setStats] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");
    const [filter, setFilter] = useState("");
    const [accountFilter, setAccountFilter] = useState("");   // "" = all accounts; otherwise account_id
    const [accounts, setAccounts] = useState([]);             // for filter pills + label lookup
    const [quotes, setQuotes] = useState({}); // {SYMBOL: price}  ← polled fallback
    const quoteHistoryRef = useRef({}); // {SYMBOL: [{ts, price}]}
    const [velocities, setVelocities] = useState({}); // {SYMBOL: priceUnitsPerSec}
    // EA v1.27+ broker-real-time tick relay. Keyed by mt5_ticket so each
    // open position carries its own broker-exact current_price + profit,
    // refreshed every PollSeconds (3-5s) via the WS `position_ticks` event.
    const [liveTicks, setLiveTicks] = useState({}); // {ticket: {current_price, profit, ts, symbol}}

    // Account_id → label map for inline display on each trade row.
    const accountLabelById = useMemo(() => {
        const m = {};
        for (const a of accounts) m[a.id] = a.label || a.account_number || a.id.slice(-6);
        return m;
    }, [accounts]);

    const [refreshing, setRefreshing] = useState(false);
    const [auditTrade, setAuditTrade] = useState(null);  // trade whose audit trail modal is open
    const [explainTrade, setExplainTrade] = useState(null);  // trade whose AI-explainability modal is open
    const load = useCallback(async () => {
        setRefreshing(true);
        try {
            const params = new URLSearchParams();
            if (filter) params.set("status", filter);
            if (accountFilter) params.set("account_id", accountFilter);
            const qs = params.toString();
            const statsParams = accountFilter ? `?account_id=${accountFilter}` : "";
            const [t, s] = await Promise.all([
                api.get(`/trades${qs ? `?${qs}` : ""}`),
                api.get(`/trades/stats${statsParams}`),
            ]);
            setTrades(t.data); setStats(s.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); setRefreshing(false); }
    }, [filter, accountFilter]);

    useEffect(() => { load(); }, [load]);

    // Accounts list — only refetched on mount (cheap, rarely changes during a session).
    useEffect(() => {
        api.get("/accounts").then(r => setAccounts(r.data || [])).catch(() => {});
    }, []);

    // Live: refresh on any trade event + ingest broker-real-time ticks.
    const { lastEvent } = useLiveStream();
    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "trade_created" || lastEvent.type === "trade_updated") load();
        // EA v1.27+: every heartbeat carries per-position broker-live ticks.
        // Replace the bucket for each ticket so we never accumulate stale entries.
        if (lastEvent.type === "position_ticks") {
            const ticks = lastEvent.payload?.ticks || [];
            if (!ticks.length) return;
            const ts = lastEvent.payload?.ts || new Date().toISOString();
            setLiveTicks(prev => {
                const next = { ...prev };
                for (const t of ticks) {
                    if (t.ticket != null && t.current_price != null) {
                        next[String(t.ticket)] = {
                            current_price: Number(t.current_price),
                            profit: Number(t.profit ?? 0),
                            symbol: t.symbol,
                            ts,
                        };
                    }
                }
                return next;
            });
        }
    }, [lastEvent, load]);

    // Poll quotes for any open-trade symbols every 5s — drives live price + P&L.
    const openSymbols = useMemo(() => {
        const set = new Set();
        for (const t of trades) {
            if (t.status === "open" && t.symbol) set.add(t.symbol);
        }
        // Sort for a deterministic key — avoids redundant re-fetches when the
        // first-encountered symbol order flips on WS updates.
        return Array.from(set).sort();
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
                const hist = quoteHistoryRef.current;
                for (const q of data.quotes || []) {
                    if (q.symbol && q.price != null && !q.error) {
                        const p = parseFloat(q.price);
                        next[q.symbol] = p;
                        // Sample by UPSTREAM timestamp (not local poll time) so
                        // cached responses don't pollute the velocity buffer.
                        // Backend caches quotes for 60-120s, so we only add a
                        // new sample when the upstream timestamp advances.
                        const ts = q.timestamp ? Date.parse(q.timestamp) : Date.now();
                        if (Number.isNaN(ts)) continue;
                        const buf = hist[q.symbol] || [];
                        const last = buf[buf.length - 1];
                        if (!last || last.ts !== ts) {
                            buf.push({ ts, price: p });
                            if (buf.length > 20) buf.shift();
                            hist[q.symbol] = buf;
                        }
                    }
                }
                setQuotes(next);
                // Recompute velocities: median |Δp|/Δt across consecutive pairs.
                const vel = {};
                for (const sym of Object.keys(hist)) {
                    const buf = hist[sym];
                    if (!buf || buf.length < 2) continue;
                    const rates = [];
                    for (let i = 1; i < buf.length; i++) {
                        const dt = (buf[i].ts - buf[i - 1].ts) / 1000;
                        if (dt <= 0) continue;
                        rates.push(Math.abs(buf[i].price - buf[i - 1].price) / dt);
                    }
                    // Need at least 2 samples (1 rate) to claim a velocity.
                    if (rates.length >= 1) vel[sym] = median(rates);
                }
                setVelocities(vel);
            } catch (err) {
                // Quote-polling errors are routine (CoinGecko 429s, brief network blips).
                // Keep the stale price on screen; surface at debug-level only.
                console.debug("[Trades] quote poll failed:", err?.message || err);
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

    const revive = async (id) => {
        if (!window.confirm(
            "Revive this trade?\n\n" +
            "Use this when STOIC marked the trade closed but the broker still " +
            "has the position open (e.g. PANIC or Force-Sync was too eager). " +
            "The trade returns to status=open and the bot can manage it again " +
            "(break-even, trailing, partial-close, PANIC)."
        )) return;
        try {
            await api.post(`/trades/${id}/revive`);
            toast.success("Trade revived — bot is now managing it again");
            await load();
        } catch (e) {
            toast.error("Revive failed", { description: formatApiError(e) });
        }
    };

    const [reconciling, setReconciling] = useState(false);
    const reconcile = async (force = false) => {
        const confirmMsg = force
            ? "FORCE SYNC — close ALL pending/open trades right now?\n\n" +
              "Use this ONLY when you've verified on MT5 that the positions are " +
              "actually closed but your EA is offline. This skips the safety " +
              "checks and marks every DB-open / pending-close trade as closed, " +
              "and cancels never-filled pending opens.\n\n" +
              "Continue?"
            : "Sync open trades with the broker?\n\n" +
              "Any STOIC-open trade that the broker no longer reports as open " +
              "will be marked closed (orphaned by a missed EA report). This is " +
              "safe — it never opens or modifies real positions.";
        if (!window.confirm(confirmMsg)) return;
        setReconciling(true);
        try {
            const url = force ? "/trades/reconcile?force=true" : "/trades/reconcile";
            const { data } = await api.post(url);
            const total = data.total_closed || 0;
            if (total > 0) {
                toast.success(`Synced · closed ${total} orphan${total === 1 ? "" : "s"}`, {
                    description: force
                        ? "Force-sync complete. Pending opens cancelled, pending closes marked closed."
                        : "Trades that had hit SL/TP at the broker are now reflected here.",
                });
            } else {
                // Check if any accounts were skipped due to stale heartbeat → offer force option
                const skippedStale = (data.accounts || []).some(a =>
                    a.skipped && a.reason === "ea_too_old_and_positions_nonzero");
                if (skippedStale && !force) {
                    if (window.confirm(
                        "Standard sync found nothing to close because your EA hasn't sent a " +
                        "heartbeat recently. We can't verify the broker's current state.\n\n" +
                        "If you've manually verified on MT5 that the positions are closed, " +
                        "click OK to FORCE SYNC anyway. Otherwise click Cancel and bring your " +
                        "EA back online first.",
                    )) {
                        setReconciling(false);
                        return reconcile(true);   // recurse with force=true
                    }
                } else {
                    toast(`All open trades match the broker — nothing to close.`);
                }
            }
            const skipped = (data.accounts || []).filter(a => a.skipped);
            if (skipped.length && !force) {
                toast(`${skipped.length} account(s) skipped — update your MT5 EA to v1.22+ for live reconciliation.`);
            }
            await load();
        } catch (e) {
            toast.error("Sync failed", { description: formatApiError(e) });
        } finally {
            setReconciling(false);
        }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Trades"
                subtitle="Open positions and historical trades synced from MT5 EA."
                testid="trades-header"
                action={
                    <div className="flex gap-2 items-center">
                        <ClearTradesMenu trades={trades} onCleared={load} />
                        <button onClick={reconcile} disabled={reconciling}
                            data-testid="trades-reconcile-button"
                            title="Close any STOIC-open trade that the broker no longer reports as open"
                            className="flex items-center gap-2 px-3 py-2 border border-[#FFB000]/40 hover:bg-[#FFB000]/10 disabled:opacity-50 text-[#FFB000] text-xs font-mono tracking-widest transition-colors">
                            <GitMerge className="w-3.5 h-3.5" /> {reconciling ? "SYNCING…" : "SYNC WITH BROKER"}
                        </button>
                        <button onClick={load} disabled={refreshing}
                            data-testid="trades-refresh-button"
                            className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] disabled:opacity-50 text-xs font-mono tracking-widest transition-colors">
                            <ArrowsClockwise className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} />
                            {refreshing ? "REFRESHING…" : "REFRESH"}
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-4">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                <BotPulsePanel />

                {stats && (() => {
                    // Prefer the EA's broker-real-time tick (per-ticket, 3-5s) over the
                    // 5s-polled external quote. Falls back to the polled quote when the
                    // user is on an older EA (<v1.27) that doesn't emit current_price.
                    const priceFor = (t) => {
                        const tick = t.mt5_ticket != null ? liveTicks[String(t.mt5_ticket)] : null;
                        if (tick && tick.current_price != null) return tick.current_price;
                        return quotes[t.symbol];
                    };
                    const openLive = trades.reduce((acc, t) => {
                        if (t.status !== "open") return acc;
                        // Broker-reported profit is broker-exact — prefer it when available.
                        const tick = t.mt5_ticket != null ? liveTicks[String(t.mt5_ticket)] : null;
                        if (tick && Number.isFinite(tick.profit)) return acc + tick.profit;
                        const p = computeLivePnl(t, priceFor(t));
                        return p == null ? acc : acc + p;
                    }, 0);
                    const hasAnyLive = trades.some(t => t.status === "open" && priceFor(t) != null);
                    const liveAccent = hasAnyLive
                        ? (openLive >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]")
                        : undefined;

                    // Closest SL & TP across all open positions (smallest $-distance).
                    let closestSL = null;     // {dist, symbol, level, eta?}
                    let closestTP = null;
                    for (const t of trades) {
                        if (t.status !== "open") continue;
                        const px = priceFor(t);
                        if (!px) continue;
                        const vel = velocities[t.symbol]; // price units / second
                        const slLevel = parseFloat(t.stop_loss);
                        const tpLevel = parseFloat(t.tp1 || t.take_profit);
                        const dSL = dollarDistance(t, px, slLevel);
                        if (dSL != null && (closestSL == null || dSL < closestSL.dist)) {
                            const priceDist = Math.abs(px - slLevel);
                            const eta = (vel != null && vel > 0) ? priceDist / vel : null;
                            closestSL = { dist: dSL, symbol: t.symbol, level: slLevel, eta };
                        }
                        const dTP = dollarDistance(t, px, tpLevel);
                        if (dTP != null && (closestTP == null || dTP < closestTP.dist)) {
                            const priceDist = Math.abs(px - tpLevel);
                            const eta = (vel != null && vel > 0) ? priceDist / vel : null;
                            closestTP = { dist: dTP, symbol: t.symbol, level: tpLevel, eta };
                        }
                    }

                    return (
                    <div className="grid grid-cols-2 md:grid-cols-6 gap-3" data-testid="trades-stats">
                        <Stat label="OPEN" value={stats.open_trades} />
                        <RiskThermometer
                            openLive={openLive}
                            hasAnyLive={hasAnyLive}
                            liveAccent={liveAccent}
                            closestSL={closestSL}
                            closestTP={closestTP}
                        />
                        <Stat label="TOTAL" value={stats.total_trades} />
                        <Stat label="WIN RATE" value={`${stats.win_rate}%`} accent="text-[#00FF41]" />
                        <Stat label="WINS / LOSSES" value={`${stats.wins} / ${stats.losses}`} />
                        <Stat label="TOTAL P&L" value={`${stats.total_pnl >= 0 ? "+$" : "-$"}${Math.abs(stats.total_pnl).toFixed(2)}`} accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
                    </div>
                    );
                })()}

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

                {accounts.length > 1 && (
                    <div className="flex items-center gap-2 flex-wrap" data-testid="account-filter-row">
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest mr-1">ACCOUNT ·</span>
                        <button onClick={() => setAccountFilter("")}
                            data-testid="account-filter-all"
                            className={`px-3 py-1.5 text-xs font-mono tracking-widest border transition-colors ${
                                !accountFilter ? "border-[#FFD700] text-[#FFD700]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                            }`}>
                            ALL ACCOUNTS
                        </button>
                        {accounts.map(a => (
                            <button key={a.id} onClick={() => setAccountFilter(a.id)}
                                data-testid={`account-filter-${a.id}`}
                                className={`px-3 py-1.5 text-xs font-mono tracking-widest border transition-colors flex items-center gap-1.5 ${
                                    accountFilter === a.id ? "border-[#FFD700] text-[#FFD700]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                                }`}>
                                <span>{(a.label || a.account_number || "—").toUpperCase()}</span>
                                <span className="font-mono text-[9px] text-[#52525B]">· {(a.mode || "live").toUpperCase()}</span>
                            </button>
                        ))}
                    </div>
                )}

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
                                    {["SYMBOL", "ACCOUNT", "SIDE", "LOTS", "ENTRY", "CURRENT", "SL", "TP", "EXIT", "LIVE P&L", "P&L", "OPENED", "CLOSED", "STATUS", ""].map(h => (
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
                                        <td className="px-3 py-2 font-mono text-[#A1A1AA] text-xs whitespace-nowrap" data-testid={`trade-account-${t.id}`}>
                                            {accountLabelById[t.account_id] || (t.account_id ? `…${t.account_id.slice(-6)}` : "—")}
                                        </td>
                                        <td className={`px-3 py-2 font-mono ${t.action === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{t.action}</td>
                                        <td className="px-3 py-2 font-mono">{t.lot_size}</td>
                                        <td className="px-3 py-2 font-mono">{fmtPrice(t.symbol, t.entry_price)}</td>
                                        <td className="px-3 py-2 font-mono" data-testid={`current-${t.id}`}>
                                            {(() => {
                                                if (t.status !== "open") return <span className="text-[#52525B]">—</span>;
                                                const tick = t.mt5_ticket != null ? liveTicks[String(t.mt5_ticket)] : null;
                                                const px = tick?.current_price ?? quotes[t.symbol];
                                                if (px == null) return <span className="text-[#52525B]">—</span>;
                                                // Green dot when sourced from broker-live EA tick; faint white when fallback feed.
                                                return (
                                                    <span className="inline-flex items-center gap-1.5">
                                                        {tick && (
                                                            <span
                                                                title="Broker-live (EA tick)"
                                                                data-testid={`tick-live-${t.id}`}
                                                                className="inline-block w-1.5 h-1.5 rounded-full bg-[#00FF41] animate-pulse"
                                                            />
                                                        )}
                                                        <span className="text-white">{fmtPrice(t.symbol, px)}</span>
                                                    </span>
                                                );
                                            })()}
                                        </td>
                                        <td className="px-3 py-2 font-mono text-[#FF3B30]">{fmtPrice(t.symbol, t.stop_loss)}</td>
                                        <td className="px-3 py-2 font-mono text-[#00FF41]">{fmtPrice(t.symbol, t.take_profit)}</td>
                                        <td className="px-3 py-2 font-mono">{t.exit_price != null ? fmtPrice(t.symbol, t.exit_price) : "—"}</td>
                                        <td className="px-3 py-2 font-mono" data-testid={`live-pnl-${t.id}`}>
                                            {(() => {
                                                if (t.status !== "open") return <span className="text-[#52525B]">—</span>;
                                                // Prefer broker-reported profit (EA tick) — broker-exact incl. swap/commission.
                                                const tick = t.mt5_ticket != null ? liveTicks[String(t.mt5_ticket)] : null;
                                                let live;
                                                if (tick && Number.isFinite(tick.profit)) {
                                                    live = tick.profit;
                                                } else {
                                                    live = computeLivePnl(t, quotes[t.symbol]);
                                                }
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
                                            <div className="flex items-center gap-3">
                                                {t.status === "open" && (
                                                    <button onClick={() => close(t.id)} data-testid={`close-trade-${t.id}`}
                                                        className="text-[#FF3B30] hover:text-[#FF6B61] text-xs font-mono tracking-widest flex items-center gap-1">
                                                        <X className="w-3 h-3" /> CLOSE
                                                    </button>
                                                )}
                                                {t.status === "closed" && t.exit_price == null && (
                                                    <button onClick={() => revive(t.id)} data-testid={`revive-trade-${t.id}`}
                                                        title="STOIC marked this closed but the broker still has the position open. Click to bring it back under the bot's control."
                                                        className="text-[#00FF41] hover:text-[#33FF66] text-xs font-mono tracking-widest flex items-center gap-1">
                                                        <RotateCcw className="w-3 h-3" /> REVIVE
                                                    </button>
                                                )}
                                                {t.mt5_ticket && (
                                                    <button onClick={() => setAuditTrade(t)} data-testid={`audit-trade-${t.id}`}
                                                        title="Show every broker deal that touched this position"
                                                        className="text-[#A1A1AA] hover:text-[#FFD700] text-xs font-mono tracking-widest flex items-center gap-1">
                                                        <History className="w-3 h-3" /> AUDIT
                                                    </button>
                                                )}
                                                <button onClick={() => setExplainTrade(t)} data-testid={`explain-trade-${t.id}`}
                                                    title="Show the AI reasoning, sizing, factors and risks for this trade"
                                                    className="text-[#A1A1AA] hover:text-[#00FF41] text-xs font-mono tracking-widest flex items-center gap-1">
                                                    <Sparkles className="w-3 h-3" /> EXPLAIN
                                                </button>
                                                {t.status === "closed" && parseFloat(t.pnl) < 0 && (
                                                    <Link to={`/loss-lab?trade=${t.id}`}
                                                        data-testid={`postmortem-trade-${t.id}`}
                                                        title="Investigate why this losing trade failed"
                                                        className="text-[#A1A1AA] hover:text-[#FF3B30] text-xs font-mono tracking-widest flex items-center gap-1">
                                                        <FlaskConical className="w-3 h-3" /> POST-MORTEM
                                                    </Link>
                                                )}
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {auditTrade && (
                <AuditTrailModal
                    trade={auditTrade}
                    onClose={() => setAuditTrade(null)}
                />
            )}

            {explainTrade && (
                <ExplainModal
                    trade={explainTrade}
                    onClose={() => setExplainTrade(null)}
                />
            )}
        </AppLayout>
    );
}


function ExplainModal({ trade, onClose }) {
    const [data, setData] = useState(null);
    const [err, setErr] = useState("");

    useEffect(() => {
        let cancelled = false;
        api.get(`/trades/${trade.id}/explain`).then(r => {
            if (!cancelled) setData(r.data);
        }).catch(e => { if (!cancelled) setErr(formatApiError(e)); });
        return () => { cancelled = true; };
    }, [trade.id]);

    const impactClass = (impact) => {
        if (impact === "HIGH") return "text-[#FFD700] border-[#FFD700]/40 bg-[#FFD700]/10";
        if (impact === "MEDIUM") return "text-[#0099FF] border-[#0099FF]/40 bg-[#0099FF]/10";
        return "text-[#A1A1AA] border-[#1F1F1F]";
    };

    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="explain-modal"
            onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#00FF41]/40 max-w-2xl w-full max-h-[85vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2 sticky top-0 bg-[#0A0A0A]">
                    <Sparkles className="w-5 h-5 text-[#00FF41]" />
                    <div className="flex-1 min-w-0">
                        <div className="font-mono text-[10px] text-[#00FF41] tracking-widest">EXPLAINABLE AI · TRADE REASONING</div>
                        <div className="font-display font-bold text-lg tracking-tight truncate">
                            {trade.symbol} · {trade.action} · {trade.lot_size} lots
                            {trade.mt5_ticket && (
                                <span className="font-mono text-xs text-[#52525B] ml-2">#{trade.mt5_ticket}</span>
                            )}
                        </div>
                    </div>
                    <button onClick={onClose} data-testid="explain-close"
                        className="p-1.5 text-[#52525B] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {err && (
                    <div className="border-b border-[#FF3B30]/30 bg-[#FF3B30]/10 px-5 py-2 text-xs text-[#FF3B30] font-mono" data-testid="explain-error">{err}</div>
                )}

                {!data && !err && (
                    <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="explain-loading">
                        LOADING…
                    </div>
                )}

                {data && (
                    <div className="p-5 space-y-5">
                        {/* 1. ENTRY */}
                        <section data-testid="explain-section-entry">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">WHY DID I ENTER?</div>
                            <div className="border border-[#1F1F1F] bg-[#050505] p-3 space-y-1.5">
                                <Row k="Action" v={`${data.entry?.action || "—"} · conf ${data.entry?.confidence ?? "—"}`} />
                                <Row k="Strategy" v={data.entry?.strategy_summary || "—"} />
                                <Row k="Technical" v={data.entry?.technical_bias || "—"} />
                                <Row k="Macro" v={data.entry?.macro_bias || "—"} />
                                <Row k="News" v={data.entry?.news_bias || "—"} />
                            </div>
                        </section>

                        {/* 2. SIZING */}
                        <section data-testid="explain-section-sizing">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">WHY THIS SIZE?</div>
                            <div className="border border-[#1F1F1F] bg-[#050505] p-3 space-y-1.5">
                                <Row k="Final lot" v={data.sizing?.final_lot ?? "—"} />
                                <Row k="Original lot" v={data.sizing?.original_lot ?? "—"} />
                                <Row k="Vol-parity ×" v={data.sizing?.vol_parity_scale ?? "—"} />
                                <Row k="Kelly ×" v={data.sizing?.kelly_scale ?? "—"} />
                                <Row k="Blended ×" v={data.sizing?.blended_scale ?? "—"} />
                                <Row k="30d win rate" v={data.sizing?.win_rate_30d != null ? `${(data.sizing.win_rate_30d * 100).toFixed(1)}%` : "—"} />
                                <Row k="Reason" v={data.sizing?.reason || "—"} />
                            </div>
                        </section>

                        {/* 3. FACTORS */}
                        <section data-testid="explain-section-factors">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">WHAT FACTORS MATTERED?</div>
                            {(data.factors || []).length === 0 ? (
                                <div className="border border-[#1F1F1F] bg-[#050505] p-3 text-xs text-[#52525B] font-mono">
                                    No standout factors recorded.
                                </div>
                            ) : (
                                <div className="space-y-2">
                                    {(data.factors || []).map((f, i) => (
                                        <div key={i} className="border border-[#1F1F1F] bg-[#050505] p-3 flex items-start gap-3" data-testid={`explain-factor-${i}`}>
                                            <span className={`font-mono text-[9px] tracking-widest px-2 py-0.5 border ${impactClass(f.impact)}`}>{f.impact}</span>
                                            <div className="flex-1 min-w-0">
                                                <div className="font-mono text-xs text-white">{f.label}</div>
                                                <div className="text-xs text-[#A1A1AA] mt-0.5">{f.detail}</div>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </section>

                        {/* 4. RISKS */}
                        <section data-testid="explain-section-risks">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">WHAT RISKS EXIST?</div>
                            <div className="border border-[#1F1F1F] bg-[#050505] p-3 space-y-1.5">
                                <Row k="Stop loss" v={data.risks?.stop_loss ?? "—"} />
                                <Row k="Take profit" v={data.risks?.take_profit ?? "—"} />
                                <Row k="SL distance (pips)" v={data.risks?.sl_pips_risk ?? "—"} />
                                <Row k="Max loss (USD)" v={data.risks?.max_loss_usd != null ? `$${data.risks.max_loss_usd}` : "—"} />
                                <Row k="Risk % of equity" v={data.risks?.risk_pct_of_equity != null ? `${data.risks.risk_pct_of_equity}%` : "—"} />
                                <Row k="Macro gate" v={data.risks?.macro_gate_open == null ? "—" : (data.risks.macro_gate_open ? "OPEN" : "CLOSED")} />
                            </div>
                        </section>

                        <div className="font-mono text-[9px] text-[#52525B] tracking-widest text-right">
                            {data.live ? "LIVE COMPOSITION" : (data.snapshotted_at ? `SNAPSHOT · ${data.snapshotted_at}` : "")}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}

function Row({ k, v }) {
    return (
        <div className="flex items-baseline gap-3 text-xs">
            <span className="font-mono text-[10px] text-[#52525B] tracking-widest min-w-[120px]">{String(k).toUpperCase()}</span>
            <span className="text-[#E4E4E7] font-mono break-words flex-1">{v === null || v === undefined || v === "" ? "—" : v}</span>
        </div>
    );
}


function AuditTrailModal({ trade, onClose }) {
    const [events, setEvents] = useState(null);
    const [err, setErr] = useState("");

    useEffect(() => {
        let cancelled = false;
        api.get(`/trades/${trade.id}/audit`).then(r => {
            if (!cancelled) setEvents(r.data);
        }).catch(e => { if (!cancelled) setErr(formatApiError(e)); });
        return () => { cancelled = true; };
    }, [trade.id]);

    return (
        <div className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="audit-modal"
            onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-2xl w-full max-h-[80vh] overflow-y-auto"
                onClick={(e) => e.stopPropagation()}>
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2 sticky top-0 bg-[#0A0A0A]">
                    <History className="w-5 h-5 text-[#FFD700]" />
                    <div className="flex-1 min-w-0">
                        <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">TRADE AUDIT TRAIL</div>
                        <div className="font-display font-bold text-lg tracking-tight truncate">
                            {trade.symbol} · {trade.action} · {trade.lot_size} lots
                            {trade.mt5_ticket && (
                                <span className="font-mono text-xs text-[#52525B] ml-2">#{trade.mt5_ticket}</span>
                            )}
                        </div>
                    </div>
                    <button onClick={onClose} data-testid="audit-close"
                        className="p-1.5 text-[#52525B] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {err && (
                    <div className="border-b border-[#FF3B30]/30 bg-[#FF3B30]/10 px-5 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>
                )}

                {events === null && !err && (
                    <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest" data-testid="audit-loading">
                        LOADING…
                    </div>
                )}

                {events && events.events.length === 0 && (
                    <div className="p-8 text-center" data-testid="audit-empty">
                        <div className="font-display font-bold text-base mb-1">No broker events yet</div>
                        <div className="text-sm text-[#A1A1AA]">
                            This trade has no recorded deal history. Once the EA reports the next deal, it will appear here.
                        </div>
                    </div>
                )}

                {events && events.events.length > 0 && (
                    <div className="p-5 space-y-3" data-testid="audit-events">
                        {events.events.map((e, i) => (
                            <AuditEventRow key={i} e={e} />
                        ))}
                    </div>
                )}

                {events && events.broker_deal_count > 0 && (
                    <div className="px-5 py-2 border-t border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                        BROKER DEALS · {events.broker_deal_count}
                    </div>
                )}
            </div>
        </div>
    );
}

const AUDIT_KIND_STYLE = {
    stoic_open:           { color: "#00FF41", dot: "● " },
    stoic_close:          { color: "#FF3B30", dot: "● " },
    breakeven:            { color: "#FFD700", dot: "◆ " },
    partial_close:        { color: "#00FF41", dot: "◇ " },
    trail_active:         { color: "#0099FF", dot: "▲ " },
    revived:              { color: "#FFD700", dot: "↻ " },
    pending_modification: { color: "#FF9500", dot: "⏳" },
    modification_error:   { color: "#FF3B30", dot: "⚠ " },
    broker_deal:          { color: "#A1A1AA", dot: "▸ " },
};

function AuditEventRow({ e }) {
    const style = AUDIT_KIND_STYLE[e.kind] || AUDIT_KIND_STYLE.broker_deal;
    const isManual = e.kind === "broker_deal" && e.details?.is_manual;

    return (
        <div className="border-l-2 pl-3 py-2" style={{ borderColor: style.color }}>
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="font-mono text-xs tracking-wide" style={{ color: style.color }}>
                    {style.dot}{e.label}
                    {isManual && (
                        <span className="ml-2 font-mono text-[9px] tracking-widest text-[#FFD700] border border-[#FFD700]/40 bg-[#FFD700]/10 px-1.5 py-0.5">
                            MANUAL
                        </span>
                    )}
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest whitespace-nowrap">
                    {fmtDateTime(e.at) || "—"}
                </div>
            </div>
            {e.details && (
                <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[10px] text-[#A1A1AA]">
                    {Object.entries(e.details).filter(([_k, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => (
                        <div key={k} className="truncate">
                            <span className="text-[#52525B]">{k.toUpperCase()}</span>{" "}
                            <span className="text-white">{typeof v === "boolean" ? (v ? "true" : "false") : String(v)}</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}


function ClearTradesMenu({ trades, onCleared }) {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const closedCount = trades.filter(t => t.status === "closed").length;
    const cancelledCount = trades.filter(t => t.status === "cancelled").length;
    const failedCount = trades.filter(t => t.status === "failed").length;
    const deletableTotal = closedCount + cancelledCount + failedCount;

    const clear = async (scope, label, days) => {
        const noun = scope === "closed" ? `${closedCount} closed trade(s)`
                   : scope === "cancelled" ? `${cancelledCount} cancelled trade(s)`
                   : scope === "failed" ? `${failedCount} failed trade(s)`
                   : days ? `trades older than ${days} day(s)`
                   : `${deletableTotal} closed/cancelled/failed trade(s)`;
        if (!window.confirm(`Delete ${noun}? Open positions are protected.\n\nThis cannot be undone.`)) return;
        setBusy(true);
        try {
            const params = new URLSearchParams();
            if (scope) params.set("scope", scope);
            if (days) params.set("older_than_days", String(days));
            const { data } = await api.delete(`/trades?${params.toString()}`);
            toast.success(`Cleared ${data.deleted} trade${data.deleted === 1 ? "" : "s"} (${label})`);
            setOpen(false);
            await onCleared();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setBusy(false); }
    };

    return (
        <div className="relative" data-testid="clear-trades-menu">
            <button onClick={() => setOpen(!open)} disabled={busy || deletableTotal === 0}
                data-testid="clear-trades-toggle"
                className="flex items-center gap-2 px-3 py-2 border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 disabled:opacity-40 disabled:cursor-not-allowed font-medium text-xs tracking-widest transition-colors duration-150">
                <Trash className="w-3.5 h-3.5" /> CLEAR <ChevronDown className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} />
            </button>
            {open && (
                <div className="absolute right-0 top-full mt-1 z-30 w-80 border border-[#1F1F1F] bg-[#0A0A0A] shadow-2xl"
                    data-testid="clear-trades-dropdown">
                    <div className="px-3 py-2 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">DELETABLE TRADES</div>
                        <div className="font-mono text-[10px] text-[#A1A1AA] mt-0.5">
                            {closedCount} closed · {cancelledCount} cancelled · {failedCount} failed
                        </div>
                        <div className="font-mono text-[10px] text-[#00FF41] mt-1">
                            ✓ Open positions are PROTECTED — never deleted
                        </div>
                    </div>
                    <ClearTradeOption icon={Trash} label="Clear closed trades only" sub={`${closedCount} settled position${closedCount === 1 ? "" : "s"}`}
                        onClick={() => clear("closed", "closed")} testid="clear-closed" disabled={closedCount === 0} />
                    <ClearTradeOption icon={Trash} label="Clear cancelled / failed only" sub={`${cancelledCount + failedCount} never-filled`}
                        onClick={() => clear("cancelled", "cancelled")} testid="clear-cancelled" disabled={cancelledCount === 0} />
                    <ClearTradeOption icon={Trash} label="Clear trades > 30 days old" sub="Keep last month only"
                        onClick={() => clear(null, "30d", 30)} testid="clear-30d" />
                    <ClearTradeOption icon={Trash} label="Clear trades > 7 days old" sub="Keep the recent week"
                        onClick={() => clear(null, "7d", 7)} testid="clear-7d" />
                    <ClearTradeOption icon={X} label="Clear ALL closed history" sub="Wipes the full closed-trade log" danger
                        onClick={() => clear("all", "all closed")} testid="clear-all" disabled={deletableTotal === 0} />
                </div>
            )}
        </div>
    );
}

function ClearTradeOption({ icon: Icon, label, sub, onClick, testid, danger, disabled }) {
    return (
        <button type="button" onClick={onClick} disabled={disabled}
            data-testid={testid}
            className={`w-full flex items-center gap-3 px-3 py-2 text-left border-b border-[#1F1F1F] last:border-b-0 transition-colors disabled:opacity-30 disabled:cursor-not-allowed ${
                danger ? "hover:bg-[#FF3B30]/10" : "hover:bg-[#121212]"
            }`}>
            <Icon className={`w-3.5 h-3.5 ${danger ? "text-[#FF3B30]" : "text-[#A1A1AA]"}`} />
            <div className="flex-1">
                <div className={`text-xs font-medium ${danger ? "text-[#FF3B30]" : "text-white"}`}>{label}</div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-wide mt-0.5">{sub}</div>
            </div>
        </button>
    );
}
