import { useEffect, useState, useCallback, Fragment } from "react";
import api from "@/lib/api";
import { Crosshair, ChevronDown, ChevronUp } from "lucide-react";

const short = (v) => (v == null || v === "" ? "—"
    : String(v).length > 10 ? `…${String(v).slice(-8)}` : String(v));

function IdCell({ label, value, testid }) {
    return (
        <span className="font-mono text-[10px] text-[#A1A1AA]" data-testid={testid}
            title={value != null ? String(value) : undefined}>
            <span className="text-[#52525B]">{label}</span>{" "}
            <span className="text-white">{short(value)}</span>
        </span>
    );
}

export function ScalpExecutions({ accountId }) {
    const [items, setItems] = useState(null);
    const [open, setOpen] = useState(null);

    const load = useCallback(async () => {
        try {
            const q = accountId ? `&account_id=${encodeURIComponent(accountId)}` : "";
            const { data } = await api.get(`/scalp/executions?limit=25${q}`);
            setItems(data.items || []);
        } catch { /* best-effort */ }
    }, [accountId]);

    useEffect(() => {
        load();
        const t = setInterval(load, 15000);
        return () => clearInterval(t);
    }, [load]);

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] mb-6" data-testid="scalp-executions-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Crosshair className="w-3.5 h-3.5 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Executions</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    INTENT · RESERVATION · ORDER · DEAL · POSITION · LATENCY
                </span>
            </div>
            {(!items || items.length === 0) && (
                <div className="p-4 font-mono text-[10px] text-[#52525B] tracking-widest"
                    data-testid="scalp-executions-empty">
                    NO SCALP EXECUTIONS YET — orders appear here once the fast path submits to the broker
                </div>
            )}
            {items && items.length > 0 && (
                <div className="divide-y divide-[#1F1F1F]">
                    {items.map((x) => {
                        const unprot = x.protection && !x.protection.protected && x.status === "open";
                        return (
                            <Fragment key={x.trade_id}>
                                <button type="button" onClick={() => setOpen(open === x.trade_id ? null : x.trade_id)}
                                    className="w-full text-left px-4 py-2.5 hover:bg-[#121212] transition-colors"
                                    data-testid={`scalp-exec-row-${x.trade_id}`}>
                                    <div className="flex items-center gap-3 flex-wrap">
                                        <span className="font-mono text-xs text-white w-20">{x.symbol}</span>
                                        <span className={`font-mono text-[10px] ${x.action === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{x.action}</span>
                                        <span className="font-mono text-[10px] text-[#A1A1AA]">{(x.lifecycle_state || x.submission_state || x.status || "").toUpperCase()}</span>
                                        <span className="font-mono text-[10px] text-[#A1A1AA]" data-testid={`scalp-exec-latency-${x.trade_id}`}>
                                            LAT <span className="text-white">{x.broker_latency_ms != null ? `${x.broker_latency_ms}ms` : "—"}</span>
                                        </span>
                                        <span className={`font-mono text-[10px] ${unprot ? "text-[#FF3B30]" : "text-[#00FF41]"}`}
                                            data-testid={`scalp-exec-protection-${x.trade_id}`}>
                                            {unprot
                                                ? `UNPROTECTED ${x.protection.unprotected_age_sec != null ? `${x.protection.unprotected_age_sec}s` : ""}`
                                                : x.status === "open" ? "PROTECTED" : "CLOSED"}
                                        </span>
                                        {x.pnl != null && (
                                            <span className={`font-mono text-[10px] ${x.pnl >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                                {x.pnl >= 0 ? "+" : ""}{Number(x.pnl).toFixed(2)}
                                            </span>
                                        )}
                                        <span className="ml-auto text-[#52525B]">
                                            {open === x.trade_id ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                                        </span>
                                    </div>
                                    <div className="flex items-center gap-3 flex-wrap mt-1">
                                        <IdCell label="INTENT" value={x.intent_id} testid={`scalp-exec-intent-${x.trade_id}`} />
                                        <IdCell label="RESV" value={x.reservation_id} testid={`scalp-exec-resv-${x.trade_id}`} />
                                        <IdCell label="ORDER" value={x.order_ticket} testid={`scalp-exec-order-${x.trade_id}`} />
                                        <IdCell label="DEAL" value={x.deal_id} testid={`scalp-exec-deal-${x.trade_id}`} />
                                        <IdCell label="POS" value={x.position_id} testid={`scalp-exec-pos-${x.trade_id}`} />
                                        {x.reservation_state && (
                                            <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]">
                                                {x.reservation_state}{x.reserved_risk_usd != null ? ` · $${Number(x.reserved_risk_usd).toFixed(0)}` : ""}
                                            </span>
                                        )}
                                        {x.reconciliation && (
                                            <span data-testid={`scalp-exec-recon-${x.trade_id}`}
                                                className={`font-mono text-[9px] tracking-widest px-1.5 py-0.5 border ${
                                                    x.reconciliation === "reconciled"
                                                        ? "border-[#00FF41]/40 text-[#00FF41]"
                                                        : "border-[#FFB000]/40 text-[#FFB000]"}`}>
                                                {x.reconciliation.toUpperCase()}
                                            </span>
                                        )}
                                    </div>
                                </button>
                                {open === x.trade_id && (
                                    <div className="px-4 py-3 bg-black/40" data-testid={`scalp-exec-timeline-${x.trade_id}`}>
                                        <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">EXECUTION TIMELINE</div>
                                        {(x.timeline || []).length === 0 && (
                                            <div className="font-mono text-[10px] text-[#52525B]">no events recorded</div>
                                        )}
                                        {(x.timeline || []).map((e, i) => (
                                            <div key={i} className="flex items-center gap-3 py-0.5 font-mono text-[10px]">
                                                <span className="text-[#0099FF]">▸</span>
                                                <span className="text-white">{e.type}</span>
                                                <span className="text-[#52525B]">{e.at ? new Date(e.at).toLocaleTimeString() : "—"}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </Fragment>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
