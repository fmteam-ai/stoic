import {
    ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, CartesianGrid,
} from "recharts";
import { BadgeCheck, ShieldAlert } from "lucide-react";

export function IntegrityStamp({ integrity }) {
    if (!integrity) return null;
    const pct = integrity.verified_pct;
    const strong = pct != null && pct >= 90;
    return (
        <div className={`border px-4 py-3 flex items-center gap-3 flex-wrap ${
            strong ? "border-[#00FF41]/30 bg-[#00FF41]/5" : "border-[#FFB000]/40 bg-[#FFB000]/5"}`}
            data-testid="integrity-stamp">
            {strong ? <BadgeCheck className="w-4 h-4 text-[#00FF41]" />
                : <ShieldAlert className="w-4 h-4 text-[#FFB000]" />}
            <span className={`font-display font-bold text-xs tracking-widest ${
                strong ? "text-[#00FF41]" : "text-[#FFB000]"}`}>
                {strong ? "BROKER-VERIFIED RECORD" : "PARTIALLY VERIFIED RECORD"}
            </span>
            <span className="font-mono text-[10px] text-[#A1A1AA]" data-testid="integrity-pct">
                {pct != null ? `${pct}%` : "—"} of {integrity.closed_trades} closed trades broker-verified
                · {integrity.estimated_or_unknown_excluded} estimates excluded
                · source: {integrity.source}
                {integrity.freshest_heartbeat_age_sec != null &&
                    ` · feed ${integrity.freshest_heartbeat_age_sec}s ago`}
            </span>
        </div>
    );
}

export function StatTiles({ overall, maxDrawdown }) {
    if (!overall) return null;
    const t = (label, value, tone) => (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
            <div className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</div>
            <div className={`font-display font-bold text-xl mt-1 ${tone || "text-white"}`}>{value}</div>
        </div>
    );
    return (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3" data-testid="verified-stats">
            {t("NET P&L (BROKER)", `${overall.net_pnl >= 0 ? "+" : ""}$${overall.net_pnl}`,
                overall.net_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]")}
            {t("WIN RATE", overall.win_rate != null ? `${overall.win_rate}%` : "—")}
            {t("CLOSED POSITIONS", overall.closed_positions)}
            {t("MAX DRAWDOWN", `$${maxDrawdown}`, "text-[#FFB000]")}
            {t("ACTIVE SINCE", overall.first_deal || "—")}
        </div>
    );
}

export function EquityCurve({ curve }) {
    if (!curve?.length) {
        return <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-8 text-center font-mono text-xs text-[#52525B]"
            data-testid="equity-empty">NO BROKER DEALS RECORDED YET</div>;
    }
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="equity-curve">
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-2">
                CUMULATIVE BROKER-VERIFIED P&L
            </div>
            <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={curve} margin={{ top: 5, right: 5, bottom: 0, left: -15 }}>
                        <defs>
                            <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stopColor="#00FF41" stopOpacity={0.25} />
                                <stop offset="100%" stopColor="#00FF41" stopOpacity={0} />
                            </linearGradient>
                        </defs>
                        <CartesianGrid stroke="#1F1F1F" vertical={false} />
                        <XAxis dataKey="date" tick={{ fill: "#52525B", fontSize: 9, fontFamily: "monospace" }} minTickGap={40} />
                        <YAxis tick={{ fill: "#52525B", fontSize: 9, fontFamily: "monospace" }} />
                        <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #1F1F1F", fontSize: 10, fontFamily: "monospace" }}
                            labelStyle={{ color: "#A1A1AA" }} />
                        <Area dataKey="cum" name="cumulative $" stroke="#00FF41" strokeWidth={1.5} fill="url(#eqfill)" />
                    </AreaChart>
                </ResponsiveContainer>
            </div>
        </div>
    );
}

export function AccountsTable({ accounts }) {
    if (!accounts?.length) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] overflow-x-auto" data-testid="verified-accounts">
            <table className="w-full text-xs">
                <thead>
                    <tr className="border-b border-[#1F1F1F] font-mono text-[9px] tracking-widest text-[#52525B]">
                        <th className="p-3 text-left">ACCOUNT</th>
                        <th className="p-3 text-left">BROKER</th>
                        <th className="p-3 text-left">MODE</th>
                        <th className="p-3 text-right">NET P&L</th>
                        <th className="p-3 text-right">WIN RATE</th>
                        <th className="p-3 text-right">POSITIONS</th>
                        <th className="p-3 text-right">LAST DEAL</th>
                    </tr>
                </thead>
                <tbody>
                    {accounts.map((a, i) => (
                        <tr key={i} className="border-b border-[#1F1F1F]">
                            <td className="p-3 font-mono text-white">{a.label}</td>
                            <td className="p-3 text-[#A1A1AA]">{a.broker || "—"}</td>
                            <td className="p-3 text-[#A1A1AA] uppercase">{a.mode || "—"}</td>
                            <td className={`p-3 text-right font-mono ${a.net_pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {a.net_pnl >= 0 ? "+" : ""}${a.net_pnl}
                            </td>
                            <td className="p-3 text-right font-mono text-[#A1A1AA]">{a.win_rate != null ? `${a.win_rate}%` : "—"}</td>
                            <td className="p-3 text-right font-mono text-[#A1A1AA]">{a.closed_positions}</td>
                            <td className="p-3 text-right font-mono text-[#52525B]">{a.last_deal || "—"}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}
