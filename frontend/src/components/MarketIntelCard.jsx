import { useCallback, useEffect, useState } from "react";
import { Brain, ExternalLink, RefreshCw } from "lucide-react";
import api from "@/lib/api";

// #41 — MARKET INTELLIGENCE (display only): everything the bot knows about the market + how it uses it.
// Reads GET /api/market-intel every 60 s. No trading effect. Each section degrades to "not available".

const GREY = "#52525B", AMBER = "#FFB000", GREEN = "#00FF41", RED = "#FF3B30", MUTED = "#A1A1AA";
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC" : "—");
const fmtDay = (iso) => (iso ? new Date(iso).toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short", timeZone: "UTC" }) : "");
const ago = (iso) => {
    if (!iso) return "—";
    const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    return m < 1 ? "just now" : m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
};
const countdown = (sec) => {
    if (sec == null) return "";
    if (sec < 0) return `${Math.round(-sec / 60)}m ago`;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return sec >= 86400 ? `in ${Math.floor(sec / 86400)}d ${h % 24}h` : h ? `in ${h}h ${m}m` : `in ${m}m`;
};
const scoreColor = (s) => (s == null ? GREY : s >= 65 ? RED : s >= 35 ? AMBER : GREEN);
const regimeColor = (r) => ({ stressed: RED, elevated: AMBER, calm: GREEN, normal: MUTED }[r] || GREY);
const statusColor = (s) => ({ ok: GREEN, stale: AMBER, error: RED, unavailable: AMBER, not_configured: GREY }[s] || GREY);

function Section({ title, children, testid, right }) {
    return (
        <div className="border-t border-[#1F1F1F] px-4 py-3" data-testid={testid}>
            <div className="flex items-center gap-2 mb-2">
                <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{title}</div>
                {right && <div className="ml-auto font-mono text-[9px] text-[#52525B]">{right}</div>}
            </div>
            {children}
        </div>
    );
}

const NA = ({ note }) => <div className="font-mono text-[10px] text-[#52525B]" data-testid="intel-not-available">{note || "not available"}</div>;

function NextEvents({ d }) {
    if (!d?.available) return <NA note={d?.note} />;
    return (
        <div className="space-y-1.5">
            {d.freeze && (
                <div className="font-mono text-[10px] px-2 py-1 inline-block border" data-testid="intel-freeze-line"
                     style={{ color: d.freeze_active ? RED : AMBER, borderColor: (d.freeze_active ? RED : AMBER) + "66" }}>
                    {d.freeze}
                </div>
            )}
            {d.events.length === 0 && <NA note="no high-impact USD/EUR/XAU event in the window" />}
            {d.events.map((e, i) => (
                <div key={i} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 font-mono text-[10px]" data-testid={`intel-event-${i}`}>
                    <span className="text-white tabular-nums">{fmtDay(e.time_utc)} {fmtTime(e.time_utc)}</span>
                    <span style={{ color: e.released ? GREEN : AMBER }}>{e.released ? "RELEASED" : countdown(e.countdown_sec)}</span>
                    <span className="text-[#FFB000] font-bold">{e.currency}</span>
                    <span className="text-[#A1A1AA]">{e.event}</span>
                    <span className="text-[#52525B]">fcst {e.forecast || "—"} · prev {e.previous || "—"}</span>
                    {e.released && <span className="text-white">actual {e.actual}{e.surprise?.pct != null ? ` · surprise ${e.surprise.pct > 0 ? "+" : ""}${e.surprise.pct}%` : e.surprise?.z != null ? ` · z ${e.surprise.z}` : ""}</span>}
                </div>
            ))}
            <div className="font-mono text-[9px] text-[#52525B]" data-testid="intel-calendar-source">
                source {(d.calendar_source || "none").toUpperCase()} · {d.calendar_status || "?"} · refreshed {ago(d.last_refresh)}{d.failsafe_active ? " · FAIL-SAFE" : ""}
            </div>
        </div>
    );
}

function Gauge({ label, net, count }) {
    const v = net == null ? 0 : Math.max(-3, Math.min(3, net));
    const pct = ((v + 3) / 6) * 100;
    return (
        <div className="min-w-[140px]" data-testid={`intel-bias-${label}`}>
            <div className="flex justify-between font-mono text-[9px] text-[#52525B]"><span>{label}</span><span>{net == null ? "—" : `${net > 0 ? "+" : ""}${net}/3 · ${count ?? 0} items`}</span></div>
            <div className="h-1.5 bg-[#1F1F1F] relative mt-1">
                <div className="absolute top-0 bottom-0 w-px bg-[#52525B]" style={{ left: "50%" }} />
                <div className="absolute top-0 bottom-0 w-1.5 -ml-0.5" style={{ left: `${pct}%`, background: net == null ? GREY : v > 0.5 ? GREEN : v < -0.5 ? RED : MUTED }} />
            </div>
        </div>
    );
}

function Headlines({ d }) {
    return (
        <div className="space-y-2">
            <div className="flex flex-wrap gap-4">
                {Object.entries(d?.bias || {}).map(([k, b]) => <Gauge key={k} label={k} net={b.net} count={b.headlines} />)}
            </div>
            {!d?.available && <NA note={d?.note} />}
            {d?.available && d.headlines.length === 0 && <NA note={`no scored headline in the last ${d.window_hours} h`} />}
            {(d?.headlines || []).map((h, i) => (
                <div key={i} className="font-mono text-[10px] leading-snug" data-testid={`intel-headline-${i}`}>
                    <span className="text-[#52525B]">{fmtTime(h.time)} · {h.source}</span>{" "}
                    {h.url ? <a href={h.url} target="_blank" rel="noreferrer" className="text-white hover:text-[#00FF41]">{h.title} <ExternalLink className="inline w-2.5 h-2.5" /></a> : <span className="text-white">{h.title}</span>}
                    <div className="text-[9px] text-[#A1A1AA]">
                        {Object.entries(h.direction || {}).map(([sym, dir]) => (
                            <span key={sym} className="mr-2"><span className="text-[#FFB000]">{sym}</span> <span style={{ color: dir === "bullish" ? GREEN : dir === "bearish" ? RED : MUTED }}>{dir}</span></span>
                        ))}
                        relevance {h.relevance} · urgency <span style={{ color: h.urgency === "high" ? AMBER : MUTED }}>{h.urgency}</span>{h.why ? ` · ${h.why}` : ""}
                    </div>
                </div>
            ))}
        </div>
    );
}

function CentralBanks({ d }) {
    if (!d?.available) return <NA note={d?.note} />;
    return (
        <div className="space-y-1">
            {d.banks.map((b) => (
                <div key={b.bank} className="font-mono text-[10px] flex flex-wrap gap-x-2 items-baseline" data-testid={`intel-cb-${b.bank}`}
                     style={{ color: b.recent ? AMBER : MUTED }}>
                    <span className="text-white w-8">{b.bank}</span>
                    <span className="text-[#52525B]">{fmtDay(b.time)} {fmtTime(b.time)}</span>
                    {b.recent && <span className="text-[9px] border border-[#FFB000]/50 px-1">LAST 60 MIN</span>}
                    {b.kind === "decision" && <span className="text-[9px] text-[#FFB000]">DECISION</span>}
                    <a href={b.link} target="_blank" rel="noreferrer" className="hover:text-white truncate max-w-full">{b.title}</a>
                </div>
            ))}
        </div>
    );
}

function Tile({ id, label, value, sub, regime, stale }) {
    return (
        <div className="border border-[#1F1F1F] p-2 min-w-[120px]" data-testid={`intel-gauge-${id}`} style={{ opacity: stale ? 0.55 : 1 }}>
            <div className="flex justify-between font-mono text-[9px] text-[#52525B] tracking-widest">
                <span>{label}</span>{stale && <span className="text-[#FFB000]" data-testid={`intel-gauge-${id}-old`}>OLD DATA</span>}
            </div>
            <div className="font-display font-bold text-lg tabular-nums text-white">{value ?? "—"}</div>
            <div className="font-mono text-[9px]" style={{ color: regimeColor(regime) }}>{regime || sub || "—"}</div>
            {regime && sub && <div className="font-mono text-[9px] text-[#52525B]">{sub}</div>}
        </div>
    );
}

function Macro({ m }) {
    if (!m) return <NA />;
    const chg = (v) => (v == null ? null : `${v > 0 ? "+" : ""}${v} 5d`);
    return (
        <div className="flex flex-wrap gap-2">
            <Tile id="dxy" label="DXY" value={m.dxy?.value} regime={m.dxy?.regime?.replace(/_/g, " ")} sub={m.dxy?.age_hours != null ? `${m.dxy.age_hours}h old` : m.dxy?.note} stale={m.dxy?.stale} />
            <Tile id="real-yield" label="US 10Y REAL (DFII10)" value={m.real_yield_10y?.value != null ? `${m.real_yield_10y.value}%` : null} regime={m.real_yield_10y?.regime?.replace(/_/g, " ")} sub={chg(m.real_yield_10y?.change_5d) || m.real_yield_10y?.note} stale={m.real_yield_10y?.stale} />
            <Tile id="gvz" label="GVZ" value={m.gvz?.value} regime={m.gvz?.regime} sub={chg(m.gvz?.change_5d) || m.gvz?.date || m.gvz?.note} stale={m.gvz?.stale} />
            <Tile id="vix" label="VIX" value={m.vix?.value} regime={m.vix?.regime} sub={chg(m.vix?.change_5d) || m.vix?.date || m.vix?.note} stale={m.vix?.stale} />
        </div>
    );
}

function Sparkline({ pts }) {
    if (!pts?.length) return null;
    const vals = pts.map((p) => Number(p.v) || 0), w = 140, h = 28, max = Math.max(100, ...vals);
    const d = vals.map((v, i) => `${(i / Math.max(1, vals.length - 1)) * w},${h - (v / max) * h}`).join(" ");
    return <svg width={w} height={h} data-testid="intel-geo-sparkline"><polyline fill="none" stroke={AMBER} strokeWidth="1.5" points={d} /></svg>;
}

function Geo({ g }) {
    if (!g?.available) return <div><NA note={g?.note} /><Sparkline pts={g?.sparkline} /></div>;
    return (
        <div className="flex flex-wrap items-center gap-4">
            <div className="font-display font-bold text-2xl tabular-nums" style={{ color: scoreColor(g.score) }} data-testid="intel-geo-score">{g.score}</div>
            <Sparkline pts={g.sparkline} />
            <div className="font-mono text-[10px] text-[#A1A1AA]">
                <div className="text-[9px] text-[#52525B] tracking-widest">TOP REGIONS</div>
                {(g.top_regions || []).map((r) => <div key={r}>{r.replace(/_/g, " ")} · {g.regions?.[r]?.score ?? g.regions?.[r]?.z ?? "—"}</div>)}
                <div className="text-[#52525B]">7-day sparkline · updated {ago(g.at)}</div>
            </div>
        </div>
    );
}

function Regime({ r }) {
    return (
        <div className="flex flex-wrap items-center gap-4">
            <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border" data-testid="intel-regime-label"
                  style={{ color: r?.gate_enabled ? RED : AMBER, borderColor: (r?.gate_enabled ? RED : AMBER) + "66" }}>{r?.label}</span>
            {!r?.available ? <NA note={r?.note} /> : (
                <>
                    <div className="font-display font-bold text-2xl tabular-nums" style={{ color: scoreColor(r.score) }} data-testid="intel-regime-score">{r.score} <span className="font-mono text-[9px] text-[#52525B] uppercase">{r.bucket}</span></div>
                    <div className="font-mono text-[10px] text-[#A1A1AA]">
                        {Object.entries(r.components || {}).map(([k, v]) => <span key={k} className="mr-3">{k.replace(/_/g, " ")} <span className="text-white">{v ?? "—"}</span></span>)}
                        <div className="text-[#52525B]">updated {ago(r.at)}{r.missing?.length ? ` · missing: ${r.missing.join(", ")}` : ""}</div>
                    </div>
                </>
            )}
        </div>
    );
}

const OnOff = ({ on, label }) => <span className="font-mono text-[9px] px-1 border" style={{ color: on ? AMBER : GREY, borderColor: (on ? AMBER : GREY) + "66" }}>{label} {on ? "ON" : "OFF"}</span>;

function Usage({ rows }) {
    if (!rows?.length) return <NA />;
    return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {rows.map((u) => (
                <div key={u.instrument} className="font-mono text-[10px] space-y-1 border border-[#1F1F1F] p-2" data-testid={`intel-usage-${u.instrument}`}>
                    <div className="text-[#FFB000] font-bold tracking-widest">{u.instrument}</div>
                    {u.available === false ? <NA note={u.note} /> : (
                        <>
                            <div><OnOff on={u.macro_freeze?.on} label="macro freeze" /> <span className="text-[#A1A1AA]">{u.macro_freeze?.why}</span></div>
                            <div><OnOff on={u.avalanche_guard?.on} label="avalanche guard" /> <span className="text-[#A1A1AA]">{u.avalanche_guard?.state}</span></div>
                            <div><span className="text-[#52525B]">news bias → Strategy agent:</span> <span className="text-white">{u.news_bias?.net == null ? "none" : `${u.news_bias.net > 0 ? "+" : ""}${u.news_bias.net} ${u.news_bias.label}`}</span>
                                {u.news_bias?.gate && <span className="text-[#A1A1AA]"> · gate BUY {u.news_bias.gate.BUY === "pass" ? "pass" : "VETO"} / SELL {u.news_bias.gate.SELL === "pass" ? "pass" : "VETO"}</span>}
                                {u.news_bias?.note && <span className="text-[#52525B]"> {u.news_bias.note}</span>}</div>
                            {u.gold_windows && (
                                <div><OnOff on={u.gold_windows.off_hours?.on} label="gold off-hours" /> <span className="text-[#52525B]">{u.gold_windows.off_hours?.window_utc}</span>{" "}
                                    <OnOff on={u.gold_windows.asia_skip?.on} label="Asia skip" /> <span className="text-[#52525B]">{u.gold_windows.asia_skip?.enabled ? u.gold_windows.asia_skip.window_utc : "disabled"}</span></div>
                            )}
                            <div className="text-[#52525B]">last decision citing these: {u.last_decision ? <span className="text-[#A1A1AA]">{fmtDay(u.last_decision.time)} {fmtTime(u.last_decision.time)} · {u.last_decision.action} · {u.last_decision.reason}</span> : "none"}</div>
                        </>
                    )}
                </div>
            ))}
        </div>
    );
}

function Sources({ rows }) {
    if (!rows?.length) return <NA />;
    return (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1">
            {rows.map((r) => (
                <div key={r.feed} className="font-mono text-[10px] flex items-baseline gap-2" data-testid={`intel-source-${r.feed.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`}>
                    <span className="w-2 h-2 inline-block shrink-0 rounded-full" style={{ background: statusColor(r.status) }} />
                    <span className="text-white w-40 truncate">{r.feed}</span>
                    <span style={{ color: statusColor(r.status) }}>{r.status.replace("_", " ")}</span>
                    <span className="text-[#52525B] truncate">{r.last_success ? ago(r.last_success) : ""}{r.items_last_hour != null ? ` · ${r.items_last_hour}/h` : ""}{r.note ? ` · ${r.note}` : ""}</span>
                </div>
            ))}
        </div>
    );
}

export function MarketIntelCard({ compact = false }) {
    const [d, setD] = useState(null);
    const [err, setErr] = useState(null);
    const load = useCallback(() => {
        api.get("/market-intel").then((r) => { setD(r.data); setErr(null); }).catch(() => setErr("unavailable"));
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid={compact ? "market-intel-card-compact" : "market-intel-card"}>
            <div className="px-4 py-3 flex items-center gap-2 flex-wrap">
                <Brain className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Market Intelligence</span>
                <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#1F1F1F] text-[#52525B]">DISPLAY ONLY · NO TRADING EFFECT</span>
                <span className="ml-auto font-mono text-[9px] text-[#52525B]">{d ? `updated ${ago(d.generated_at)} · 60 s` : err || "loading…"}</span>
                <button onClick={load} className="text-[#52525B] hover:text-white" data-testid="market-intel-refresh" aria-label="Refresh"><RefreshCw className="w-3 h-3" /></button>
            </div>
            {err && !d && <div className="px-4 pb-3 font-mono text-[10px] text-[#FF3B30]" data-testid="market-intel-error">{err}</div>}
            {d && (
                <>
                    <Section title="NEXT EVENTS" testid="intel-next-events"><NextEvents d={d.next_events} /></Section>
                    <Section title="RISK REGIME (ADVISORY)" testid="intel-risk-regime"><Regime r={d.risk_regime} /></Section>
                    {d.gold_trend_pulse?.available && <Section title="GOLD TREND PULSE · DAILY BIAS" testid="intel-gold-trend-pulse">
                        {(d.gold_trend_pulse.accounts || []).map((a) => (
                            <div key={a.account_id} className="text-xs mb-2" data-testid={`intel-gtp-${a.account_id}`}>
                                <span className="text-white/60">{a.account || a.account_id.slice(-6)} · </span>
                                <span className={`font-mono font-bold ${a.bias === "UP" ? "text-[#10F2C5]" : a.bias === "DOWN" ? "text-[#FF5E5B]" : "text-white/50"}`}>{a.bias}</span>
                                <span className="text-white/50"> · regime {a.regime} · {a.mode}</span>
                                {a.news && <div className="text-[10px] text-[#F5C542]" data-testid={`intel-gtp-news-${a.account_id}`}>{a.news.phase ? `[${a.news.phase}] ` : ""}{a.news.plan}{a.news.forecast ? ` · fcst ${a.news.forecast}` : ""}{a.news.actual ? ` · actual ${a.news.actual}` : ""} · PRE: {a.news.pre_action}</div>}
                                <ul className="text-[10px] text-white/50 ml-3">{(a.reasons || []).map((r, i) => <li key={i}>{r.ok ? "✓" : "✗"} {r.text}</li>)}</ul>
                            </div>))}
                    </Section>}
                    {!compact && <Section title="TOP HEADLINES" testid="intel-headlines" right={`last ${d.headlines?.window_hours || 4} h`}><Headlines d={d.headlines} /></Section>}
                    {compact && <Section title="NEWS BIAS" testid="intel-headlines"><div className="flex flex-wrap gap-4">{Object.entries(d.headlines?.bias || {}).map(([k, b]) => <Gauge key={k} label={k} net={b.net} count={b.headlines} />)}</div></Section>}
                    {!compact && <Section title="CENTRAL BANKS" testid="intel-central-banks"><CentralBanks d={d.central_banks} /></Section>}
                    <Section title="MACRO GAUGES" testid="intel-macro"><Macro m={d.macro} /></Section>
                    {!compact && <Section title="GEOPOLITICAL RISK (GDELT)" testid="intel-geo"><Geo g={d.geo_risk} /></Section>}
                    <Section title="HOW THE BOT IS USING THIS" testid="intel-usage"><Usage rows={d.usage} /></Section>
                    {!compact && <Section title="SOURCE HEALTH" testid="intel-sources"><Sources rows={d.source_health} /></Section>}
                </>
            )}
        </div>
    );
}

export default MarketIntelCard;
