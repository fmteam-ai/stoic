import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Activity } from "lucide-react";

const AXES = [
    ["trend", "TREND"], ["volatility", "VOLATILITY"],
    ["liquidity", "LIQUIDITY"], ["news_risk", "NEWS RISK"],
    ["confidence", "CONFIDENCE"],
];

const color = (k, v) => {
    const good = k === "news_risk" ? 100 - v : v;
    return good >= 60 ? "#00FF41" : good >= 35 ? "#FFD700" : "#FF3B30";
};

export default function MarketStateStrip() {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/bot/market-state")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d) return null;
    const health = d.market_health;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="market-state-strip">
            <div className="flex items-center gap-2 flex-wrap">
                <Activity className="w-4 h-4 text-[#38BDF8]" />
                <span className="font-display font-bold text-sm">Market State</span>
                <span className="font-mono text-[9px] tracking-widest text-[#52525B]">
                    {d.regime?.label?.toUpperCase()}
                </span>
                <span className="ml-auto font-mono text-[10px] tracking-widest px-2 py-0.5 border"
                    style={{ color: color("health", health), borderColor: `${color("health", health)}66` }}
                    data-testid="market-health-score">
                    MARKET HEALTH {health}
                </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-x-4 gap-y-1 mt-3">
                {AXES.map(([k, label]) => (
                    <div key={k} data-testid={`market-state-${k}`}>
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</div>
                        <div className="flex items-center gap-1.5 mt-0.5">
                            <div className="flex-1 h-1 bg-[#141414]">
                                <div className="h-full" style={{ width: `${d.scores[k]}%`, background: color(k, d.scores[k]) }} />
                            </div>
                            <span className="font-mono text-[10px] text-white tabular-nums w-6 text-right">{d.scores[k]}</span>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
