import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Link } from "react-router-dom";
import { CheckCircle2, Circle, Sparkles, X } from "lucide-react";

const DISMISS_KEY = "stoic_onboarding_dismissed";

/* Step states are derived live from backend so the banner self-completes
 * as the user makes progress, without ever lying. */
async function computeSteps() {
    const [{ data: accs }, { data: cfgs }, { data: trades }] = await Promise.all([
        api.get("/accounts"),
        api.get("/bot/configs"),
        api.get("/trades?status=open&limit=1"),
    ]);
    const liveAccounts = (accs || []).filter(a => (a.mode || "live") !== "paper");
    const hasAccount = liveAccounts.length > 0;
    const hasConnectedEa = liveAccounts.some(a =>
        a.status === "connected" && a.ea_version
    );
    const hasActiveConfig = (cfgs || []).some(c => c.active);
    const hasFirstTrade = Array.isArray(trades?.items)
        ? trades.items.length > 0
        : Array.isArray(trades) ? trades.length > 0 : false;

    return [
        {
            key: "account",
            label: "Connect a broker account",
            hint: "Link your MT5 broker so STOIC can route trades.",
            done: hasAccount,
            to: "/accounts",
            cta: "Go to Accounts",
        },
        {
            key: "ea",
            label: "Install the STOIC EA (v1.28+)",
            hint: "Download EmergentTradingBridge.mq5, compile in MetaEditor (F7), attach to a chart.",
            done: hasConnectedEa,
            to: "/accounts",
            cta: "Download EA",
        },
        {
            key: "config",
            label: "Pick a risk profile and start the bot",
            hint: "Choose a risk level (Low / Medium / High) and toggle the bot ON.",
            done: hasActiveConfig,
            to: "/bot-config",
            cta: "Open BotConfig",
        },
        {
            key: "first_trade",
            label: "Wait for your first signal",
            hint: "Once the bot is live, it scans every 60s. First trade lands when confidence + risk filters pass.",
            done: hasFirstTrade,
            to: "/trades",
            cta: "Open Trades",
        },
    ];
}

export function OnboardingBanner() {
    const [steps, setSteps] = useState(null);
    const [dismissed, setDismissed] = useState(
        () => localStorage.getItem(DISMISS_KEY) === "1"
    );
    const [collapsed, setCollapsed] = useState(false);

    useEffect(() => {
        let cancelled = false;
        computeSteps()
            .then(s => { if (!cancelled) setSteps(s); })
            .catch(() => { /* show banner with all-undone fallback */
                if (!cancelled) setSteps(null);
            });
        return () => { cancelled = true; };
    }, []);

    // Auto-hide once everything is done (and stay hidden for future visits).
    useEffect(() => {
        if (steps && steps.every(s => s.done)) {
            localStorage.setItem(DISMISS_KEY, "1");
        }
    }, [steps]);

    if (dismissed || !steps) return null;
    const remaining = steps.filter(s => !s.done).length;
    if (remaining === 0) return null;

    const nextStep = steps.find(s => !s.done);

    return (
        <div className="border border-[#FFD700]/40 bg-[#FFD700]/[0.04]"
             data-testid="onboarding-banner">
            <div className="px-5 py-3 flex items-center gap-3 flex-wrap">
                <Sparkles className="w-4 h-4 text-[#FFD700] shrink-0" />
                <div className="flex-1 min-w-0">
                    <div className="font-display text-base">
                        Welcome to STOIC — {steps.length - remaining}/{steps.length} steps complete
                    </div>
                    <div className="font-mono text-[11px] text-[#A1A1AA] mt-0.5">
                        Next: {nextStep?.label}
                    </div>
                </div>
                {nextStep && (
                    <Link to={nextStep.to} data-testid="onboarding-next-cta"
                          className="font-mono text-[10px] tracking-widest text-[#FFD700] border border-[#FFD700]/40 hover:bg-[#FFD700]/10 px-3 py-1.5">
                        {nextStep.cta}
                    </Link>
                )}
                <button type="button" onClick={() => setCollapsed(v => !v)}
                        data-testid="onboarding-toggle"
                        className="font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-white px-2 py-1.5">
                    {collapsed ? "EXPAND" : "COLLAPSE"}
                </button>
                <button type="button" onClick={() => {
                    localStorage.setItem(DISMISS_KEY, "1");
                    setDismissed(true);
                }} data-testid="onboarding-dismiss" title="Hide onboarding"
                        className="text-[#A1A1AA] hover:text-white p-1">
                    <X className="w-3.5 h-3.5" />
                </button>
            </div>
            {!collapsed && (
                <div className="border-t border-[#1F1F1F] px-5 py-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4"
                     data-testid="onboarding-steps">
                    {steps.map((s, i) => (
                        <Link key={s.key} to={s.to}
                              data-testid={`onboarding-step-${s.key}`}
                              className={`flex items-start gap-2 p-2 border transition-colors ${
                                  s.done
                                      ? "border-[#00FF41]/30 bg-[#00FF41]/5"
                                      : "border-[#1F1F1F] hover:border-[#FFD700]/40 hover:bg-[#FFD700]/5"
                              }`}>
                            {s.done
                                ? <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0 text-[#00FF41]" />
                                : <Circle className="w-4 h-4 mt-0.5 shrink-0 text-[#52525B]" />}
                            <div className="min-w-0">
                                <div className="font-mono text-[10px] tracking-widest text-[#52525B]">
                                    STEP {i + 1}
                                </div>
                                <div className={`font-display text-sm tracking-tight leading-tight ${
                                    s.done ? "text-[#A1A1AA] line-through" : "text-white"
                                }`}>
                                    {s.label}
                                </div>
                                <div className="font-mono text-[10px] text-[#52525B] mt-1 leading-snug">
                                    {s.hint}
                                </div>
                            </div>
                        </Link>
                    ))}
                </div>
            )}
        </div>
    );
}
