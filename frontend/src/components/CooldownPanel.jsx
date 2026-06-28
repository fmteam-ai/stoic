import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import {
    Timer, Flame, ShieldOff, TrendingUp, TrendingDown,
    Clock, AlertOctagon,
} from "lucide-react";

/* Per-account Cooldown + Loss-Streak tile.
   Complements BotWatching (which shows global market state) by giving the
   trader a per-account view: how close each broker is to anti-tilt freeze,
   what its last trade was, and how long until the next signal evaluation.
   Polls /api/bot/cooldowns every 20s. */

function fmtCountdown(secs) {
    if (secs == null) return "—";
    if (secs <= 0) return "any moment";
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    if (m === 0) return `${s}s`;
    return `${m}m ${s.toString().padStart(2, "0")}s`;
}

function fmtTimeAgo(s) {
    if (s == null) return "—";
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m ago`;
}

function StreakBar({ losses, threshold, antiTiltActive }) {
    if (!threshold || threshold <= 0) {
        return <span className="font-mono text-[10px] text-[#52525B]">anti-tilt off</span>;
    }
    const segments = Array.from({ length: threshold }, (_, i) => i);
    return (
        <div className="flex items-center gap-1" data-testid="streak-bar">
            {segments.map(i => (
                <span
                    key={i}
                    className={`h-1.5 flex-1 transition-colors ${
                        i < losses
                            ? (antiTiltActive ? "bg-[#FF3B30]" : "bg-[#FFB000]")
                            : "bg-[#1F1F1F]"
                    }`}
                />
            ))}
        </div>
    );
}

function AccountCard({ a }) {
    const lossStreak = a.loss_streak || 0;
    const winStreak = a.win_streak || 0;
    const threshold = a.anti_tilt_threshold || 0;
    const lossesRemainingBeforeFreeze = Math.max(0, threshold - lossStreak);
    const lastPnl = a.last_trade?.pnl;
    const isWin = lastPnl != null && lastPnl > 0;
    const isLoss = lastPnl != null && lastPnl < 0;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-4"
             data-testid={`cooldown-card-${a.account_id || 'default'}`}>
            {/* Header */}
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">
                        {a.broker?.toUpperCase()}
                    </div>
                    <div className="font-mono font-medium text-sm tracking-tight mt-0.5 truncate">
                        {a.label}
                    </div>
                </div>
                <div className="text-right shrink-0">
                    {a.active ? (
                        <span className="font-mono text-[10px] text-[#00FF41] tracking-widest">● ACTIVE</span>
                    ) : a.paper_shadow_mode ? (
                        <span className="font-mono text-[10px] text-[#FFB000] tracking-widest">◐ SHADOW</span>
                    ) : (
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">○ IDLE</span>
                    )}
                </div>
            </div>

            {/* Anti-tilt freeze banner — takes precedence if active */}
            {a.anti_tilt_active ? (
                <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 flex items-start gap-2"
                     data-testid={`cooldown-frozen-${a.account_id || 'default'}`}>
                    <ShieldOff className="w-4 h-4 text-[#FF3B30] shrink-0 mt-0.5" />
                    <div className="font-mono text-[11px] text-[#FF3B30] leading-relaxed">
                        <div className="font-bold tracking-widest">ANTI-TILT FROZEN</div>
                        <div className="text-[10px] mt-0.5">
                            Trading paused after {lossStreak} consecutive losses.
                            Unfreezes in <span className="font-bold">{fmtCountdown(a.anti_tilt_unfreeze_in_seconds)}</span>.
                        </div>
                    </div>
                </div>
            ) : threshold > 0 && lossStreak > 0 ? (
                <div className="space-y-1.5">
                    <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                        <span className={`flex items-center gap-1 ${
                            lossesRemainingBeforeFreeze === 1 ? "text-[#FFB000]" : "text-[#A1A1AA]"
                        }`}>
                            <AlertOctagon className="w-3 h-3" />
                            LOSS STREAK
                        </span>
                        <span className="text-[#52525B]">
                            {lossStreak} / {threshold} · {lossesRemainingBeforeFreeze} until freeze
                        </span>
                    </div>
                    <StreakBar losses={lossStreak} threshold={threshold} antiTiltActive={false} />
                </div>
            ) : winStreak > 0 ? (
                <div className="flex items-center gap-2">
                    <Flame className="w-3.5 h-3.5 text-[#00FF41]" />
                    <span className="font-mono text-[11px] text-[#00FF41] tracking-widest"
                          data-testid={`cooldown-win-streak-${a.account_id || 'default'}`}>
                        {winStreak}-TRADE WIN STREAK
                    </span>
                </div>
            ) : (
                <div className="flex items-center gap-2 text-[#52525B]">
                    <span className="font-mono text-[10px] tracking-widest">CLEAN SLATE — NO RECENT CLOSES</span>
                </div>
            )}

            {/* Per-symbol cooldown / market status */}
            <div className="grid grid-cols-1 gap-2 pt-3 border-t border-[#1F1F1F]">
                {(a.symbols || []).map(s => (
                    <div key={s.symbol} className="flex items-center justify-between gap-2"
                         data-testid={`cooldown-symbol-${a.account_id || 'default'}-${s.symbol}`}>
                        <div className="font-mono text-xs tracking-widest">{s.symbol}</div>
                        {s.market_closed ? (
                            <div className="flex items-center gap-1.5 font-mono text-[10px] text-[#FFB000]">
                                <ShieldOff className="w-3 h-3" />
                                <span>MARKET CLOSED · reopens in {s.reopens_in_hours}h</span>
                            </div>
                        ) : (
                            <div className="flex items-center gap-1.5 font-mono text-[10px] text-[#A1A1AA]">
                                <Timer className="w-3 h-3" />
                                <span>NEXT EVAL IN {fmtCountdown(a.cooldown_seconds_left)}</span>
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {/* Last trade summary */}
            {a.last_trade ? (
                <div className="pt-3 border-t border-[#1F1F1F] flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                        {isWin ? <TrendingUp className="w-3.5 h-3.5 text-[#00FF41] shrink-0" /> :
                         isLoss ? <TrendingDown className="w-3.5 h-3.5 text-[#FF3B30] shrink-0" /> :
                                  <Clock className="w-3.5 h-3.5 text-[#A1A1AA] shrink-0" />}
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">
                            LAST: {a.last_trade.action} {a.last_trade.symbol}
                        </div>
                    </div>
                    <div className={`font-mono text-xs tracking-tight ${
                        isWin ? "text-[#00FF41]" : isLoss ? "text-[#FF3B30]" : "text-[#A1A1AA]"
                    }`} data-testid={`cooldown-last-pnl-${a.account_id || 'default'}`}>
                        {lastPnl >= 0 ? "+" : ""}${lastPnl?.toFixed(2)}
                    </div>
                </div>
            ) : (
                <div className="pt-3 border-t border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                    NO TRADES YET ON THIS ACCOUNT
                </div>
            )}
        </div>
    );
}

export default function CooldownPanel() {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);

    const fetchCooldowns = useCallback(async () => {
        try {
            const res = await api.get("/bot/cooldowns");
            setData(res.data);
            setErr(null);
        } catch (e) {
            setErr(e?.response?.data?.detail || e.message || "Failed to load cooldowns");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchCooldowns();
        const id = setInterval(fetchCooldowns, 20_000);
        return () => clearInterval(id);
    }, [fetchCooldowns]);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6"
                 data-testid="cooldown-panel-loading">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest animate-pulse">
                    LOADING COOLDOWN STATE…
                </div>
            </div>
        );
    }
    if (err || !data?.items?.length) return null;

    // Hide the legacy "default profile" (no account_id) when the user has at
    // least one real account-scoped config — it's noise once accounts exist.
    const hasAccountScoped = data.items.some(x => x.account_id);
    const items = data.items.filter(x => {
        if (hasAccountScoped && !x.account_id) return false;
        // Also hide stale account-less configs that have never produced anything.
        return x.account_id || x.active || x.paper_shadow_mode || x.last_trade;
    });
    if (!items.length) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="cooldown-panel">
            <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center gap-3">
                <Timer className="w-4 h-4 text-[#00FF41]" />
                <div>
                    <div className="font-mono text-xs tracking-widest">PER-ACCOUNT COOLDOWN STATE</div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                        Next-eval countdown · loss streak · anti-tilt distance · last trade
                    </div>
                </div>
            </div>
            <div className={`p-5 grid gap-4 ${items.length > 1 ? "md:grid-cols-2" : ""}`}>
                {items.map(a => (
                    <AccountCard key={a.config_id} a={a} />
                ))}
            </div>
        </div>
    );
}
