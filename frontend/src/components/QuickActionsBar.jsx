import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Power, Zap, TrendingUp, TrendingDown, AlertOctagon } from "lucide-react";
import { toast } from "sonner";
import { formatApiError } from "@/lib/api";
import { AccountSwitcher } from "@/components/AccountSwitcher";

/* Sticky Quick Actions bar — appears top-right on every authed page. Shows:
 *   • Bot ON/OFF toggle (mirrors active state across all configs)
 *   • Today's realised P&L (real-time)
 *   • Open trades count
 *   • PANIC button (full close-all confirmation modal)
 *
 * Designed for repeated daily use: every primary action one click away,
 * without hunting through nav.
 */
const REFRESH_MS = 15_000;

export function QuickActionsBar() {
    const [data, setData] = useState(null);
    const [panicOpen, setPanicOpen] = useState(false);
    const [actionInflight, setActionInflight] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data: d } = await api.get("/bot/quick-actions");
            setData(d);
        } catch { /* keep last good */ }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, REFRESH_MS);
        return () => clearInterval(t);
    }, [load]);

    const toggleBot = async () => {
        if (!data || actionInflight) return;
        setActionInflight(true);
        try {
            const next = !data.bot_active;
            await api.put("/bot/config", { active: next });
            setData(d => ({ ...d, bot_active: next }));
            toast.success(next ? "Bot started · trading enabled" : "Bot paused · no new trades");
        } catch (e) {
            toast.error("Bot toggle failed", { description: formatApiError(e) });
        } finally {
            setActionInflight(false);
        }
    };

    const panicClose = async () => {
        setActionInflight(true);
        try {
            await api.post("/panic");
            toast.success("PANIC sent · closing all positions");
            setPanicOpen(false);
            await load();
        } catch (e) {
            toast.error("PANIC failed", { description: formatApiError(e) });
        } finally {
            setActionInflight(false);
        }
    };

    if (!data) return null;
    const botPnl = data.todays_bot_pnl_usd ?? 0;
    const manPnl = data.todays_manual_pnl_usd ?? 0;
    const pnl = botPnl + manPnl;
    const pnlPositive = pnl >= 0;
    const fmt = (v) => `${v >= 0 ? "+" : "−"}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

    return (
        <>
            <div className="fixed top-8 right-3 z-40 flex items-center gap-1.5 sm:gap-2 bg-[#0A0A0A]/95 backdrop-blur-md border border-[#1F1F1F] shadow-xl px-2 py-1.5"
                 data-testid="quick-actions-bar">

                {/* Multi-account switcher (iter-137) */}
                <AccountSwitcher />

                {/* Today's P&L — bot vs manual */}
                <div className="hidden md:flex flex-col items-end px-2"
                     data-testid="quick-todays-pnl"
                     title={`${data.todays_bot_closed_count ?? 0} bot + ${(data.todays_closed_count ?? 0) - (data.todays_bot_closed_count ?? 0)} manual trades closed today`}>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest leading-none">
                        BOT TODAY
                    </div>
                    <div className="font-display text-sm leading-tight mt-0.5"
                         style={{ color: botPnl >= 0 ? "#00FF41" : "#FF3B30" }}>
                        {fmt(botPnl)}
                    </div>
                </div>
                <div className="hidden lg:flex flex-col items-end px-2 border-l border-[#1F1F1F]"
                     data-testid="quick-manual-pnl">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest leading-none">
                        MANUAL
                    </div>
                    <div className="font-display text-sm leading-tight mt-0.5"
                         style={{ color: manPnl >= 0 ? "#A1A1AA" : "#FF3B30" }}>
                        {fmt(manPnl)}
                    </div>
                </div>

                {/* Open trades — broker-verified truth; UNKNOWN never shows 0 */}
                <div className="hidden sm:flex flex-col items-end px-2 border-l border-[#1F1F1F]"
                     data-testid="quick-open-trades"
                     title={data.position_truth === "FRESH"
                         ? `Broker-verified open positions · as of ${data.as_of ? new Date(data.as_of).toLocaleTimeString() : "now"}`
                         : `Position truth ${data.position_truth || "UNKNOWN"} — broker count cannot be confirmed right now (local projection: ${data.open_trades})`}>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest leading-none">
                        {data.position_truth === "FRESH" ? "OPEN · VERIFIED" : "OPEN"}
                    </div>
                    <div className={`font-display text-sm leading-tight mt-0.5 ${
                        data.position_truth === "FRESH" ? "text-white" : "text-[#FFB020]"}`}
                        data-testid="quick-open-trades-value">
                        {data.position_truth === "FRESH"
                            ? (data.open_trades_broker ?? data.open_trades)
                            : `${data.open_trades} · ${data.position_truth || "UNKNOWN"}`}
                    </div>
                </div>

                {/* Bot ON/OFF — configured state + server-derived EFFECTIVE state */}
                <button type="button" onClick={toggleBot} disabled={actionInflight}
                        data-testid="quick-toggle-bot"
                        title={data.bot_active
                            ? `Bot configured ON — effective execution state: ${data.effective_state || "…"}. Click to pause.`
                            : "Bot is OFF — click to start"}
                        className={`flex items-center gap-1.5 font-mono text-[10px] tracking-widest px-2.5 py-1.5 border transition-colors ${
                            data.bot_active
                                ? (data.effective_state === "ACTIVE"
                                    ? "text-[#00FF41] border-[#00FF41]/40 bg-[#00FF41]/10 hover:bg-[#00FF41]/15"
                                    : "text-[#FFB020] border-[#FFB020]/40 bg-[#FFB020]/10 hover:bg-[#FFB020]/15")
                                : "text-[#A1A1AA] border-[#1F1F1F] hover:border-[#52525B]"
                        } disabled:opacity-50`}>
                    <Power className="w-3 h-3" />
                    <span className="hidden sm:inline" data-testid="quick-bot-effective-state">
                        {data.bot_active
                            ? `BOT REQUESTED ON · ${data.effective_state === "ACTIVE" ? "EXECUTING" : `EXECUTION ${data.effective_state || "…"}`}`
                            : "BOT OFF"}
                    </span>
                </button>

                {/* PANIC — available whenever exposure exists OR cannot be ruled out */}
                <button type="button" onClick={() => setPanicOpen(true)}
                        disabled={!(data.panic_available ?? data.open_trades > 0) || actionInflight}
                        data-testid="quick-panic"
                        title={!(data.panic_available ?? data.open_trades > 0)
                            ? "Nothing to panic-close (broker-verified flat)"
                            : data.position_truth === "FRESH"
                                ? `Close all ${data.open_trades} open positions immediately`
                                : "Position truth is not fresh — PANIC stays armed and will issue durable close intents for every known position"}
                        className={`relative flex items-center gap-1.5 font-mono text-[11px] font-bold tracking-widest px-3 py-1.5 border-2 transition-all
                            ${(data.panic_available ?? data.open_trades > 0) && !actionInflight
                                ? "bg-[#FF3B30] border-[#FF3B30] text-white hover:bg-[#E5352B] hover:border-[#E5352B] shadow-[0_0_0_3px_rgba(255,59,48,0.25)] hover:shadow-[0_0_0_4px_rgba(255,59,48,0.4)] panic-pulse"
                                : "border-[#FF3B30]/30 text-[#FF3B30]/40 cursor-not-allowed"}
                        `}>
                    <AlertOctagon className={`w-3.5 h-3.5 ${data.open_trades > 0 ? "animate-pulse" : ""}`} />
                    <span>PANIC</span>
                    {data.open_trades > 0 && (
                        <span className="hidden sm:inline-block ml-1 px-1.5 py-0.5 text-[9px] bg-white/20 text-white rounded-sm font-bold">
                            {data.open_trades}
                        </span>
                    )}
                </button>
            </div>

            {/* PANIC confirmation modal */}
            {panicOpen && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm p-4"
                     data-testid="panic-confirm-modal"
                     onClick={() => setPanicOpen(false)}>
                    <div onClick={e => e.stopPropagation()}
                         className="w-full max-w-md bg-[#0A0A0A] border border-[#FF3B30]/60 shadow-2xl">
                        <div className="border-b border-[#1F1F1F] px-5 py-3 flex items-center gap-2">
                            <AlertOctagon className="w-4 h-4 text-[#FF3B30]" />
                            <div className="font-display text-lg text-[#FF3B30]">
                                PANIC-CLOSE ALL POSITIONS?
                            </div>
                        </div>
                        <div className="px-5 py-4 space-y-3 font-mono text-xs text-[#A1A1AA] leading-relaxed">
                            <div>
                                This will tell the EA to <span className="text-white">immediately close every open position</span>{" "}
                                on every connected account.
                            </div>
                            <div className="border-l-2 border-[#FF3B30]/40 pl-3">
                                Currently open: <span className="text-white">{data.open_trades}</span> trade(s)<br />
                                Today&apos;s realised P&amp;L: <span style={{ color: pnlPositive ? "#00FF41" : "#FF3B30" }}>
                                    {pnlPositive ? "+" : ""}${pnl.toFixed(2)}
                                </span>
                            </div>
                            <div className="text-[10px] text-[#52525B] tracking-widest">
                                ⚠ THIS LOCKS IN UNREALISED P&amp;L AT CURRENT PRICE
                            </div>
                        </div>
                        <div className="border-t border-[#1F1F1F] px-5 py-3 flex items-center justify-end gap-2">
                            <button type="button" onClick={() => setPanicOpen(false)}
                                    data-testid="panic-cancel"
                                    className="font-mono text-xs tracking-widest text-[#A1A1AA] hover:text-white px-3 py-2">
                                CANCEL
                            </button>
                            <button type="button" onClick={panicClose} disabled={actionInflight}
                                    data-testid="panic-confirm"
                                    className="font-mono text-xs tracking-widest text-[#FF3B30] border border-[#FF3B30]/40 hover:bg-[#FF3B30]/10 px-3 py-2 disabled:opacity-50">
                                {actionInflight ? "CLOSING…" : "CLOSE ALL NOW"}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </>
    );
}
