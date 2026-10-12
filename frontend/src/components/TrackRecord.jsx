import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Loader2 } from "lucide-react";

const BADGE = { tp: ["TP", "bg-[#00FF41]/15 text-[#00FF41] border-[#00FF41]/40"], sl: ["SL", "bg-[#FF3B30]/15 text-[#FF3B30] border-[#FF3B30]/40"],
    expired: ["EXPIRED", "bg-white/5 text-[#A1A1AA] border-white/10"], pending: ["PENDING", "bg-[#FFB000]/15 text-[#FFB000] border-[#FFB000]/40"], no_data: ["NO DATA", "bg-white/5 text-[#52525B] border-white/10"] };
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);

function Outcome({ row }) {
    const [label, cls] = BADGE[row.outcome] || BADGE.no_data;
    return <span className={`inline-flex items-center gap-1 border px-1.5 py-0.5 text-[10px] font-mono ${cls}`} data-testid={`track-outcome-${row.decision_id}`}>{label}{row.bars != null && row.outcome !== "no_data" ? <span className="opacity-70">· {row.bars}b</span> : null}</span>;
}

function CalibrationChart({ buckets }) {
    const rows = (buckets || []).filter((b) => b.n >= 5);
    if (!rows.length) return <div className="text-[11px] text-[#52525B] font-mono" data-testid="track-chart-empty">Calibration chart appears once a confidence bucket has 5+ decided calls.</div>;
    return (
        <div className="space-y-1.5" data-testid="track-calibration-chart">
            {rows.map((b) => {
                const mid = (parseInt(b.bucket.split("-")[0], 10) + parseInt(b.bucket.split("-")[1], 10)) / 200;
                return (
                    <div key={b.bucket} className="grid grid-cols-[3rem_1fr_3.5rem] items-center gap-2 text-[11px] font-mono">
                        <span className="text-[#A1A1AA]">{b.bucket}</span>
                        <div className="relative h-3 bg-white/5">
                            <div className="absolute inset-y-0 left-0 bg-[#FFD700]/70" style={{ width: `${Math.round(b.hit_rate * 100)}%` }} />
                            <div className="absolute inset-y-0 w-px bg-white/60" style={{ left: `${Math.round(mid * 100)}%` }} title="perfectly calibrated" />
                        </div>
                        <span className="text-white text-right">{pct(b.hit_rate)} <span className="text-[#52525B]">n{b.n}</span></span>
                    </div>
                );
            })}
            <div className="text-[10px] text-[#52525B]">bar = hit rate · white tick = where a perfectly calibrated bot would be</div>
        </div>
    );
}

/** Spec A screen — Bot → Track record. */
export function TrackRecord({ symbols = ["XAUUSD"] }) {
    const [sym, setSym] = useState(symbols[0] || "XAUUSD");
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(() => { setData(null); api.get("/trades/predictions", { params: { symbol: sym, limit: 30 } }).then((r) => setData(r.data)).catch((e) => setErr(formatApiError(e))); }, [sym]);
    const c = data?.calibration;
    const rej = c?.by_status?.rejected;
    const prov = data?.provenance;
    const cnt = prov?.counts;
    return (
        <div className="space-y-4" data-testid="track-record-tab">
            <div className="flex flex-wrap items-center gap-3">
                <select value={sym} onChange={(e) => setSym(e.target.value)} data-testid="track-symbol-picker" className="bg-black border border-[#1F1F1F] px-2 py-1.5 text-xs font-mono text-white">
                    {(symbols.length ? symbols : ["XAUUSD"]).map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
                <span className="text-xs text-[#A1A1AA]">Every BUY/SELL the bot considered, checked against what price did next ({data?.horizon_hours ?? 24} h).</span>
            </div>
            {data && (
                <div className="border border-[#FFB000]/30 bg-[#FFB000]/5 px-3 py-2 text-[11px] font-mono text-[#A1A1AA] space-y-0.5" data-testid="track-provenance">
                    <div className="text-[#FFB000] font-bold tracking-wide" data-testid="track-provenance-label">{prov?.label || "Prediction calibration — not broker performance"}</div>
                    <div>source <span className="text-white" data-testid="track-source">{prov?.source || "M15 candles"}</span> · as of <span className="text-white" data-testid="track-as-of">{prov?.as_of ? String(prov.as_of).slice(0, 16).replace("T", " ") + " UTC" : "no candles yet"}</span> · horizon <span className="text-white" data-testid="track-horizon">{data.horizon_hours ?? 24} h</span> ({data.horizon_bars} bars)</div>
                    <div>same-bar rule: <span className="text-white" data-testid="track-same-bar-rule">{prov?.same_bar_rule || "SL counted first"}</span></div>
                    <div data-testid="track-counts">decided <span className="text-white">{cnt?.decided ?? c?.decided ?? 0}</span> · excluded (no data) <span className="text-white">{cnt?.excluded_no_data ?? 0}</span> · expired <span className="text-white">{cnt?.expired ?? 0}</span> · pending <span className="text-white">{cnt?.pending ?? data.pending}</span></div>
                </div>
            )}
            {err && <div className="text-xs text-[#FF3B30] font-mono" data-testid="track-error">{err}</div>}
            {!data && !err && <div className="text-xs text-[#71717A] font-mono flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> scoring…</div>}
            {data && (
                <>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-2" data-testid="track-summary">
                        <div className="border border-[#1F1F1F] p-3"><div className="text-[10px] font-mono text-[#71717A]">HIT RATE</div>
                            <div className="text-lg text-white font-bold" data-testid="track-hit-rate">{c.decided ? `${pct(c.hit_rate)} (${pct(c.ci95[0])}–${pct(c.ci95[1])})` : "—"}</div>
                            <div className="text-[11px] text-[#A1A1AA]">of {c.decided} decided</div></div>
                        <div className="border border-[#1F1F1F] p-3"><div className="text-[10px] font-mono text-[#71717A]">PENDING</div>
                            <div className="text-lg text-white font-bold" data-testid="track-pending">{data.pending}</div><div className="text-[11px] text-[#A1A1AA]">waiting for 24 h of candles</div></div>
                        <div className="border border-[#1F1F1F] p-3"><div className="text-[10px] font-mono text-[#71717A]">REJECTED THAT WOULD HAVE WON</div>
                            <div className="text-lg text-white font-bold" data-testid="track-rejected-won">{rej ? pct(rej.hit_rate) : "—"}</div><div className="text-[11px] text-[#A1A1AA]">{rej ? `${rej.tp} of ${rej.n} rejected` : "no rejected calls scored"}</div></div>
                    </div>
                    <CalibrationChart buckets={c.by_confidence} />
                    {data.recent.length === 0 ? (
                        <div className="border border-dashed border-[#1F1F1F] p-6 text-center text-sm text-[#A1A1AA]" data-testid="track-empty">No scored predictions yet — results appear after the bot's first signals have had 24 h of candles.</div>
                    ) : (
                        <ul className="divide-y divide-[#141414]" data-testid="track-table">
                            {data.recent.map((r) => (
                                <li key={r.decision_id} className="py-2 grid grid-cols-2 sm:grid-cols-[9rem_3.5rem_1fr_4rem_5rem_7rem] gap-x-3 gap-y-1 items-center text-[11px] font-mono" data-testid={`track-row-${r.decision_id}`}>
                                    <span className="text-[#A1A1AA]">{String(r.ts || "").slice(5, 16).replace("T", " ")}</span>
                                    <span className={r.action === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}>{r.action}</span>
                                    <span className="text-white col-span-2 sm:col-span-1">{r.entry} <span className="text-[#52525B]">SL</span> {r.stop_loss} <span className="text-[#52525B]">TP</span> {r.take_profit}</span>
                                    <span className="text-[#A1A1AA]">conf {r.confidence ?? "—"}</span>
                                    <span className="text-[#71717A]">{r.status}</span>
                                    <span className="justify-self-start sm:justify-self-end"><Outcome row={r} /></span>
                                </li>
                            ))}
                        </ul>
                    )}
                </>
            )}
        </div>
    );
}
