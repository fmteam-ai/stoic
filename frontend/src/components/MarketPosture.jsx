import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Compass, RefreshCw, LineChart, Globe, Newspaper, Cpu, ShieldCheck, TrendingUp, Layers, CalendarClock } from "lucide-react";

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
                        {s.consensus && (
                            <span className="ml-auto flex items-center gap-2 font-mono text-[11px]" data-testid={`posture-consensus-${sym}`}>
                                <span className="text-[#52525B] text-[9px] tracking-widest">MASTER CONSENSUS</span>
                                {["BUY", "SELL"].map(a => {
                                    const sc = s.consensus[a]?.score;
                                    const col = sc >= 70 ? "#00FF41" : sc >= 55 ? "#FFB000" : "#52525B";
                                    return (
                                        <span key={a} className="px-1.5 py-0.5 border"
                                              style={{ borderColor: `${col}55`, color: col }}
                                              title={s.consensus[a] ? Object.entries(s.consensus[a].votes).map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${v}`).join(" · ") : ""}>
                                            {a} {sc}
                                        </span>
                                    );
                                })}
                                <span className="text-[#52525B] text-[9px]">≥55 to trade</span>
                            </span>
                        )}
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

                    <AgentRow icon={Layers} color="#E879F9" label="LIQUIDITY" testid={`posture-liquidity-${sym}`}>
                        {s.liquidity?.ready
                            ? <>{s.liquidity.draw
                                    ? <>Draw on liquidity: <strong style={{ color: s.liquidity.draw === "UP" ? "#00FF41" : "#FF3B30" }}>{s.liquidity.draw}</strong></>
                                    : "No dominant liquidity pull"}
                                {s.liquidity.nearest_above ? ` · buy-stops @ ${s.liquidity.nearest_above.level} (×${s.liquidity.nearest_above.strength})` : ""}
                                {s.liquidity.nearest_below ? ` · sell-stops @ ${s.liquidity.nearest_below.level} (×${s.liquidity.nearest_below.strength})` : ""}
                                {s.liquidity.active_zone
                                    ? <> · in <strong style={{ color: s.liquidity.active_zone === "DEMAND" ? "#00FF41" : "#FF3B30" }}>{s.liquidity.active_zone} block</strong></>
                                    : ""}
                                {(s.liquidity.order_blocks || []).length > 0 ? ` · ${s.liquidity.order_blocks.length} unmitigated OB` : ""}
                                {s.liquidity.profile ? ` · POC ${s.liquidity.profile.poc}` : ""}
                                {s.liquidity.cum_delta ? ` · delta ${s.liquidity.cum_delta.bias.toLowerCase()}${s.liquidity.cum_delta.divergence ? ` (${s.liquidity.cum_delta.divergence.replace(/_/g, " ").toLowerCase()})` : ""}` : ""}
                                <span className="block font-mono text-[10px] mt-0.5" style={{ color: s.liquidity.dom?.live ? "#E879F9" : "#52525B" }} data-testid={`posture-dom-${sym}`}>
                                    {s.liquidity.dom?.live
                                        ? `DOM LIVE: book ${s.liquidity.dom.imbalance > 0 ? "+" : ""}${Math.round(s.liquidity.dom.imbalance * 100)}% ${s.liquidity.dom.imbalance >= 0 ? "bid" : "ask"}-heavy · walls ${s.liquidity.dom.wall_bid ? `bid ${s.liquidity.dom.wall_bid.p}` : ""}${s.liquidity.dom.wall_ask ? ` / ask ${s.liquidity.dom.wall_ask.p}` : ""}`
                                        : "DOM offline — EA v1.43 streams the order book where the broker provides one"}
                                </span></>
                            : <span className="text-[#52525B]">{s.liquidity?.reason || "waiting for M15 candles (EA v1.43)"}</span>}
                    </AgentRow>

                    <AgentRow icon={Newspaper} color="#A855F7" label="AI NEWS" testid={`posture-news-${sym}`}>
                        {s.news_ai
                            ? <>Reads the tape as <strong style={{ color: s.news_ai.net >= 0.75 ? "#00FF41" : s.news_ai.net <= -0.75 ? "#FF3B30" : "#A1A1AA" }}>
                                    {s.news_ai.label.replace(/_/g, " ")} ({s.news_ai.net > 0 ? "+" : ""}{s.news_ai.net}/3)
                                </strong> for {sym} across {s.news_ai.headlines} headlines
                                {(s.news_ai.drivers || []).length > 0 && (
                                    <span className="block font-mono text-[10px] text-[#A855F7] mt-0.5 space-y-0.5" data-testid={`posture-news-drivers-${sym}`}>
                                        {s.news_ai.drivers.map((d, i) => (
                                            <span key={i} className="block">
                                                <span style={{ color: d.score >= 1 ? "#00FF41" : d.score <= -1 ? "#FF3B30" : "#A1A1AA" }}>{d.score > 0 ? "+" : ""}{d.score}</span>
                                                {" "}{d.title.length > 90 ? d.title.slice(0, 90) + "…" : d.title}{d.why ? ` — ${d.why}` : ""}
                                            </span>
                                        ))}
                                    </span>
                                )}</>
                            : <span className="text-[#52525B]">AI headline scoring warming up (Reuters / Bloomberg / FOMC / CPI / NFP)</span>}
                    </AgentRow>

                    <AgentRow icon={CalendarClock} color="#FB923C" label="CALENDAR INTEL" testid={`posture-calendar-${sym}`}>
                        {s.calendar_intel
                            ? <><strong>{s.calendar_intel.title}</strong>
                                {" "}{s.calendar_intel.minutes_to >= 0
                                    ? `in ${s.calendar_intel.minutes_to >= 60 ? `${Math.floor(s.calendar_intel.minutes_to / 60)}h ${s.calendar_intel.minutes_to % 60}m` : `${s.calendar_intel.minutes_to}m`}`
                                    : `${Math.abs(s.calendar_intel.minutes_to)}m ago`}
                                {" "}— most likely <strong style={{ color: s.calendar_intel.top === "fakeout" ? "#FF3B30" : s.calendar_intel.top === "breakout" ? "#00FF41" : "#FFB000" }}>
                                    {s.calendar_intel.top} ({Math.round(s.calendar_intel.top_p * 100)}%)</strong>
                                <span className="block font-mono text-[10px] text-[#FB923C] mt-0.5" data-testid={`posture-calendar-probs-${sym}`}>
                                    {["breakout", "fakeout", "reversal", "continuation"].map(k =>
                                        `${k} ${Math.round((s.calendar_intel.probs?.[k] || 0) * 100)}%`).join(" · ")}
                                    {s.calendar_intel.learned_outcomes > 0 ? ` · learned from ${s.calendar_intel.learned_outcomes} real outcomes` : " · priors only (learning)"}
                                </span>
                                {(s.calendar_intel.context_drivers || []).length > 0 && (
                                    <span className="block text-[10px] text-[#A1A1AA]">
                                        {s.calendar_intel.context_drivers.map((d, i) => <span key={i} className="block">→ {d}</span>)}
                                    </span>
                                )}</>
                            : <span className="text-[#52525B]">no high-impact event in the next 24h</span>}
                    </AgentRow>

                    <AgentRow icon={Cpu} color="#06B6D4" label="QUANT" testid={`posture-quant-${sym}`}>
                        Last read: <strong>{s.action || "—"}</strong>{s.confidence ? ` @ ${s.confidence}%` : ""}
                        {s.range_forecast?.remaining_range != null
                            ? ` · day range ${s.range_forecast.used_pct}% used, ~${s.range_forecast.remaining_range} left`
                            : s.range_forecast?.atr14 ? ` · ATR14 ${s.range_forecast.atr14}` : ""}
                        {s.intraday_momentum?.change_pct != null ? ` · intraday ${s.intraday_momentum.change_pct > 0 ? "+" : ""}${s.intraday_momentum.change_pct}%` : ""}
                        {s.bayes && (
                            <span className="block font-mono text-[10px] text-[#06B6D4] mt-0.5" data-testid={`posture-bayes-${sym}`}>
                                {["BUY", "SELL"].map(a => {
                                    const b = s.bayes[a];
                                    if (!b) return null;
                                    return `${a}: P ${Math.round(b.p_success * 100)}% · +${b.expected_reward_r}R / -${b.expected_loss_r}R · EV ${b.ev_r > 0 ? "+" : ""}${b.ev_r}R (${b.quality})`;
                                }).filter(Boolean).join("   |   ")}
                            </span>
                        )}
                    </AgentRow>

                    <AgentRow icon={TrendingUp} color="#38BDF8" label="FORECAST" testid={`posture-forecast-${sym}`}>
                        {s.forecast
                            ? <>Next {s.forecast.horizon} {s.forecast.source === "M15" ? "×15min" : "days"} (Chronos):
                                {" "}median <strong style={{ color: s.forecast.median_change_pct >= 0 ? "#00FF41" : "#FF3B30" }}>
                                    {s.forecast.median_change_pct > 0 ? "+" : ""}{s.forecast.median_change_pct}%</strong>,
                                {" "}80% band [{s.forecast.band_low_pct > 0 ? "+" : ""}{s.forecast.band_low_pct}% … {s.forecast.band_high_pct > 0 ? "+" : ""}{s.forecast.band_high_pct}%]
                                {s.forecast.distribution?.scenarios && (
                                    <span className="block font-mono text-[10px] text-[#38BDF8] mt-0.5" data-testid={`posture-scenarios-${sym}`}>
                                        {s.forecast.distribution.scenarios.map(sc =>
                                            `${Math.round(sc.prob * 100)}%: ${sc.pips > 0 ? "+" : ""}${sc.pips} pips`).join("  ·  ")}
                                        {"  →  EV "}{s.forecast.distribution.ev_pips_long > 0 ? "+" : ""}{s.forecast.distribution.ev_pips_long} pips (long)
                                    </span>
                                )}</>
                            : <span className="text-[#52525B]">forecast model warming up</span>}
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

            <AgentRow icon={Cpu} color="#F97316" label="RL POLICY" testid="posture-rl">
                {data.rl_policy
                    ? <>Learned from <strong>{data.rl_policy.trades_used}</strong> real trades ·
                        {" "}{data.rl_policy.states_learned} market states ·
                        {" "}<span className={data.rl_policy.negative_states > 0 ? "text-[#FFB000]" : ""}>{data.rl_policy.negative_states} reliably-losing setup{data.rl_policy.negative_states === 1 ? "" : "s"} identified</span>
                        {" "}(reward = return − risk − drawdown)</>
                    : <span className="text-[#52525B]">policy trains on the next bot cycle</span>}
            </AgentRow>
        </div>
    );
}
