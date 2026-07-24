import { useEffect, useRef, useState } from "react";
import api from "@/lib/api";
import { X, Play, Pause, Film } from "lucide-react";

const W = 640, H = 260, PAD = 34;

export const ReplayModal = ({ trade, onClose }) => {
    const [d, setD] = useState(null);
    const [err, setErr] = useState(null);
    const [cursor, setCursor] = useState(0);
    const [playing, setPlaying] = useState(false);
    const raf = useRef(null);

    useEffect(() => {
        api.get(`/trades/${trade.id}/replay`)
            .then(r => { setD(r.data); setCursor((r.data.ticks?.length || 1) - 1); })
            .catch(e => setErr(e?.response?.data?.detail || "replay unavailable"));
    }, [trade.id]);

    useEffect(() => {
        if (!playing || !d?.ticks?.length) return;
        let i = cursor >= d.ticks.length - 1 ? 0 : cursor;
        const step = () => {
            i += Math.max(1, Math.floor(d.ticks.length / 600));
            if (i >= d.ticks.length) { setCursor(d.ticks.length - 1); setPlaying(false); return; }
            setCursor(i);
            raf.current = requestAnimationFrame(step);
        };
        raf.current = requestAnimationFrame(step);
        return () => cancelAnimationFrame(raf.current);
    }, [playing]); // eslint-disable-line react-hooks/exhaustive-deps

    const ticks = d?.ticks || [];
    let chart = null;
    if (ticks.length > 1) {
        const prices = ticks.map(t => t.price);
        const lv = d.levels || {};
        const all = [...prices, lv.entry, lv.stop_loss, lv.tp1].filter(v => v != null).map(Number);
        const lo = Math.min(...all), hi = Math.max(...all);
        const y = v => H - PAD - ((v - lo) / Math.max(hi - lo, 1e-9)) * (H - 2 * PAD);
        const x = i => PAD + (i / (ticks.length - 1)) * (W - 2 * PAD);
        const shown = ticks.slice(0, cursor + 1);
        const pts = shown.map((t, i) => `${x(i)},${y(t.price)}`).join(" ");
        const t0 = ticks[0].t, t1 = ticks[ticks.length - 1].t;
        const xt = ts => PAD + ((ts - t0) / Math.max(t1 - t0, 1)) * (W - 2 * PAD);
        chart = (
            <svg viewBox={`0 0 ${W} ${H}`} className="w-full" data-testid="replay-chart">
                {[["entry", "#FFD700"], ["stop_loss", "#FF3B30"], ["tp1", "#00FF41"]].map(([k, c]) =>
                    lv[k] != null && (
                        <g key={k}>
                            <line x1={PAD} x2={W - PAD} y1={y(lv[k])} y2={y(lv[k])}
                                stroke={c} strokeDasharray="4 4" strokeWidth="0.7" opacity="0.6" />
                            <text x={W - PAD + 2} y={y(lv[k]) + 3} fill={c} fontSize="8"
                                fontFamily="monospace">{k.toUpperCase().replace("_", " ")}</text>
                        </g>
                    ))}
                <polyline points={pts} fill="none" stroke="#38BDF8" strokeWidth="1.2" />
                {(d.markers || []).filter(m => m.t >= t0 && m.t <= t1 && ticks[cursor]?.t >= m.t).map((m, i) => (
                    <g key={i}>
                        <line x1={xt(m.t)} x2={xt(m.t)} y1={PAD} y2={H - PAD}
                            stroke={m.kind === "ENTRY" ? "#FFD700" : m.kind === "EXIT" ? "#FF3B30" : "#A78BFA"}
                            strokeWidth="0.8" opacity="0.8" />
                        <text x={xt(m.t) + 2} y={PAD + 8 + (i % 3) * 9} fontSize="7"
                            fontFamily="monospace" fill="#A1A1AA">{m.kind}</text>
                    </g>
                ))}
                {shown.length > 0 && (
                    <circle cx={x(shown.length - 1)} cy={y(shown[shown.length - 1].price)}
                        r="3" fill="#38BDF8" />
                )}
            </svg>
        );
    }

    const cur = ticks[cursor];
    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
            onClick={onClose} data-testid="replay-modal">
            <div className="w-full max-w-3xl border border-[#1F1F1F] bg-[#0A0A0A]"
                onClick={e => e.stopPropagation()}>
                <div className="flex items-center gap-2 border-b border-[#1F1F1F] px-5 py-3">
                    <Film className="w-4 h-4 text-[#38BDF8]" />
                    <span className="font-display font-bold text-sm text-white">
                        Replay — {trade.action} {trade.symbol}
                    </span>
                    {d && <span className="font-mono text-[9px] tracking-widest text-[#52525B]">
                        {d.source === "ticks" ? `${ticks.length} TICKS` : "M15 FALLBACK"}
                    </span>}
                    <button onClick={onClose} data-testid="replay-close"
                        className="ml-auto text-[#71717A] hover:text-white"><X className="w-4 h-4" /></button>
                </div>
                <div className="p-4">
                    {err && <div className="font-mono text-xs text-[#FF3B30]">{err}</div>}
                    {!d && !err && <div className="p-8 text-center font-mono text-xs text-[#52525B] tracking-widest">LOADING REPLAY…</div>}
                    {d && ticks.length < 2 && (
                        <div className="p-6 text-center font-mono text-xs text-[#71717A]" data-testid="replay-empty">
                            No recorded price path for this trade's window.
                        </div>
                    )}
                    {chart}
                    {d && ticks.length > 1 && (
                        <div className="flex items-center gap-3 mt-2">
                            <button onClick={() => setPlaying(p => !p)} data-testid="replay-play"
                                className="flex items-center gap-1.5 font-mono text-[10px] tracking-widest px-2.5 py-1 border border-[#38BDF8]/40 text-[#38BDF8] hover:bg-[#38BDF8]/10">
                                {playing ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                                {playing ? "PAUSE" : "REPLAY"}
                            </button>
                            <input type="range" min="0" max={ticks.length - 1} value={cursor}
                                onChange={e => { setPlaying(false); setCursor(Number(e.target.value)); }}
                                className="flex-1 accent-[#38BDF8]" data-testid="replay-scrubber" />
                            <span className="font-mono text-[10px] text-[#A1A1AA] w-40 text-right">
                                {cur ? `${new Date(cur.t * 1000).toISOString().slice(11, 19)} · ${cur.price}` : ""}
                            </span>
                        </div>
                    )}
                    {d?.note && <div className="font-mono text-[9px] text-[#71717A] mt-2">{d.note}</div>}
                </div>
            </div>
        </div>
    );
};
