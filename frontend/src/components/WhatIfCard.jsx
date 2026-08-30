import { useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { FlaskConical, Loader2 } from "lucide-react";

const F = ({ label, value, onChange, placeholder }) => (
    <label className="flex flex-col gap-1">
        <span className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</span>
        <input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
            data-testid={`whatif-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`}
            className="bg-black border border-[#2A2A2A] px-2 py-1 font-mono text-xs text-white w-24 focus:border-[#FFD700] outline-none" />
    </label>
);

const money = (v) => (v >= 0 ? `+$${v}` : `-$${Math.abs(v)}`);

export const WhatIfCard = () => {
    const [risk, setRisk] = useState("");
    const [slM, setSlM] = useState("");
    const [tpM, setTpM] = useState("");
    const [trail, setTrail] = useState("");
    const [days, setDays] = useState("30");
    const [busy, setBusy] = useState(false);
    const [res, setRes] = useState(null);
    const [err, setErr] = useState(null);

    const run = async () => {
        setBusy(true); setErr(null); setRes(null);
        const body = { days: Number(days) || 30 };
        if (risk) body.risk_pct = Number(risk);
        if (slM) body.sl_mult = Number(slM);
        if (tpM) body.tp_mult = Number(tpM);
        if (trail) body.trailing_start_r = Number(trail);
        try {
            const { data } = await api.post("/trades/what-if", body);
            if (data.error) setErr(data.error); else setRes(data);
        } catch (e) {
            setErr(formatApiError(e));
        } finally { setBusy(false); }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="whatif-card">
            <div className="flex items-center gap-2">
                <FlaskConical className="w-4 h-4 text-[#FFD700]" />
                <span className="font-display font-bold text-sm text-white">What-If Analysis</span>
                <span className="font-mono text-[9px] tracking-widest text-[#52525B]">RE-SIMULATES YOUR REAL TRADES</span>
            </div>
            <div className="flex gap-3 flex-wrap items-end mt-3">
                <F label="RISK %" value={risk} onChange={setRisk} placeholder="0.5" />
                <F label="SL ×" value={slM} onChange={setSlM} placeholder="1.2" />
                <F label="TP ×" value={tpM} onChange={setTpM} placeholder="0.8" />
                <F label="TRAIL START R" value={trail} onChange={setTrail} placeholder="1.2" />
                <F label="DAYS" value={days} onChange={setDays} placeholder="30" />
                <button onClick={run} disabled={busy || (!risk && !slM && !tpM && !trail)}
                    data-testid="whatif-run"
                    className="flex items-center gap-1.5 font-mono text-[10px] tracking-widest px-3 py-1.5 border border-[#FFD700]/40 text-[#FFD700] hover:bg-[#FFD700]/10 disabled:opacity-40">
                    {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                    {busy ? "SIMULATING…" : "RUN SCENARIO"}
                </button>
            </div>
            {err && <div className="font-mono text-[10px] text-[#FF3B30] mt-2" data-testid="whatif-error">{String(err)}</div>}
            {res && (
                <div className="mt-3 border-t border-[#141414] pt-2" data-testid="whatif-result">
                    <div className="grid grid-cols-3 gap-3 font-mono text-[11px]">
                        <div>
                            <div className="text-[9px] tracking-widest text-[#52525B]">ACTUAL</div>
                            <div className="text-white">{money(res.baseline.total_pnl)}</div>
                            <div className="text-[#FF8C00] text-[10px]">DD ${res.baseline.max_drawdown}</div>
                            <div className="text-[#A1A1AA] text-[10px]">WR {Math.round(res.baseline.win_rate * 100)}%</div>
                        </div>
                        <div>
                            <div className="text-[9px] tracking-widest text-[#52525B]">SCENARIO</div>
                            <div className="text-white">{money(res.simulated.total_pnl)}</div>
                            <div className="text-[#FF8C00] text-[10px]">DD ${res.simulated.max_drawdown}</div>
                            <div className="text-[#A1A1AA] text-[10px]">WR {Math.round(res.simulated.win_rate * 100)}%</div>
                        </div>
                        <div>
                            <div className="text-[9px] tracking-widest text-[#52525B]">DELTA</div>
                            <div style={{ color: res.delta.total_pnl >= 0 ? "#00FF41" : "#FF3B30" }}
                                data-testid="whatif-delta">{money(res.delta.total_pnl)}</div>
                            <div className="text-[10px]" style={{ color: res.delta.max_drawdown <= 0 ? "#00FF41" : "#FF3B30" }}>
                                DD {res.delta.max_drawdown >= 0 ? "+" : ""}${res.delta.max_drawdown}
                            </div>
                        </div>
                    </div>
                    <div className="font-mono text-[9px] text-[#71717A] mt-2">
                        {res.coverage.simulated}/{res.coverage.total} trades · {res.mode} · {res.note}
                    </div>
                </div>
            )}
        </div>
    );
};
