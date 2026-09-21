import { Lock, ShieldCheck, AlertTriangle, OctagonAlert, Pause } from "lucide-react";
import { KpiTile } from "@/components/PammPanels";

const box = "border border-[#1F1F1F] bg-[#0A0A0A]";
const label = "font-mono text-[10px] tracking-widest text-[#52525B]";

export const fmtMoney = (v, ccy = "USD") => v == null ? "—" : `${v < 0 ? "-" : ""}${ccy === "USD" ? "$" : ""}${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${ccy !== "USD" ? ` ${ccy}` : ""}`;
export const fmtPct = (v) => v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
export const toneOf = (v) => v > 0 ? "text-[#00FF41]" : v < 0 ? "text-[#FF3B30]" : "text-white";

export function MonitorOnlyBanner({ reason }) {
    return (
        <div className="border border-[#FFB000]/40 bg-[#FFB000]/5 px-4 py-3 flex items-start gap-3" data-testid="investor-monitor-only-banner">
            <Lock className="w-4 h-4 text-[#FFB000] mt-0.5 shrink-0" />
            <div>
                <div className="font-mono text-[10px] tracking-widest text-[#FFB000]">EXECUTION AUTHORITY · LOCKED · MONITOR ONLY</div>
                <div className="text-xs text-[#A1A1AA] mt-1">{reason || "STOIC never places orders on investor accounts. This view mirrors the master program — nothing here can touch capital."}</div>
            </div>
        </div>
    );
}

const OP_STYLE = {
    running: { cls: "border-[#00FF41]/40 text-[#00FF41]", Icon: ShieldCheck, text: "RUNNING" },
    new_trades_paused: { cls: "border-[#FFB000]/40 text-[#FFB000]", Icon: Pause, text: "NEW TRADES PAUSED" },
    broker_uncertain: { cls: "border-[#FFB000]/40 text-[#FFB000]", Icon: AlertTriangle, text: "BROKER UNCERTAIN" },
    locked: { cls: "border-[#FF3B30]/40 text-[#FF3B30]", Icon: OctagonAlert, text: "LOCKED" },
};

export function ProgramStatusStrip({ program, tradingAllowed, blockReason }) {
    const st = OP_STYLE[program.op_state] || { cls: "border-[#FF3B30]/40 text-[#FF3B30]", Icon: OctagonAlert, text: (program.op_state || "").replace(/_/g, " ").toUpperCase() };
    return (
        <div className="flex flex-wrap items-center gap-2" data-testid="investor-program-status">
            <span className={`px-2 py-1 border font-mono text-[10px] tracking-widest flex items-center gap-1.5 ${st.cls}`} data-testid="investor-op-state">
                <st.Icon className="w-3 h-3" /> {st.text}
            </span>
            {program.emergency_stop && <span className="px-2 py-1 border border-[#FF3B30]/40 text-[#FF3B30] font-mono text-[10px] tracking-widest" data-testid="investor-estop">EMERGENCY STOP</span>}
            {program.risk_breach && <span className="px-2 py-1 border border-[#FF3B30]/40 text-[#FF3B30] font-mono text-[10px] tracking-widest" data-testid="investor-risk-breach">RISK BREACH</span>}
            <span className={`px-2 py-1 border font-mono text-[10px] tracking-widest ${tradingAllowed ? "border-[#1F1F1F] text-[#A1A1AA]" : "border-[#FFB000]/40 text-[#FFB000]"}`} data-testid="investor-trading-allowed">
                TRADING · {tradingAllowed ? "ALLOWED" : `BLOCKED${blockReason ? ` · ${String(blockReason).toUpperCase()}` : ""}`}
            </span>
            {program.position_truth_status && <span className="px-2 py-1 border border-[#1F1F1F] text-[#52525B] font-mono text-[10px] tracking-widest">POSITION TRUTH · {program.position_truth_status.toUpperCase()}</span>}
        </div>
    );
}

export function MasterKpis({ program, performance }) {
    const ccy = program.currency || "USD";
    return (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3" data-testid="investor-master-kpis">
            <KpiTile title="MASTER NAV" value={fmtMoney(program.last_nav?.nav ?? program.aum, ccy)} sub={program.last_nav?.at ? `broker · ${program.last_nav.at.slice(0, 16).replace("T", " ")}` : "awaiting broker NAV"} testid="investor-kpi-nav" />
            <KpiTile title="TOTAL RETURN" value={fmtPct(performance?.total_return_pct)} tone={toneOf(performance?.total_return_pct)} sub={performance?.points ? `${performance.points} NAV points` : "no NAV history yet"} testid="investor-kpi-return" />
            <KpiTile title="MAX DRAWDOWN" value={performance?.max_drawdown_pct != null ? `-${performance.max_drawdown_pct.toFixed(2)}%` : "—"} tone="text-[#FF3B30]" sub="peak-to-trough on broker NAV" testid="investor-kpi-dd" />
            <KpiTile title="INVESTORS" value={program.investor_count ?? "—"} sub={`manager fee ${program.manager_fee_pct ?? "—"}%`} testid="investor-kpi-investors" />
        </div>
    );
}

export function MyShareCard({ share, program, linkedAccounts }) {
    const ccy = program.currency || "USD";
    const brokerEq = (linkedAccounts || []).find(a => a.equity != null);
    return (
        <div className={`${box} p-4 space-y-3`} data-testid="investor-share-card">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className={label}>MY MIRRORED SHARE</div>
                <span className="px-1.5 py-0.5 border border-[#FFB000]/40 text-[#FFB000] font-mono text-[9px] tracking-widest" data-testid="investor-share-estimated">ESTIMATED</span>
            </div>
            <div className="grid grid-cols-2 gap-3">
                <div><div className={label}>CONTRIBUTED</div><div className="font-display font-bold text-lg text-white" data-testid="investor-share-contributed">{fmtMoney(share.contributed, ccy)}</div></div>
                <div><div className={label}>SHARE OF PROGRAM</div><div className="font-display font-bold text-lg text-white" data-testid="investor-share-pct">{share.share_pct != null ? `${share.share_pct.toFixed(3)}%` : "—"}</div></div>
                <div><div className={label}>EST. VALUE</div><div className="font-display font-bold text-lg text-white" data-testid="investor-share-value">{fmtMoney(share.estimated_value, ccy)}</div></div>
                <div><div className={label}>MIRRORED P&L</div><div className={`font-display font-bold text-lg ${toneOf(share.mirrored_pnl)}`} data-testid="investor-share-pnl">{fmtMoney(share.mirrored_pnl, ccy)} <span className="text-[10px] font-mono">{share.mirrored_return_pct != null ? `(${fmtPct(share.mirrored_return_pct)})` : ""}</span></div></div>
            </div>
            {brokerEq && (
                <div className="border-t border-[#141414] pt-2 font-mono text-[10px] text-[#A1A1AA]" data-testid="investor-broker-equity">
                    BROKER-REPORTED EQUITY · <span className="text-white">{fmtMoney(brokerEq.equity, ccy)}</span> <span className="text-[#52525B]">({brokerEq.broker} #{brokerEq.account_number}{brokerEq.last_heartbeat ? ` · ${brokerEq.last_heartbeat.slice(0, 16).replace("T", " ")}` : ""})</span>
                </div>
            )}
            <div className="font-mono text-[9px] text-[#52525B] leading-relaxed">
                {share.allocations ? `${share.allocations} allocation${share.allocations > 1 ? "s" : ""} · basis: ${share.basis}. Your broker's allocation statement is authoritative.` : "No broker allocation is linked to this program for your login yet — figures appear once an allocation is mirrored."}
            </div>
        </div>
    );
}

export function MasterTradesTable({ trades, openPositions, currency }) {
    return (
        <div className={`${box} p-4`} data-testid="investor-master-trades">
            <div className="flex items-center justify-between mb-2">
                <div className={label}>MASTER TRADE HISTORY · RECENT</div>
                <span className="font-mono text-[10px] text-[#A1A1AA]" data-testid="investor-open-positions">OPEN · {openPositions ?? 0}</span>
            </div>
            {!trades?.length ? (
                <div className="text-xs font-mono text-[#52525B] py-6 text-center">No closed master trades attributed to this program yet.</div>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full font-mono text-[10px]">
                        <thead><tr className="text-[#52525B] text-left"><th className="py-1 pr-3">CLOSED</th><th className="pr-3">SYMBOL</th><th className="pr-3">SIDE</th><th className="pr-3 text-right">LOTS</th><th className="pr-3 text-right">ENTRY</th><th className="pr-3 text-right">EXIT</th><th className="text-right">P&L</th></tr></thead>
                        <tbody>
                            {trades.map((t, i) => (
                                <tr key={i} className="border-t border-[#141414] text-[#A1A1AA]" data-testid={`investor-trade-row-${i}`}>
                                    <td className="py-1.5 pr-3 whitespace-nowrap">{(t.closed_at || "").slice(5, 16).replace("T", " ")}</td>
                                    <td className="pr-3 text-white">{t.symbol}</td>
                                    <td className={`pr-3 ${t.action === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{t.action}</td>
                                    <td className="pr-3 text-right">{t.lot_size}</td>
                                    <td className="pr-3 text-right">{t.entry_price}</td>
                                    <td className="pr-3 text-right">{t.exit_price ?? "—"}</td>
                                    <td className={`text-right ${toneOf(t.pnl)}`}>{fmtMoney(t.pnl, currency)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    );
}

export function AllocationsList({ allocations, currency }) {
    if (!allocations?.length) return null;
    return (
        <div className={`${box} p-4`} data-testid="investor-allocations">
            <div className={`${label} mb-2`}>MY ALLOCATIONS · BROKER MIRROR</div>
            <div className="space-y-1">
                {allocations.map((a, i) => (
                    <div key={i} className="flex items-center justify-between font-mono text-[10px] border-t border-[#141414] pt-1" data-testid={`investor-allocation-${i}`}>
                        <span className="text-[#52525B]">{(a.at || "").slice(0, 16).replace("T", " ")} · {a.broker_allocation_id}</span>
                        <span className="text-white">{fmtMoney(a.amount, currency)}</span>
                    </div>
                ))}
            </div>
        </div>
    );
}
