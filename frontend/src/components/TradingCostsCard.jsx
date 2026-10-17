import { useCallback, useEffect, useState } from "react";
import { Coins, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const fmt = (v, d = 2, suffix = "") => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}${suffix}`);
const MODE = {
    shadow: "border-[#FFB000]/60 text-[#FFB000]", enforce: "border-[#00FF41]/60 text-[#00FF41]", off: "border-[#52525B] text-[#71717A]",
};

// v1.60.63 #85 — per-broker trading costs (spread by session, entry/exit slippage, cost in R and % of TP, worst trades)
// and the smart-spread-gate shadow scoreboard. Costs are measured per broker and per symbol; gold first.
export function TradingCostsCard() {
    const [d, setD] = useState(null);
    const [days, setDays] = useState(7);
    const [loading, setLoading] = useState(false);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try { setD((await api.get(`/diagnostic/trade-costs?days=${days}`)).data); }
        catch (e) { setD(null); if ([401, 403].includes(e?.response?.status)) setHidden(true); }
        finally { setLoading(false); }
    }, [days]);

    useEffect(() => { if (!hidden) load(); }, [load, hidden]);

    if (hidden) return null;
    const gate = d?.gate || {};
    const sb = d?.all?.shadow_scoreboard;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="trading-costs-card">
            <div className="flex flex-wrap items-center gap-2 mb-3">
                <Coins className="w-3.5 h-3.5 text-[#F5C542]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">Trading costs · spread gate</span>
                {gate.mode && <span className={`px-1.5 py-0.5 border font-mono text-[10px] tracking-widest uppercase ${MODE[gate.mode] || MODE.off}`} data-testid="spread-gate-mode-badge">gate {gate.mode}</span>}
                <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="ml-auto bg-[#050505] border border-[#1F1F1F] font-mono text-[10px] px-1 py-0.5" data-testid="trading-costs-days">
                    {[7, 14, 30].map((n) => <option key={n} value={n}>{n} d</option>)}
                </select>
                <button onClick={load} disabled={loading} className="text-[#52525B] hover:text-white" data-testid="trading-costs-refresh"><RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /></button>
            </div>
            {!d ? (
                <div className="font-mono text-[10px] text-[#52525B]">{loading ? "loading…" : "trading costs unavailable"}</div>
            ) : (
                <div className="space-y-3 font-mono text-[10px]">
                    <div className="text-[#E4E4E7]" data-testid="trading-costs-all">
                        {d.all?.trades} closed trades · avg cost <b>{fmt(d.all?.avg_total_cost_R, 3)} R</b> · {fmt(d.all?.avg_cost_pct_of_tp, 1, "%")} of TP ·
                        entry slip {fmt(d.all?.avg_entry_slippage_pips, 2, "p")} · exit slip {fmt(d.all?.avg_exit_slippage_pips, 2, "p")} · total ${fmt(d.all?.total_cost_usd)}
                    </div>
                    {(d.brokers || []).filter((b) => b.trades > 0 || Object.keys(b.spread_by_session || {}).length).map((b) => (
                        <div key={b.account_id} className="border border-[#1F1F1F] p-2" data-testid={`trading-costs-broker-${b.account_id}`}>
                            <div className="text-[#F5C542] mb-1">{b.label} <span className="text-[#52525B]">· {b.trades} trades · cost {fmt(b.avg_total_cost_R, 3)} R · {fmt(b.avg_cost_pct_of_tp, 1, "%")} of TP · slip in/out {fmt(b.avg_entry_slippage_pips, 2)}/{fmt(b.avg_exit_slippage_pips, 2)}p</span></div>
                            {Object.entries(b.spread_by_session || {}).map(([sym, ses]) => (
                                <div key={sym} className="text-[#71717A]">{sym} spread · {Object.entries(ses).map(([s, v]) => `${s} ${fmt(v.median, 1, "p")}${v.n ? ` (n${v.n})` : ""}`).join(" · ")}</div>
                            ))}
                            {(b.worst_trades || []).length > 0 && (
                                <div className="text-[#71717A] mt-1">worst: {b.worst_trades.map((w) => `${w.symbol} ${w.action} ${fmt(w.total_cost_R, 2)}R (${fmt(w.cost_pct_of_tp, 0, "%")} TP, ${w.exit_kind || "?"})`).join(" · ")}</div>
                            )}
                        </div>
                    ))}
                    {sb && (
                        <div className="text-[#A1A1AA]" data-testid="spread-gate-scoreboard">
                            gate shadow scoreboard: would-block {sb.would_block.trades} trades · WR {fmt(sb.would_block.win_rate, 0, "%")} · net {fmt(sb.would_block.net_R, 2)} R
                            {" vs "}passed {sb.passed.trades} · WR {fmt(sb.passed.win_rate, 0, "%")} · net {fmt(sb.passed.net_R, 2)} R
                            <span className="text-[#52525B]"> · rules median {sb.rules?.spread_vs_hour_median} / cap {sb.rules?.spread_vs_static_cap} / cost% {sb.rules?.cost_pct_of_tp}</span>
                        </div>
                    )}
                    <div className="text-[#52525B]">gate: {gate.mult}× hour median (n ≥ {gate.hour_min_samples}, else static cap) · max cost {gate.max_cost_pct_of_tp}% of TP · history {gate.history_days} d</div>
                </div>
            )}
        </div>
    );
}
