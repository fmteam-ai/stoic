import { useEffect, useState, useCallback } from "react";
import { Trophy, RefreshCw, Layers, TrendingUp, TrendingDown, Minus, Sparkles } from "lucide-react";
import api, { formatApiError } from "@/lib/api";

/**
 * Side-by-side performance comparison for every MT5 account.
 *
 * Surfaces:
 *  - 30-day P&L (the metric users actually care about)
 *  - Lifetime P&L · win rate · profit factor
 *  - Open + pending trade counts
 *  - Active preset / risk level / per-account lot cap
 *  - LEADER badge on whichever account has the highest 30d P&L (only when >$0)
 *
 * Refreshes on `refreshSignal` (parent passes the last WS event timestamp) so
 * the widget moves in lockstep with the rest of the dashboard.
 */
export function PerAccountComparison({ refreshSignal }) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");
    const [refreshing, setRefreshing] = useState(false);

    const fetchData = useCallback(async () => {
        try {
            const { data } = await api.get("/analytics/by-account");
            setData(data);
            setErr("");
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, []);

    useEffect(() => { fetchData(); }, [fetchData, refreshSignal]);

    const onRefresh = () => { setRefreshing(true); fetchData(); };

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 h-32 animate-pulse"
                data-testid="per-account-comparison-loading" />
        );
    }

    const accounts = data?.accounts || [];
    if (accounts.length < 2) {
        // Single-account users get no value from a comparison widget.
        return null;
    }

    // Find leader = highest 30d P&L (with a positive value). Falls back to
    // lifetime total_pnl when nobody has booked anything in the last 30d.
    const pnl30Max = Math.max(...accounts.map(a => a.pnl_30d));
    const leaderId = pnl30Max > 0
        ? accounts.find(a => a.pnl_30d === pnl30Max)?.account_id
        : null;

    return (
        <div className="border border-[#FFD700]/30 bg-[#0A0A0A]" data-testid="per-account-comparison">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Trophy className="w-4 h-4 text-[#FFD700]" />
                <div className="flex-1 min-w-0">
                    <div className="font-mono text-[10px] text-[#FFD700] tracking-widest">PER-ACCOUNT COMPARISON · A/B PERFORMANCE</div>
                    <div className="font-display font-bold text-lg tracking-tight">Which account is actually winning?</div>
                </div>
                <button onClick={onRefresh}
                    disabled={refreshing}
                    data-testid="per-account-refresh"
                    title="Refresh per-account stats"
                    className="px-2.5 py-1.5 text-[10px] font-mono tracking-widest border border-[#1F1F1F] hover:border-[#FFD700]/40 disabled:opacity-50 text-[#A1A1AA] flex items-center gap-1.5 transition-colors">
                    <RefreshCw className={`w-3 h-3 ${refreshing ? "animate-spin" : ""}`} />
                    {refreshing ? "SYNC…" : "REFRESH"}
                </button>
            </div>

            {err && (
                <div className="border-b border-[#FF3B30]/30 bg-[#FF3B30]/10 px-5 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 p-5" data-testid="per-account-grid">
                {accounts.map(a => (
                    <AccountCard
                        key={a.account_id}
                        acc={a}
                        isLeader={a.account_id === leaderId}
                        testid={`per-account-card-${a.account_id}`}
                    />
                ))}
            </div>
        </div>
    );
}

function AccountCard({ acc, isLeader, testid }) {
    const pnl30 = acc.pnl_30d;
    const pnl30Pos = pnl30 > 0;
    const pnl30Neg = pnl30 < 0;
    const pnlTotal = acc.total_pnl;
    const pnlTotalPos = pnlTotal > 0;
    const pnlTotalNeg = pnlTotal < 0;

    const PnlIcon = pnl30Pos ? TrendingUp : pnl30Neg ? TrendingDown : Minus;
    const pnlColor = pnl30Pos ? "text-[#00FF41]" : pnl30Neg ? "text-[#FF3B30]" : "text-[#A1A1AA]";

    return (
        <div
            data-testid={testid}
            className={`relative p-4 border bg-[#050505] transition-colors duration-150 ${
                isLeader ? "border-[#FFD700] bg-[#FFD700]/[0.04]" : "border-[#1F1F1F]"
            }`}>
            {isLeader && (
                <div className="absolute -top-2 left-3 px-1.5 py-0.5 bg-[#FFD700] text-black font-mono text-[9px] tracking-widest font-bold flex items-center gap-1"
                    data-testid={`per-account-leader-${acc.account_id}`}>
                    <Sparkles className="w-3 h-3" /> LEADER · 30D
                </div>
            )}

            <div className="flex items-center justify-between mb-2">
                <div className="min-w-0">
                    <div className="font-display font-bold text-sm tracking-tight truncate">{acc.label}</div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">
                        {acc.mode.toUpperCase()} · {acc.broker}
                    </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                    <span className={`font-mono text-[9px] tracking-widest px-1.5 py-0.5 border ${
                        acc.bot_active
                            ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10"
                            : "border-[#1F1F1F] text-[#52525B]"
                    }`}>
                        {acc.bot_active ? "● BOT ON" : "○ BOT OFF"}
                    </span>
                </div>
            </div>

            {/* Headline metric: 30-day P&L */}
            <div className="border-y border-[#1F1F1F] py-2 my-2 flex items-center justify-between">
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">30-DAY P&amp;L</div>
                    <div className={`font-display font-bold text-lg tracking-tight tabular-nums ${pnlColor}`}>
                        {pnl30 >= 0 ? "+" : "-"}${Math.abs(pnl30).toFixed(2)}
                    </div>
                </div>
                <PnlIcon className={`w-6 h-6 ${pnlColor}`} />
            </div>

            <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 font-mono text-[10px]">
                <Metric label="LIFETIME P&L"
                    value={`${pnlTotal >= 0 ? "+" : "-"}$${Math.abs(pnlTotal).toFixed(2)}`}
                    valueClass={pnlTotalPos ? "text-[#00FF41]" : pnlTotalNeg ? "text-[#FF3B30]" : "text-white"} />
                <Metric label="WIN RATE"
                    value={`${acc.win_rate}%`}
                    valueClass={acc.win_rate >= 50 ? "text-[#00FF41]" : acc.win_rate > 0 ? "text-[#FFB000]" : "text-[#52525B]"} />
                <Metric label="TRADES" value={`${acc.closed_trades} closed`} />
                <Metric label="OPEN / PEND" value={`${acc.open_trades} / ${acc.pending_trades}`}
                    valueClass={acc.open_trades > 0 ? "text-[#FFD700]" : "text-white"} />
                <Metric label="PROFIT FACTOR"
                    value={acc.profit_factor !== null ? acc.profit_factor.toFixed(2) : "—"}
                    valueClass={
                        acc.profit_factor === null ? "text-[#52525B]"
                        : acc.profit_factor >= 1.5 ? "text-[#00FF41]"
                        : acc.profit_factor >= 1.0 ? "text-[#FFB000]"
                        : "text-[#FF3B30]"
                    } />
                <Metric label="AVG / TRADE"
                    value={acc.closed_trades > 0
                        ? `${acc.avg_pnl >= 0 ? "+" : "-"}$${Math.abs(acc.avg_pnl).toFixed(2)}`
                        : "—"}
                    valueClass={acc.avg_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
            </div>

            {/* Bot config footer */}
            <div className="mt-3 pt-2 border-t border-[#1F1F1F] flex items-center justify-between gap-2 font-mono text-[9px] text-[#52525B] tracking-widest">
                <div className="flex items-center gap-1.5 min-w-0">
                    <Layers className="w-3 h-3 shrink-0" />
                    <span className="truncate">
                        {acc.has_override ? "OWN CFG" : "DEFAULT"}
                        {" · "}
                        <span className="text-[#A1A1AA]">{(acc.risk_level || "—").toUpperCase()}</span>
                        {acc.active_preset && (
                            <>{" · "}<span className="text-[#FFD700]">{acc.active_preset.replace("custom:", "").toUpperCase()}</span></>
                        )}
                    </span>
                </div>
                {acc.max_lot_size > 0 && (
                    <span className="shrink-0 text-[#FFD700]">CAP {acc.max_lot_size}</span>
                )}
            </div>
        </div>
    );
}

function Metric({ label, value, valueClass = "text-white" }) {
    return (
        <div className="flex items-center justify-between gap-1">
            <span className="text-[#52525B]">{label}</span>
            <span className={`tabular-nums ${valueClass}`}>{value}</span>
        </div>
    );
}
