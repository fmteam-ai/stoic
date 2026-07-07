import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Compass, RefreshCw, LineChart, Globe, Newspaper, Cpu, ShieldCheck } from "lucide-react";

const DIR_COLOR = { UP: "#00FF41", DOWN: "#FF3B30", FLAT: "#A1A1AA" };

function TierPill({ name, tier }) {
    const d = tier?.direction || "?";
    return (
        <span className="font-mono text-[10px] tracking-wider px-1.5 py-0.5 border border-[#1F1F1F]"
              style={{ color: DIR_COLOR[d] || "#A1A1AA" }}
              data-testid={`posture-tier-${name.toLowerCase()}`}>
            {name} {d}{tier?.slope_pct != null ? ` ${tier.slope_pct > 0 ? "+" : ""}${tier.slope_pct}%` : ""}
        </span>
    );
}

function AgentRow({ icon: Icon, color, label, children, testid }) {
    return (
        <div className="flex items-start gap-2 py-1.5 border-b border-[#141414] last:border-0" data-testid={testid}>
            <Icon size={13} style={{ color }} className="mt-0.5 shrink-0" />
            <div className="min-w-0">
                <span className="font-mono text-[10px] tracking-widest" style={{ color }}>{label}</span>
                <div className="text-xs text-[#A1A1AA] leading-snug">{children}</div>
            </div>
        </div>
    );
}

export default function MarketPosture() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        try {
            const r = await api.get("/bot/posture");
            setData(r.data);
        } catch { /* silent */ }
        setLoading(false);
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 120000);
        return () => clearInterval(id);
    }, [load]);

    if (loading || !data) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="market-posture-card">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">MARKET POSTURE — LOADING…</div>
            </div>
        );
    }

    const exp = data.today_expectancy;
    const expPositive = exp && exp.expectancy_per_trade >= 0;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-3" data-testid="market-posture-card">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Compass size={14} className="text-[#FFB000]" />
                    <span className="font-display font-bold text-sm tracking-wide">MARKET POSTURE</span>
                    <span className="font-mono text-[10px] text-[#52525B]">what every agent thinks right now</span>
                </div>
                <button onClick={load} className="text-[#52525B] hover:text-white transition-colors" data-testid="posture-refresh-btn">
                    <RefreshCw size={12} />
                </button>
            </div>

            {exp && (
                <div className={`border p-2 font-mono text-[11px] ${expPositive ? "border-[#00FF41]/30 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}
                     data-testid="posture-expectancy">
                    TODAY: {exp.trades} trades · WR {exp.win_rate_pct}% · avgW ${exp.avg_win} / avgL ${exp.avg_loss} ·
                    expectancy <strong>${exp.expectancy_per_trade}/trade</strong> · total ${exp.total}
                </div>
            )}

            {Object.entries(data.symbols || {}).map(([sym, s]) => (
                <div key={sym} className="border border-[#141414] bg-black/30 p-3 space-y-1" data-testid={`posture-symbol-${sym}`}>
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-display font-bold text-sm">{sym}</span>
                        <span className="font-mono text-[10px] text-[#A1A1AA]">{s.regime || ""}</span>
                        {["SHORT", "MEDIUM", "LONG"].map(n => <TierPill key={n} name={n === "SHORT" ? "WEEK" : n === "MEDIUM" ? "MONTH" : "QTR"} tier={s.tiers?.[n]} />)}
                    </div>

                    <AgentRow icon={LineChart} color="#00FF41" label="STRUCTURE" testid={`posture-structure-${sym}`}>
                        {s.structure?.ready
                            ? <>Bias <strong style={{ color: s.structure.bias === "BULLISH" ? "#00FF41" : s.structure.bias === "BEARISH" ? "#FF3B30" : "#A1A1AA" }}>{s.structure.bias}</strong>
                                {s.structure.last_bos ? ` · ${s.structure.last_bos.dir} BOS ${s.structure.last_bos.age_bars} bars ago` : " · no recent BOS"}
                                {s.structure.recent_sweep ? ` · ${s.structure.recent_sweep.side === "BUY_SIDE" ? "buy-side" : "sell-side"} liquidity sweep` : ""}
                                {s.structure.acc_dist ? ` · ${s.structure.acc_dist.phase.toLowerCase()}` : ""}
                                {(s.structure.unfilled_fvg || []).length > 0 ? ` · ${s.structure.unfilled_fvg.length} open FVG` : ""}</>
                            : <span className="text-[#52525B]">{s.structure?.reason || "waiting for M15 candles (EA v1.42)"}</span>}
                    </AgentRow>

                    <AgentRow icon={Cpu} color="#06B6D4" label="QUANT" testid={`posture-quant-${sym}`}>
                        Last read: <strong>{s.action || "—"}</strong>{s.confidence ? ` @ ${s.confidence}%` : ""}
                        {s.range_forecast?.remaining_range != null
                            ? ` · day range ${s.range_forecast.used_pct}% used, ~${s.range_forecast.remaining_range} left`
                            : s.range_forecast?.atr14 ? ` · ATR14 ${s.range_forecast.atr14}` : ""}
                        {s.intraday_momentum?.change_pct != null ? ` · intraday ${s.intraday_momentum.change_pct > 0 ? "+" : ""}${s.intraday_momentum.change_pct}%` : ""}
                    </AgentRow>

                    {(s.active_vetoes || []).length > 0 && (
                        <div className="font-mono text-[10px] text-[#FFB000] space-y-0.5" data-testid={`posture-vetoes-${sym}`}>
                            {s.active_vetoes.map((v, i) => <div key={i}>⛔ {v.length > 130 ? v.slice(0, 130) + "…" : v}</div>)}
                        </div>
                    )}
                    {(s.unlock_hints || []).length > 0 && (
                        <div className="text-[11px] text-[#A1A1AA] space-y-0.5" data-testid={`posture-hints-${sym}`}>
                            {s.unlock_hints.map((h, i) => <div key={i}>→ {h}</div>)}
                        </div>
                    )}
                </div>
            ))}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4">
                <AgentRow icon={Globe} color="#FFD700" label="MACRO" testid="posture-macro">
                    {Array.isArray(data.macro?.series) && data.macro.series.length > 0
                        ? data.macro.series.slice(0, 4).map(s => `${s.name} ${s.latest}${s.unit || ""}`).join(" · ")
                        : (data.macro ? "snapshot live" : "unavailable")}
                </AgentRow>
                <AgentRow icon={Newspaper} color="#A855F7" label="FED TONE" testid="posture-fed">
                    {data.fed_tone
                        ? <><strong>{data.fed_tone.label}</strong> ({data.fed_tone.score > 0 ? "+" : ""}{data.fed_tone.score}) — {data.fed_tone.summary}</>
                        : <span className="text-[#52525B]">no recent Fed headlines scored</span>}
                </AgentRow>
            </div>

            <AgentRow icon={ShieldCheck} color="#FF3B30" label="RISK" testid="posture-risk">
                {data.risk?.loss_cooldown_armed
                    ? <span className="text-[#FFB000]">Loss cooldown ARMED ({data.risk.losses_last_30min} loss in last 30min — same-direction re-entries paused)</span>
                    : "Loss cooldown idle"}
                {(data.risk?.active_auto_guards || []).length > 0 &&
                    <> · {data.risk.active_auto_guards.length} auto-guard{data.risk.active_auto_guards.length > 1 ? "s" : ""} active: {data.risk.active_auto_guards.map(g => g.title).join("; ")}</>}
            </AgentRow>
        </div>
    );
}
