import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Radar, Check, Ban } from "lucide-react";

const AXIS_COLORS = {
    trending_up: "#00FF41", trending_down: "#FF3B30", ranging: "#FFD700",
    high: "#FF8C00", normal: "#A1A1AA", low: "#38BDF8",
    risk_on: "#00FF41", risk_off: "#FF3B30", neutral: "#A1A1AA",
};

const NAMES = {
    trend: "TREND", scalp: "SCALP", breakout: "BREAKOUT",
    mean_reversion: "MEAN REV", experimental: "EXPERIMENTAL",
};

const Badge = ({ text, color, testid }) => (
    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border"
        style={{ color, borderColor: `${color}66` }} data-testid={testid}>
        {text}
    </span>
);

export const RegimeCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/risk/regime")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d) return null;
    const r = d.regime;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="regime-card">
            <div className="flex items-center gap-2 flex-wrap">
                <Radar className="w-4 h-4 text-[#38BDF8]" />
                <span className="font-display font-bold text-sm text-white">Market Regime</span>
                <span className="font-mono text-[10px] tracking-widest text-[#38BDF8]" data-testid="regime-label">
                    {r.label}
                </span>
            </div>
            <div className="flex gap-2 flex-wrap mt-3">
                <Badge text={r.trend.axis.replace("_", " ").toUpperCase()}
                    color={AXIS_COLORS[r.trend.axis]} testid="regime-trend" />
                <Badge text={`${r.volatility.axis.toUpperCase()} VOL${r.volatility.ratio ? ` ×${r.volatility.ratio}` : ""}`}
                    color={AXIS_COLORS[r.volatility.axis]} testid="regime-volatility" />
                <Badge text={r.sentiment.axis.replace("_", "-").toUpperCase()}
                    color={AXIS_COLORS[r.sentiment.axis]} testid="regime-sentiment" />
                <Badge text={r.news.news_driven ? "NEWS-DRIVEN" : "NO NEWS RISK"}
                    color={r.news.news_driven ? "#FF3B30" : "#A1A1AA"} testid="regime-news" />
            </div>
            <div className="font-mono text-[9px] text-[#71717A] mt-2 space-y-0.5">
                <div>{r.trend.detail}</div>
                <div>{r.volatility.detail} · {r.sentiment.detail}</div>
                <div>{r.news.detail}</div>
            </div>
            {r.probabilities && (
                <div className="mt-3 pt-2 border-t border-[#141414]" data-testid="regime-probabilities">
                    <div className="flex items-center justify-between gap-2 mb-1.5">
                        <span className="font-mono text-[9px] tracking-widest text-[#52525B]">
                            REGIME PROBABILITIES — UNCERTAIN CLASSIFICATION = REDUCED EXPOSURE
                        </span>
                        <span className={`font-mono text-[9px] tracking-widest ${
                            r.probabilities.uncertainty > 0.7 ? "text-[#FF8C00]" : "text-[#A1A1AA]"}`}
                            data-testid="regime-uncertainty">
                            UNCERTAINTY {Math.round(r.probabilities.uncertainty * 100)}%
                        </span>
                    </div>
                    <div className="space-y-1">
                        {Object.entries(r.probabilities.classes).slice(0, 5).map(([cls, p]) => (
                            <div key={cls} className="flex items-center gap-2" data-testid={`regime-prob-${cls}`}>
                                <span className="font-mono text-[9px] text-[#A1A1AA] w-40 shrink-0">
                                    {cls.replace(/_/g, " ").toUpperCase()}
                                </span>
                                <div className="flex-1 h-1.5 bg-[#141414]">
                                    <div className="h-full bg-[#38BDF8]" style={{ width: `${Math.round(p * 100)}%` }} />
                                </div>
                                <span className="font-mono text-[9px] text-white w-9 text-right tabular-nums">
                                    {Math.round(p * 100)}%
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            )}
            <div className="mt-3 pt-2 border-t border-[#141414]">
                <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">
                    STRATEGY FIT IN THIS REGIME — PROVEN NEGATIVE EDGE IS BENCHED UNTIL CONDITIONS CHANGE
                </div>
                <div className="space-y-1">
                    {Object.entries(d.strategy_fit).map(([k, f]) => (
                        <div key={k} className="flex items-center gap-2" data-testid={`regime-fit-${k}`}>
                            {f.allowed
                                ? <Check className="w-3 h-3 text-[#00FF41] shrink-0" />
                                : <Ban className="w-3 h-3 text-[#FF3B30] shrink-0" />}
                            <span className="font-mono text-[10px] text-white w-24 shrink-0">{NAMES[k] || k.toUpperCase()}</span>
                            <span className={`font-mono text-[10px] tracking-widest shrink-0 ${f.allowed ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                                data-testid={`regime-fit-${k}-status`}>
                                {f.allowed ? "ENABLED" : "BENCHED"}
                            </span>
                            <span className="font-mono text-[9px] text-[#71717A] truncate">{f.reason}</span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
};
