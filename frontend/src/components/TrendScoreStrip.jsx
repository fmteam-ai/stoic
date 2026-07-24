import { useEffect, useState } from "react";
import api from "@/lib/api";
import { TrendingUp, AlertTriangle } from "lucide-react";

const DIR_COLOR = { UP: "#00FF41", DOWN: "#FF3B30", FLAT: "#A1A1AA" };

export default function TrendScoreStrip({ symbol = "XAUUSD" }) {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get(`/bot/trend-score?symbol=${symbol}`)
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, [symbol]);

    if (hidden || !d || !d.ready) return null;
    const q = d.quality;
    const ex = d.exhaustion;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="trend-score-strip">
            <div className="flex items-center gap-2 flex-wrap">
                <TrendingUp className="w-4 h-4" style={{ color: DIR_COLOR[q.direction] }} />
                <span className="font-display font-bold text-sm">Trend Quality · {d.symbol}</span>
                <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border"
                    style={{ color: DIR_COLOR[q.direction], borderColor: `${DIR_COLOR[q.direction]}66` }}
                    data-testid="trend-direction">
                    {q.direction} · {q.score}/100
                </span>
                {d.exhausted && (
                    <span className="flex items-center gap-1 font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#FF8C00]/50 text-[#FF8C00]"
                        data-testid="trend-exhausted-badge">
                        <AlertTriangle className="w-3 h-3" /> EXHAUSTION {ex.score}
                    </span>
                )}
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-x-4 gap-y-1 mt-3">
                {Object.entries(q.components).map(([k, v]) => (
                    <div key={k} data-testid={`trend-comp-${k}`}>
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B]">
                            {k.replace(/_/g, " ").toUpperCase()}
                        </div>
                        <div className="flex items-center gap-1.5 mt-0.5">
                            <div className="flex-1 h-1 bg-[#141414]">
                                <div className="h-full" style={{ width: `${v}%`, background: v >= 60 ? "#00FF41" : v >= 35 ? "#FFD700" : "#FF3B30" }} />
                            </div>
                            <span className="font-mono text-[10px] text-white tabular-nums w-6 text-right">{v}</span>
                        </div>
                    </div>
                ))}
            </div>
            {ex.signals?.length > 0 && (
                <div className="font-mono text-[9px] tracking-widest text-[#FF8C00] mt-2" data-testid="trend-exhaustion-signals">
                    EXHAUSTION SIGNS: {ex.signals.map(s => s.replace(/_/g, " ").toUpperCase()).join(" · ")}
                </div>
            )}
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-2">
                STRUCTURE + MOMENTUM + VOLATILITY + CROSS-TF + SPREAD — NEVER A SINGLE MA CROSSOVER
            </div>
        </div>
    );
}
