import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Eye, Activity, TrendingDown, TrendingUp, Minus, Clock, ShieldCheck } from "lucide-react";

/* "Bot is patiently watching" tile — explains WHY the bot isn't trading.
   Polls /api/signals/watch-status every 30s. Surfaces live entropy +
   sentiment + indicators per watched symbol so the user can see the bot's
   reasoning instead of staring at silence. */

function timeAgo(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return `${seconds}s ago`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m ago`;
}

function fmtCountdown(secs) {
    if (secs == null) return "—";
    if (secs <= 0) return "any moment";
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

const SENTIMENT_STYLE = {
    very_bullish: { fg: "text-[#00FF41]", Icon: TrendingUp,   label: "VERY BULLISH" },
    bullish:      { fg: "text-[#00FF41]", Icon: TrendingUp,   label: "BULLISH" },
    neutral:      { fg: "text-[#A1A1AA]", Icon: Minus,        label: "NEUTRAL" },
    bearish:      { fg: "text-[#FF3B30]", Icon: TrendingDown, label: "BEARISH" },
    very_bearish: { fg: "text-[#FF3B30]", Icon: TrendingDown, label: "VERY BEARISH" },
};

function EntropyBar({ entropy, threshold, marketClosed }) {
    if (entropy == null) {
        if (marketClosed) {
            return (
                <div className="font-mono text-[10px] text-[#FFB000]" data-testid="entropy-market-closed">
                    market closed · entropy paused
                </div>
            );
        }
        return <div className="font-mono text-[10px] text-[#52525B]">entropy: n/a</div>;
    }
    const pct = Math.min(100, Math.round(entropy * 100));
    const thresholdPct = Math.round(threshold * 100);
    const noisy = entropy >= threshold;
    return (
        <div className="space-y-1.5">
            <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                <span className="text-[#52525B]">ENTROPY</span>
                <span className={noisy ? "text-[#FFB000]" : "text-[#00FF41]"}>
                    {entropy.toFixed(4)} {noisy ? "· NOISY" : "· TRADABLE"}
                </span>
            </div>
            <div className="relative h-1.5 bg-[#1F1F1F]">
                <div
                    className={`absolute inset-y-0 left-0 ${noisy ? "bg-[#FFB000]" : "bg-[#00FF41]"}`}
                    style={{ width: `${pct}%` }}
                />
                {/* Threshold tick */}
                <div
                    className="absolute inset-y-0 w-px bg-[#FAFAFA]/40"
                    style={{ left: `${thresholdPct}%` }}
                    title={`Noise threshold: ${threshold}`}
                />
            </div>
            <div className="font-mono text-[9px] text-[#52525B]">
                noise threshold ▲ {threshold.toFixed(2)}
            </div>
        </div>
    );
}

function SymbolCard({ s, cooldownMin }) {
    const isHold = s.action === "HOLD";
    const sentLabel = (s.sentiment?.label || "neutral").toLowerCase();
    const sentStyle = SENTIMENT_STYLE[sentLabel] || SENTIMENT_STYLE.neutral;
    const SentIcon = sentStyle.Icon;
    const ind = s.indicators || {};

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-4"
             data-testid={`watch-card-${s.symbol}`}>
            {/* Header — symbol + current action */}
            <div className="flex items-start justify-between">
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SYMBOL</div>
                    <div className="font-mono font-medium text-lg tracking-tight mt-1">{s.symbol}</div>
                </div>
                <div className="text-right">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">CURRENT</div>
                    <div className={`font-mono text-sm tracking-widest mt-1 ${
                        isHold ? "text-[#A1A1AA]" : "text-[#00FF41]"
                    }`} data-testid={`watch-action-${s.symbol}`}>
                        {s.action || "—"}
                    </div>
                </div>
            </div>

            {/* Entropy meter — the headline metric */}
            <EntropyBar entropy={s.entropy} threshold={s.entropy_threshold ?? 0.9}
                        marketClosed={(s.reason || "").toLowerCase().includes("market closed")} />

            {/* Indicators strip */}
            <div className="grid grid-cols-3 gap-3 pt-1 border-t border-[#1F1F1F]">
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">PRICE</div>
                    <div className="font-mono text-xs mt-0.5">
                        {ind.current_price?.toLocaleString(undefined,
                            { minimumFractionDigits: 2, maximumFractionDigits: 2 }) || "—"}
                    </div>
                </div>
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">RSI 14</div>
                    <div className={`font-mono text-xs mt-0.5 ${
                        ind.rsi_14 == null ? "text-[#52525B]" :
                        ind.rsi_14 < 30 ? "text-[#00FF41]" :
                        ind.rsi_14 > 70 ? "text-[#FF3B30]" : "text-[#A1A1AA]"
                    }`}>
                        {ind.rsi_14?.toFixed(2) ?? "—"}
                    </div>
                </div>
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">VOL 30D</div>
                    <div className="font-mono text-xs mt-0.5">
                        {ind.volatility_30d_pct?.toFixed(2) ?? "—"}%
                    </div>
                </div>
            </div>

            {/* Sentiment strip */}
            {s.sentiment?.label && (
                <div className="pt-3 border-t border-[#1F1F1F] space-y-2">
                    <div className="flex items-center justify-between">
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">NEWS SENTIMENT</span>
                        <span className={`flex items-center gap-1.5 font-mono text-[10px] tracking-widest ${sentStyle.fg}`}>
                            <SentIcon className="w-3 h-3" />
                            {sentStyle.label}
                            <span className="text-[#52525B]">({s.sentiment.score?.toFixed(2)})</span>
                        </span>
                    </div>
                    {s.sentiment.summary && (
                        <p className="font-mono text-[11px] text-[#A1A1AA] leading-relaxed line-clamp-3"
                           data-testid={`watch-sentiment-${s.symbol}`}>
                            {s.sentiment.summary}
                        </p>
                    )}
                </div>
            )}

            {/* Why the bot is sitting out */}
            {isHold && s.reason && (
                <div className="pt-3 border-t border-[#1F1F1F] space-y-1.5">
                    <div className="flex items-center gap-1.5 font-mono text-[10px] text-[#FFB000] tracking-widest">
                        <ShieldCheck className="w-3 h-3" />
                        WHY I&apos;M NOT TRADING
                    </div>
                    <p className="font-mono text-[11px] text-[#A1A1AA] leading-relaxed"
                       data-testid={`watch-reason-${s.symbol}`}>
                        {s.reason}
                    </p>
                </div>
            )}

            {/* Footer — timing */}
            <div className="pt-3 border-t border-[#1F1F1F] flex items-center justify-between
                            font-mono text-[10px] text-[#52525B] tracking-widest">
                <span className="flex items-center gap-1.5">
                    <Clock className="w-3 h-3" />
                    LAST EVAL {timeAgo(s.seconds_since_last_eval)}
                </span>
                <span>
                    NEXT IN {fmtCountdown(s.next_evaluation_in_seconds)}
                </span>
            </div>
        </div>
    );
}

export default function BotWatching() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);

    const fetchStatus = useCallback(async () => {
        try {
            const res = await api.get("/signals/watch-status");
            setData(res.data);
            setErr(null);
        } catch (e) {
            setErr(e?.response?.data?.detail || e.message || "Failed to load watch status");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchStatus();
        const id = setInterval(fetchStatus, 30_000);
        return () => clearInterval(id);
    }, [fetchStatus]);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6"
                 data-testid="bot-watching-loading">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest animate-pulse">
                    LOADING BOT WATCH STATUS…
                </div>
            </div>
        );
    }
    if (err || !data) return null;

    const allHold = data.symbols?.every(s => s.action === "HOLD");

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="bot-watching-panel">
            {/* Header */}
            <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div className="relative">
                        <Eye className="w-4 h-4 text-[#00FF41]" />
                        <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 bg-[#00FF41] rounded-full pulse-dot" />
                    </div>
                    <div>
                        <div className="font-mono text-xs tracking-widest">
                            {allHold ? "BOT IS PATIENTLY WATCHING" : "BOT IS ACTIVELY HUNTING"}
                        </div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                            {allHold ? "Every HOLD is a loss avoided." : "Live signals in flight."}
                        </div>
                    </div>
                </div>
                <div className="text-right">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">HOLD STREAK</div>
                    <div className="font-mono text-lg mt-0.5" data-testid="watch-hold-streak">
                        {data.hold_streak}
                    </div>
                </div>
            </div>

            {/* Symbol cards */}
            <div className={`p-5 grid gap-4 ${
                (data.symbols?.length || 0) > 1 ? "md:grid-cols-2" : ""
            }`}>
                {(data.symbols || []).map(s => (
                    <SymbolCard key={s.symbol} s={s} cooldownMin={data.cooldown_minutes} />
                ))}
            </div>

            {/* Footer philosophy line */}
            <div className="px-5 py-3 border-t border-[#1F1F1F] flex items-center gap-2
                            font-mono text-[10px] text-[#52525B] tracking-widest">
                <Activity className="w-3 h-3" />
                <span data-testid="watch-philosophy">{data.philosophy}</span>
            </div>
        </div>
    );
}
