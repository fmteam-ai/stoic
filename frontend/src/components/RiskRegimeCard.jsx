import { useCallback, useEffect, useState } from "react";
import { RefreshCw, ShieldAlert } from "lucide-react";
import api from "../lib/api";

// v1.60.53 (B5) — advisory risk regime: SHADOW — not affecting trades.
const COLOR = (s) => (s == null ? "#52525B" : s >= 65 ? "#FF3B30" : s >= 35 ? "#FFB000" : "#00FF41");

export function RiskRegimeCard() {
    const [d, setD] = useState(null);
    const load = useCallback(() => {
        api.get("/ops/risk-regime").then((r) => setD(r.data)).catch(() => setD({ error: true }));
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
    const adv = d?.advisory;
    const score = adv?.risk_off_score;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="risk-regime-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <ShieldAlert className="w-3.5 h-3.5 text-[#FFB000]" />
                <span className="font-display font-bold text-sm">Risk regime (advisory)</span>
                <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#FFB000]/40 text-[#FFB000]" data-testid="risk-regime-shadow-badge">
                    SHADOW — NOT AFFECTING TRADES
                </span>
                <button onClick={load} className="ml-auto text-[#52525B] hover:text-white" data-testid="risk-regime-refresh" aria-label="Refresh"><RefreshCw className="w-3 h-3" /></button>
            </div>
            <div className="px-4 py-3">
                {d?.error && <div className="font-mono text-[11px] text-[#FF3B30]">unavailable</div>}
                {d && !d.error && !adv && <div className="font-mono text-[11px] text-[#52525B]" data-testid="risk-regime-empty">No advisory computed yet — the first GDELT poll runs within 15 min.</div>}
                {adv && (
                    <div className="flex flex-wrap items-start gap-6">
                        <div>
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">RISK-OFF SCORE</div>
                            <div className="font-display font-bold text-3xl tabular-nums" style={{ color: COLOR(score) }} data-testid="risk-regime-score">{score ?? "—"}</div>
                            <div className="font-mono text-[9px] text-[#52525B] uppercase">{adv.bucket || "—"}</div>
                        </div>
                        <div className="font-mono text-[10px] text-[#A1A1AA]">
                            <div className="text-[#52525B] text-[9px] tracking-widest mb-1">TOP DRIVERS</div>
                            {(adv.top_drivers || []).map((k) => (
                                <div key={k} data-testid={`risk-regime-driver-${k}`}>{k.replace(/_/g, " ")} · {adv.components?.[k] ?? "—"}</div>
                            ))}
                        </div>
                        <div className="font-mono text-[10px] text-[#A1A1AA]">
                            <div className="text-[#52525B] text-[9px] tracking-widest mb-1">CONTEXT</div>
                            <div>geo {adv.geo_risk ?? "—"} · VIX {adv.vix_regime || "—"} · GVZ {adv.gvz_regime || "—"}</div>
                            <div>{adv.cb_event_active ? `${adv.cb_event_active.bank} ${adv.cb_event_active.ccy} · ${adv.cb_event_active.minutes_since} min ago` : "no central-bank decision in the last 90 min"}</div>
                            <div className="text-[#52525B]">updated {adv.at ? new Date(adv.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}</div>
                        </div>
                    </div>
                )}
                {d?.finnhub && !d.finnhub.configured && (
                    <div className="font-mono text-[9px] text-[#52525B] mt-2" data-testid="risk-regime-finnhub">{d.finnhub.message}</div>
                )}
            </div>
        </div>
    );
}
