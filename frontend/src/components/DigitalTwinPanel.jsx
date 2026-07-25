import { useEffect, useState } from "react";
import api from "@/lib/api";

const fmtPnl = (v) => v == null ? "—" : `${v >= 0 ? "+$" : "-$"}${Math.abs(v).toFixed(2)}`;
const pnlCls = (v) => v > 0 ? "text-[#00FF41]" : v < 0 ? "text-[#FF3B30]" : "text-[#A1A1AA]";

const VERDICT_CLS = {
    "GATES PROTECTING": "text-[#00FF41] border-[#00FF41]/40",
    "GATES COSTING EDGE": "text-[#FF3B30] border-[#FF3B30]/40",
    "NEUTRAL": "text-[#FFD700] border-[#FFD700]/40",
    "NO INTERCEPTS": "text-[#52525B] border-[#1F1F1F]",
};

export function DigitalTwinPanel({ days }) {
    const [tw, setTw] = useState(null);
    useEffect(() => {
        api.get(`/twin/summary?days=${days || 30}`).then(({ data }) => setTw(data)).catch(() => {});
    }, [days]);
    const accounts = tw?.accounts || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="twin-panel">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                DIGITAL TWIN · LIVE ACCOUNT VS SHADOW COPY
            </div>
            <div className="font-mono text-[10px] text-[#3F3F46] mb-3">
                Every intercepted decision replayed against the bars that actually followed — what would have happened without the gates, per account.
            </div>
            {accounts.length ? accounts.map((a) => (
                <div key={a.account_id} className="py-2 border-t border-[#141414]"
                    data-testid={`twin-account-${a.account_id}`}>
                    <div className="flex flex-wrap items-center gap-3">
                        <div className="w-40 font-mono text-[10px] text-[#A1A1AA] truncate">
                            {a.label || a.account_id}
                        </div>
                        <div className="font-mono text-[10px]">
                            <span className="text-[#52525B]">LIVE </span>
                            <span className={pnlCls(a.live.pnl)}>{fmtPnl(a.live.pnl)}</span>
                            <span className="text-[#52525B]"> ({a.live.wins}W/{a.live.losses}L)</span>
                        </div>
                        <div className="font-mono text-[10px]">
                            <span className="text-[#52525B]">TWIN </span>
                            <span className="text-[#0099FF]">{a.twin.replayed} intercepts</span>
                            <span className="text-[#52525B]"> · alt </span>
                            <span className={pnlCls(a.twin.alt_r)}>{a.twin.alt_r >= 0 ? "+" : ""}{a.twin.alt_r}R</span>
                            {a.twin.alt_pnl_est != null && (
                                <span className={`${pnlCls(a.twin.alt_pnl_est)}`}> ≈ {fmtPnl(a.twin.alt_pnl_est)}</span>
                            )}
                        </div>
                        <span className={`ml-auto font-mono text-[10px] px-2 py-0.5 border ${VERDICT_CLS[a.verdict] || VERDICT_CLS.NEUTRAL}`}
                            data-testid={`twin-verdict-${a.account_id}`}>
                            {a.verdict}
                        </span>
                    </div>
                </div>
            )) : (
                <div className="font-mono text-xs text-[#52525B]" data-testid="twin-empty">
                    No live trades or intercepted decisions in this window yet.
                </div>
            )}
            {(tw?.top_divergences || []).length > 0 && (
                <div className="mt-3 pt-2 border-t border-[#141414]" data-testid="twin-divergences">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1.5">
                        BIGGEST ALTERNATIVE DECISIONS
                    </div>
                    <div className="flex flex-wrap gap-2">
                        {tw.top_divergences.map((d, i) => (
                            <span key={i} className="font-mono text-[10px] px-2 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]">
                                {d.action} {d.symbol} · {d.stage} · <span className={pnlCls(d.r)}>{d.r >= 0 ? "+" : ""}{d.r}R</span>
                            </span>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}
