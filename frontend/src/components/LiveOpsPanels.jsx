import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";

const STAGE_CLS = {
    1: "text-[#FFD700] border-[#FFD700]/40",
    2: "text-[#0099FF] border-[#0099FF]/40",
    3: "text-[#00FF41] border-[#00FF41]/40",
};

export function CapitalStageCard() {
    const [s, setS] = useState(null);
    useEffect(() => {
        api.get("/risk/capital-stage").then(({ data }) => setS(data)).catch(() => {});
    }, []);
    if (!s) return null;
    const ev = s.evidence || {};
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="capital-stage-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">
                CAPITAL STAGE · EVIDENCE-BASED SCALING
            </div>
            <div className="flex items-center gap-2 flex-wrap">
                <span className={`font-mono text-[10px] px-2 py-0.5 border ${STAGE_CLS[s.stage] || STAGE_CLS[1]}`}
                    data-testid="capital-stage-label">
                    STAGE {s.stage} · {s.label}
                </span>
                <span className="font-mono text-[10px] text-white" data-testid="capital-stage-cap">
                    {s.risk_cap_pct != null ? `risk capped at ${s.risk_cap_pct}%/trade` : "config risk honored"}
                </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">
                {[["TRADES", ev.n], ["MEAN R", ev.mean_r], ["CI95 LOWER", ev.ci95_lower_r],
                  ["MAX DD (R)", ev.max_drawdown_r]].map(([k, v]) => (
                    <div key={k} className="border border-[#141414] p-2">
                        <div className="font-mono text-[8px] text-[#52525B]">{k}</div>
                        <div className="font-mono text-xs text-white">{v ?? "—"}</div>
                    </div>
                ))}
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mt-2" data-testid="capital-stage-next">
                NEXT: {s.next_milestone}
            </div>
        </div>
    );
}

export function SubsystemHealthCard() {
    const [h, setH] = useState(null);
    useEffect(() => {
        api.get("/subsystems/health").then(({ data }) => setH(data)).catch(() => {});
    }, []);
    if (!h) return null;
    const cls = (v) => v == null ? "text-[#3F3F46]" : v >= 60 ? "text-[#00FF41]" : v >= 40 ? "text-[#FFD700]" : "text-[#FF3B30]";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="subsystem-health-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">
                SUBSYSTEM SELF-MONITORING · AUTOMATIC CONSERVATISM
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
                {Object.entries(h.subsystems || {}).map(([k, v]) => (
                    <div key={k} className="border border-[#141414] p-2" data-testid={`subsystem-${k}`}>
                        <div className="font-mono text-[8px] text-[#52525B]">{k.replace(/_/g, " ").toUpperCase()}</div>
                        <div className={`font-mono text-xs ${cls(v)}`}>{v ?? "n/a"}</div>
                    </div>
                ))}
            </div>
            <div className={`font-mono text-[9px] mt-2 ${h.multiplier < 1 ? "text-[#FFD700]" : "text-[#3F3F46]"}`}
                data-testid="subsystem-conservatism">
                SIZING ×{h.multiplier} — {h.reason}
            </div>
        </div>
    );
}

export function RealtimeRiskCard() {
    const [r, setR] = useState(null);
    useEffect(() => {
        api.get("/risk/realtime").then(({ data }) => setR(data)).catch(() => {});
    }, []);
    if (!r) return null;
    const ot = r.open_trades || {};
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="realtime-risk-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">
                REAL-TIME RISK COMPOSITE
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">OPEN TRADES</div>
                    <div className="font-mono text-xs text-white" data-testid="realtime-open-trades">{ot.total ?? 0}</div>
                </div>
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">ACCOUNTS</div>
                    <div className="font-mono text-xs text-white">{(r.accounts || []).length}</div>
                </div>
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">SHADOW HEALTH</div>
                    <div className={`font-mono text-xs ${(r.shadow_health?.overall ?? 100) >= 60 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                        {r.shadow_health?.overall ?? "—"}
                    </div>
                </div>
                <div className="border border-[#141414] p-2">
                    <div className="font-mono text-[8px] text-[#52525B]">PROMOTIONS</div>
                    <div className={`font-mono text-xs ${r.shadow_health?.promotions_paused ? "text-[#FFD700]" : "text-[#00FF41]"}`}>
                        {r.shadow_health?.promotions_paused ? "PAUSED" : "OPEN"}
                    </div>
                </div>
            </div>
            {Object.keys(ot.by_symbol || {}).length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                    {Object.entries(ot.by_symbol).map(([sym, n]) => (
                        <span key={sym} className="font-mono text-[8px] px-1.5 py-0.5 border border-[#27272A] text-[#A1A1AA]">
                            {sym}: {n}
                        </span>
                    ))}
                </div>
            )}
            {(r.accounts || []).length > 0 && (
                <div className="mt-2 space-y-1">
                    {r.accounts.map((a) => (
                        <div key={a.id} className="flex items-center gap-2 font-mono text-[9px] text-[#52525B]">
                            <span className="text-[#A1A1AA]">{a.label || a.id.slice(0, 8)}</span>
                            <span className="ml-auto">eq {a.equity ?? "—"} · bal {a.balance ?? "—"} {a.currency || ""}</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

export function ModeGuardianCard() {
    const [g, setG] = useState(null);
    useEffect(() => {
        api.get("/modes/guardian").then(({ data }) => setG(data)).catch(() => {});
    }, []);
    if (!g) return null;
    const rec = g.recovery || {};
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="mode-guardian-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                MODE GUARDIAN · AUTOMATIC AUTHORITY DEMOTION
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-2">
                autonomous → supervised (health &lt; 60 sustained) → defensive (&lt; 40) → observe (broker-truth uncertainty). Recovery: {g.green_hours_required}h green + explicit approval.
            </div>
            <div className={`font-mono text-[9px] px-2 py-1 border inline-block ${!rec.applicable ? "text-[#00FF41] border-[#00FF41]/40" : rec.eligible ? "text-[#0099FF] border-[#0099FF]/40" : "text-[#FFD700] border-[#FFD700]/40"}`}
                data-testid="mode-guardian-recovery">
                {!rec.applicable ? "NO AUTOMATIC DEMOTIONS ON RECORD" : rec.eligible ? "RECOVERY ELIGIBLE — EXPLICIT PROMOTION REQUIRED" : `RECOVERY LOCKED — ${rec.reason}`}
            </div>
            {(g.recent_demotions || []).length > 0 && (
                <div className="mt-2 space-y-1">
                    {g.recent_demotions.map((d, i) => (
                        <div key={i} className="font-mono text-[9px] text-[#52525B] border-t border-[#141414] pt-1"
                            data-testid={`mode-demotion-${i}`}>
                            <span className="text-[#FF3B30]">→ {d.ceiling}</span> · {d.reason} · health {d.health_overall ?? "—"} · {(d.demotions || []).length} config(s) · {d.at}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

const ACTION_ICON_CLS = {
    freeze_trading: "text-[#0099FF] border-[#0099FF]/40 hover:bg-[#0099FF]/10",
    reduce_exposure: "text-[#FFD700] border-[#FFD700]/40 hover:bg-[#FFD700]/10",
    pause_symbol: "text-[#FFD700] border-[#FFD700]/40 hover:bg-[#FFD700]/10",
    defensive_mode: "text-[#FFD700] border-[#FFD700]/40 hover:bg-[#FFD700]/10",
    panic_mode: "text-[#FF3B30] border-[#FF3B30]/40 hover:bg-[#FF3B30]/10",
};

export function OperatorConsole({ onAction }) {
    const [actions, setActions] = useState([]);
    const [armed, setArmed] = useState(null);
    const [symbol, setSymbol] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        api.get("/operator/actions").then(({ data }) => setActions(data.actions || [])).catch(() => {});
    }, []);
    if (!actions.length) return null;

    const fire = async (action) => {
        if (armed !== action) { setArmed(action); return; }
        setArmed(null);
        setBusy(true);
        try {
            const params = action === "pause_symbol" ? { symbol: symbol.trim().toUpperCase() } : {};
            const { data } = await api.post("/operator/action", { action, params });
            toast.success(data.detail || `${action} executed`);
            onAction && onAction();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="operator-console">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                OPERATOR INTERVENTION · ONE-CLICK, AUDITED
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-3">
                Every action only reduces authority or risk. Click twice to confirm.
            </div>
            <div className="space-y-2">
                {actions.map((a) => (
                    <div key={a.action} className="flex items-center gap-2 flex-wrap">
                        <button
                            onClick={() => fire(a.action)}
                            disabled={busy || (a.action === "pause_symbol" && armed !== a.action && !symbol.trim() && armed === a.action)}
                            data-testid={`operator-action-${a.action}`}
                            className={`font-mono text-[9px] px-2.5 py-1.5 border tracking-widest transition-colors disabled:opacity-40 ${armed === a.action ? "bg-[#FF3B30]/20 text-[#FF3B30] border-[#FF3B30]" : ACTION_ICON_CLS[a.action] || "text-[#A1A1AA] border-[#27272A]"}`}>
                            {armed === a.action ? "CONFIRM?" : a.action.replace(/_/g, " ").toUpperCase()}
                        </button>
                        {a.action === "pause_symbol" && (
                            <input value={symbol} onChange={(e) => setSymbol(e.target.value)}
                                placeholder="SYMBOL"
                                data-testid="operator-pause-symbol-input"
                                className="bg-transparent border border-[#1F1F1F] font-mono text-[9px] px-2 py-1.5 w-24 text-white placeholder:text-[#3F3F46] focus:outline-none focus:border-[#333333]" />
                        )}
                        <span className="font-mono text-[9px] text-[#52525B]">{a.description}</span>
                        {armed === a.action && (
                            <button onClick={() => setArmed(null)}
                                data-testid={`operator-cancel-${a.action}`}
                                className="font-mono text-[9px] px-2 py-1.5 border border-[#27272A] text-[#A1A1AA]">
                                CANCEL
                            </button>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}
