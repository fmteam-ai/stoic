import { useEffect, useState } from "react";
import api from "@/lib/api";

const fmtUsd = (v) => (v == null ? "—" : `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);
const fmtPips = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v}`);

function Stat({ label, value, tone = "#E4E4E7", testid }) {
    return (
        <div className="min-w-0" data-testid={testid}>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</div>
            <div className="font-mono text-sm" style={{ color: tone }}>{value}</div>
        </div>
    );
}

export const ExitRuleScoreboard = ({ days = 30 }) => {
    const [data, setData] = useState(null);
    useEffect(() => {
        api.get(`/scalp/exit-rules?days=${days}`).then(r => setData(r.data)).catch(() => setData({ error: true }));
    }, [days]);
    if (!data) return null;
    if (data.error) return <div className="text-xs text-[#FF3B30]" data-testid="exit-rules-error">Exit rule scoreboard unavailable.</div>;
    const be = data.rules.breakeven_0_5r, ts = data.rules.time_stop_90m;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 mb-6" data-testid="exit-rule-scoreboard">
            <div className="flex flex-wrap items-baseline gap-3 mb-3">
                <h3 className="text-base md:text-lg text-[#E4E4E7]">Exit Rule Scoreboard</h3>
                <span className="font-mono text-[10px] tracking-widest text-[#52525B]">HF SCALP ENGINES · LAST {data.days} DAYS</span>
                <span className="ml-auto font-mono text-xs" style={{ color: data.avoided_loss_usd > 0 ? "#00FF41" : "#A1A1AA" }}
                    data-testid="exit-rules-avoided">SPARED ≈ {fmtUsd(data.avoided_loss_usd)}</span>
            </div>
            <div className="grid sm:grid-cols-2 gap-4">
                <div className="border border-[#1F1F1F] p-3" data-testid="exit-rule-breakeven">
                    <div className="font-mono text-[10px] tracking-widest text-[#FFD700] mb-2">BREAKEVEN AT +0.5R</div>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                        <Stat label="FIRED" value={be.trades} testid="be-fired" />
                        <Stat label="STOPPED AT BE" value={be.be_stop} tone="#FFB000" />
                        <Stat label="WENT TO TARGET" value={be.target} tone="#00FF41" />
                        <Stat label="REALISED P&L" value={fmtUsd(be.pnl_usd)} tone={be.pnl_usd >= 0 ? "#00FF41" : "#FF3B30"} />
                    </div>
                </div>
                <div className="border border-[#1F1F1F] p-3" data-testid="exit-rule-timestop">
                    <div className="font-mono text-[10px] tracking-widest text-[#FFD700] mb-2">TIME-STOP · 90 MIN BELOW +0.5R</div>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                        <Stat label="FIRED" value={ts.trades} testid="ts-fired" />
                        <Stat label="CLOSED RED" value={ts.closed_negative} tone="#FF3B30" />
                        <Stat label="CLOSED GREEN" value={ts.closed_positive} tone="#00FF41" />
                        <Stat label="REALISED P&L" value={fmtUsd(ts.pnl_usd)} tone={ts.pnl_usd >= 0 ? "#00FF41" : "#FF3B30"} />
                    </div>
                </div>
            </div>
            <div className="mt-3 text-xs text-[#A1A1AA]" data-testid="exit-rules-lesson">{data.lesson}</div>
            <div className="mt-1 font-mono text-[9px] text-[#3F3F46]">{data.note} · {data.unaffected.trades} scalp trade(s) closed without either rule ({data.unaffected.stop_loss} SL / {data.unaffected.take_profit} TP, {fmtUsd(data.unaffected.pnl_usd)})</div>
        </div>
    );
};

function SymbolCard({ row }) {
    const g = row.gate || {};
    const th = g.thresholds || {};
    const pct = th.min_labeled ? Math.min(100, Math.round(100 * (g.labeled || 0) / th.min_labeled)) : 0;
    const expTone = g.expectancy_pips == null ? "#A1A1AA" : g.expectancy_pips > (th.min_expectancy_pips ?? 0) ? "#00FF41" : "#FF3B30";
    return (
        <div className="border border-[#1F1F1F] p-3" data-testid={`expectancy-${row.symbol}`}>
            <div className="flex items-center gap-2 mb-2">
                <span className="font-mono text-[#E4E4E7]">{row.symbol}</span>
                <span className={`px-2 py-0.5 text-[10px] font-mono tracking-widest border ${g.eligible ? "border-[#00FF41]/50 text-[#00FF41]" : "border-[#FFB000]/50 text-[#FFB000]"}`}
                    data-testid={`expectancy-${row.symbol}-gate`}>
                    {g.eligible ? "LIVE GATE OPEN" : "SHADOW UNTIL PROVEN"}
                </span>
            </div>
            <div className="h-1.5 bg-[#141414] mb-1">
                <div className="h-1.5 bg-[#FFD700]" style={{ width: `${pct}%` }} />
            </div>
            <div className="font-mono text-[10px] text-[#52525B] mb-3" data-testid={`expectancy-${row.symbol}-samples`}>
                {g.labeled ?? 0} / {th.min_labeled ?? "—"} labeled shadow samples
            </div>
            <div className="grid grid-cols-3 gap-3 mb-3">
                <Stat label="SHADOW EXPECTANCY" value={`${fmtPips(g.expectancy_pips)} pips`} tone={expTone} testid={`expectancy-${row.symbol}-exp`} />
                <Stat label="PROFIT FACTOR" value={g.profit_factor == null ? "—" : g.profit_factor === Infinity ? "∞" : g.profit_factor} />
                <Stat label="WIN RATE" value={row.shadow?.win_rate == null ? "—" : `${Math.round(row.shadow.win_rate * 100)}%`} />
            </div>
            <div className="border-t border-[#1F1F1F] pt-2 grid grid-cols-3 gap-3" data-testid={`expectancy-${row.symbol}-live`}>
                <Stat label="LIVE-MODE TRADES" value={row.live?.labeled ?? 0} tone="#A1A1AA" />
                <Stat label="LIVE EXPECTANCY" value={`${fmtPips(row.live?.expectancy_pips)} pips`}
                    tone={row.live?.expectancy_pips == null ? "#A1A1AA" : row.live.expectancy_pips > 0 ? "#00FF41" : "#FF3B30"} />
                <Stat label="LIVE NET" value={`${fmtPips(row.live?.net_pips)} pips`} tone="#A1A1AA" />
            </div>
            {!g.eligible && (g.reasons || []).length > 0 && (
                <ul className="mt-2 text-[11px] text-[#A1A1AA] list-disc pl-4" data-testid={`expectancy-${row.symbol}-reasons`}>
                    {g.reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
            )}
        </div>
    );
}

export const ShadowExpectancyReport = () => {
    const [data, setData] = useState(null);
    useEffect(() => {
        api.get("/scalp/expectancy").then(r => setData(r.data)).catch(() => setData({ error: true }));
    }, []);
    if (!data) return null;
    if (data.error) return <div className="text-xs text-[#FF3B30]" data-testid="expectancy-error">Expectancy report unavailable.</div>;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 mb-6" data-testid="shadow-expectancy-report">
            <div className="flex flex-wrap items-baseline gap-3 mb-3">
                <h3 className="text-base md:text-lg text-[#E4E4E7]">Shadow Expectancy Report</h3>
                <span className="font-mono text-[10px] tracking-widest text-[#52525B]">NET OF SPREAD + COMMISSION · PER SYMBOL</span>
            </div>
            <div className="grid md:grid-cols-2 gap-4">
                {(data.symbols || []).map(row => <SymbolCard key={row.symbol} row={row} />)}
            </div>
        </div>
    );
};
