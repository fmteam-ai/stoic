import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { RefreshCw as ArrowsClockwise, X } from "lucide-react";

const STATUS_STYLE = {
    pending: "border-[#FFB000]/40 text-[#FFB000]",
    open: "border-[#00FF41]/40 text-[#00FF41]",
    closed: "border-[#1F1F1F] text-[#A1A1AA]",
    cancelled: "border-[#1F1F1F] text-[#52525B]",
    failed: "border-[#FF3B30]/40 text-[#FF3B30]",
};

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
                    <div className="grid grid-cols-2 md:grid-cols-5 gap-3" data-testid="trades-stats">
                        <Stat label="OPEN" value={stats.open_trades} />
                        <Stat label="TOTAL" value={stats.total_trades} />
                        <Stat label="WIN RATE" value={`${stats.win_rate}%`} accent="text-[#00FF41]" />
                        <Stat label="WINS / LOSSES" value={`${stats.wins} / ${stats.losses}`} />
                        <Stat label="TOTAL P&L" value={`${stats.total_pnl >= 0 ? "+" : ""}${stats.total_pnl}`} accent={stats.total_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
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
                                    {["SYMBOL", "SIDE", "LOTS", "ENTRY", "SL", "TP", "EXIT", "P&L", "STATUS", ""].map(h => (
                                        <th key={h} className="px-3 py-2 text-left font-mono text-[10px] text-[#52525B] tracking-widest whitespace-nowrap">{h}</th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody>
                                {trades.map(t => (
                                    <tr key={t.id} className="border-b border-[#1F1F1F] hover:bg-[#121212] transition-colors" data-testid={`trade-row-${t.id}`}>
                                        <td className="px-3 py-2 font-mono">{t.symbol}</td>
                                        <td className={`px-3 py-2 font-mono ${t.action === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{t.action}</td>
                                        <td className="px-3 py-2 font-mono">{t.lot_size}</td>
                                        <td className="px-3 py-2 font-mono">{t.entry_price}</td>
                                        <td className="px-3 py-2 font-mono text-[#FF3B30]">{t.stop_loss}</td>
                                        <td className="px-3 py-2 font-mono text-[#00FF41]">{t.take_profit}</td>
                                        <td className="px-3 py-2 font-mono">{t.exit_price ?? "—"}</td>
                                        <td className={`px-3 py-2 font-mono ${(t.pnl ?? 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                            {t.pnl ? (t.pnl >= 0 ? "+" : "") + t.pnl.toFixed(2) : "—"}
                                        </td>
                                        <td className="px-3 py-2">
                                            <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border inline-block ${STATUS_STYLE[t.status] || "border-[#1F1F1F]"}`}>
                                                {t.status?.toUpperCase()}
                                            </span>
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
