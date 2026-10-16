import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";

const fmt = (v, d = 2) => (v === null || v === undefined ? "—" : typeof v === "number" ? v.toFixed(d) : String(v));
const BIAS_COLOR = { UP: "text-[#10F2C5]", DOWN: "text-[#FF5E5B]", NONE: "text-white/50" };
const REGIME_COLOR = { TREND: "text-[#10F2C5]", RANGE: "text-[#F5C542]", NO_TRADE: "text-white/50" };

function Stats({ s, label }) {
    if (!s) return null;
    return (
        <div className="grid grid-cols-3 md:grid-cols-6 gap-2 text-xs text-[#A1A1AA]" data-testid={`gtp-stats-${label}`}>
            <div><div className="text-[9px] uppercase">{label} trades</div><div className="text-white">{s.n ?? 0}{s.decisions !== undefined ? ` / ${s.decisions} dec.` : ""}</div></div>
            <div><div className="text-[9px] uppercase">win rate</div><div className="text-white">{s.win_rate === null || s.win_rate === undefined ? "—" : `${Math.round(s.win_rate * 100)}%`}</div></div>
            <div><div className="text-[9px] uppercase">avg win / loss $</div><div className="text-white">{fmt(s.avg_win_usd)} / {fmt(s.avg_loss_usd)}</div></div>
            <div><div className="text-[9px] uppercase">profit factor</div><div className="text-white">{fmt(s.profit_factor)}</div></div>
            <div><div className="text-[9px] uppercase">expectancy (costs in)</div><div className="text-white">{s.expectancy_r === undefined ? "—" : `${s.expectancy_r > 0 ? "+" : ""}${fmt(s.expectancy_r, 3)} R`}</div></div>
            <div><div className="text-[9px] uppercase">max DD</div><div className="text-white">{fmt(s.max_dd_r)} R</div></div>
        </div>
    );
}

export function NewsPlan({ news, runner }) {
    if (!news) return null;
    const n = news.next;
    const ev = news.recent || [];
    return (
        <div className="mt-3 text-[11px] font-mono border-t border-white/5 pt-2" data-testid="gtp-news">
            <div className="flex flex-wrap gap-4">
                <span><span className="text-white/50">news mode </span><span className={news.enabled ? "text-[#10F2C5]" : "text-white/50"}>{news.enabled ? `ON · pre: ${news.pre_action} · release ${news.release_min} min` : "OFF"}</span></span>
                <span><span className="text-white/50">sweep runner </span><span className={runner ? "text-[#10F2C5]" : "text-white/50"}>{runner ? "ON (½ size → pre-drop high)" : "OFF"}</span></span>
            </div>
            <div className="text-[#F5C542] mt-1" data-testid="gtp-news-next">
                {n ? `${n.phase ? `[${n.phase}] ` : ""}${n.plan}${n.forecast ? ` · fcst ${n.forecast}` : ""}${n.actual ? ` · actual ${n.actual}` : ""}` : "no configured USD event in the next window"}
            </div>
            {ev.length > 0 && (
                <table className="w-full text-[10px] text-white/70 mt-2" data-testid="gtp-news-events">
                    <thead><tr className="text-white/40 text-left"><th>event</th><th>UTC</th><th>surprise</th><th>bias</th><th>1st move</th><th>confirmed</th><th>trades</th><th>skipped / PRE</th></tr></thead>
                    <tbody>{ev.slice(0, 6).map((e) => (
                        <tr key={e.key} className="border-t border-white/5">
                            <td>{e.title}</td><td>{String(e.time_utc || "").slice(5, 16)}</td><td>{e.surprise ?? "—"}</td><td>{e.news_bias || "—"}</td><td>{e.first_move || "—"}</td>
                            <td>{e.confirmed === true ? "yes" : e.confirmed === false ? "no" : "—"}</td><td>{e.trades ?? 0}</td>
                            <td>{[...(e.skipped || []).slice(0, 2), ...(e.pre_actions || []).map((a) => `PRE ${a.action}`).slice(0, 2)].join("; ")}</td>
                        </tr>))}</tbody>
                </table>
            )}
        </div>
    );
}

export function GoldTrendPulseCard({ accountId }) {
    const [d, setD] = useState(null);
    const [err, setErr] = useState(null);
    const load = useCallback(async () => {
        if (!accountId) return;
        try { const { data } = await api.get(`/gold-trend-pulse/card?account_id=${accountId}`); setD(data); setErr(null); }
        catch (e) { setErr(e?.response?.data?.detail || "card unavailable"); }
    }, [accountId]);
    useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id); }, [load]);
    if (!accountId) return null;
    const gate = d?.live_gate || {};
    return (
        <div className="rounded-lg border border-white/10 bg-[#0B0F14] p-4 mb-6" data-testid="gold-trend-pulse-card">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
                <div className="font-mono text-xs text-[#F5C542]">GOLD TREND PULSE · XAUUSD M5 scalper {d?.layering_enabled ? "· LAYERED" : ""}</div>
                <div className="font-mono text-[10px] text-white/70" data-testid="gtp-mode">
                    {d ? (d.enabled ? `${d.mode.toUpperCase()}${d.requested_mode !== d.mode ? ` (demo_live requested — gate closed)` : ""}` : "OFF") : "…"}
                </div>
            </div>
            {err && <div className="text-xs text-[#FF5E5B]" data-testid="gtp-error">{err}</div>}
            {d && (
                <>
                    <div className="flex flex-wrap gap-6 mb-3 text-xs">
                        <div data-testid="gtp-bias">
                            <span className="text-white/50">daily bias </span>
                            <span className={`font-mono font-bold ${BIAS_COLOR[d.bias?.bias] || ""}`}>{d.bias?.bias || "NONE"}</span>
                            {d.bias?.candidate && d.bias.candidate !== d.bias.bias && <span className="text-white/40"> → {d.bias.candidate} ({d.bias.confirmations}/2)</span>}
                        </div>
                        <div data-testid="gtp-regime"><span className="text-white/50">regime </span><span className={`font-mono font-bold ${REGIME_COLOR[d.regime] || ""}`}>{d.regime}</span>
                            {d.range && <span className="text-white/40"> {d.range.lo}–{d.range.hi}</span>}</div>
                        {d.basket && <div data-testid="gtp-basket"><span className="text-white/50">basket </span><span className="font-mono">{d.basket.direction} {d.basket.legs?.length || 0}/3 legs · stop {d.basket.shared_stop}</span></div>}
                        <div className="text-white/40" data-testid="gtp-note">{d.last_note}</div>
                    </div>
                    <ul className="text-[11px] text-white/60 mb-3 grid md:grid-cols-2 gap-x-6" data-testid="gtp-bias-reasons">
                        {(d.bias?.reasons || []).map((r, i) => <li key={i}>{r.ok ? "✓" : "✗"} {r.text}</li>)}
                    </ul>
                    <Stats s={d.today} label="today" />
                    <div className="h-2" />
                    <Stats s={d.week} label="7d" />
                    <div className="mt-3 text-[11px] font-mono" data-testid="gtp-gate">
                        <span className="text-white/50">live gate </span>
                        <span className={gate.eligible ? "text-[#10F2C5]" : "text-[#F5C542]"}>{gate.eligible ? "PASSED" : `${gate.progress ?? 0}/${gate.needed ?? 100} labeled shadow trades`}</span>
                        {!gate.eligible && (gate.reasons || []).length > 0 && <span className="text-white/40"> — {gate.reasons.join(" · ")}</span>}
                    </div>
                    <NewsPlan news={d.news} runner={d.sweep_runner_enabled} />
                    {(d.recent || []).length > 0 && (
                        <div className="mt-3 overflow-x-auto">
                            <table className="w-full text-[10px] font-mono text-white/70" data-testid="gtp-recent">
                                <thead><tr className="text-white/40 text-left"><th>time</th><th>regime</th><th>bias</th><th>trig</th><th>dir</th><th>entry</th><th>SL</th><th>TP</th><th>spread</th><th>risk $</th><th>verdict</th><th>outcome</th><th>R</th><th>MFE/MAE</th><th>min</th><th>exit</th></tr></thead>
                                <tbody>{d.recent.slice(0, 12).map((r) => (
                                    <tr key={r.decision_id} className="border-t border-white/5">
                                        <td>{String(r.created_at).slice(11, 16)}</td><td>{r.regime}</td><td>{r.bias}</td><td>{r.trigger || r.leg || "—"}</td><td>{r.direction}</td>
                                        <td>{fmt(r.entry)}</td><td>{fmt(r.stop_loss)}</td><td>{fmt(r.take_profit)}</td><td>{fmt(r.spread_at_entry)}</td><td>{fmt(r.planned_risk_usd)}</td>
                                        <td>{r.verdict}{r.skip_reason ? ` (${r.skip_reason})` : ""}</td><td>{r.outcome?.result || "open"}</td><td>{fmt(r.outcome?.r)}</td>
                                        <td>{fmt(r.outcome?.mfe)}/{fmt(r.outcome?.mae)}</td><td>{fmt(r.outcome?.time_in_trade_min, 0)}</td><td>{r.outcome?.exit_reason || ""}</td>
                                    </tr>))}</tbody>
                            </table>
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
