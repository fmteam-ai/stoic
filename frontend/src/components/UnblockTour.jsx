import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, CheckCircle2, LifeBuoy, X } from "lucide-react";

/* iter-173 — Guided Unblock Tour: step-by-step overlay from any red
 * readiness blocker straight to the fix, with deep links. */
const TOURS = {
    PANIC_TRIPPED: {
        title: "Reset the panic switch",
        steps: [
            { t: "Verify broker positions", d: "Open MT5 (or your broker portal) and confirm every position is where you expect — the panic close-all may have partially filled." },
            { t: "Understand what tripped it", d: "Check the alerts feed and today's P&L on the Dashboard to see what triggered the panic before re-arming anything.", link: { to: "/", label: "Open Dashboard" } },
            { t: "Reset the panic switch", d: "Go to Bot Config and reset PANIC in the status row. The bot stays OFF until you explicitly start it again.", link: { to: "/bot", label: "Open Bot Config" } },
        ],
    },
    POSITION_TRUTH_STALE: {
        title: "Restore fresh position truth",
        steps: [
            { t: "Check the terminal is running", d: "On the machine (PC or VPS) for each affected account, make sure the MT5 terminal is open and connected to your broker." },
            { t: "Check the EA is attached", d: "The STOIC EA (EmergentTradingBridge) must be on a chart with AutoTrading ON (green play button in the MT5 toolbar)." },
            { t: "Wait for it to clear itself", d: "Once heartbeats resume, a fresh broker snapshot plus reconciliation clears this automatically within ~3 minutes — no reset needed.", link: { to: "/accounts", label: "Watch account status" } },
        ],
    },
    EXECUTION_BLOCKED: {
        title: "Unblock execution",
        steps: [
            { t: "Open your account panel", d: "Go to the Accounts page and expand the blocked account.", link: { to: "/accounts", label: "Open Accounts" } },
            { t: "One-click trust (no PowerShell)", d: "If the EA is already connected you'll see a green TRUST THIS TERMINAL button in the Quick Install panel — one click and you're done." },
            { t: "Or run the strongest pairing", d: "Prefer maximum assurance? Generate a pairing token in Quick Install and paste the PowerShell one-liner on your MT5 host (~60 seconds)." },
        ],
    },
    RECONCILIATION_PENDING: {
        title: "Reconcile unknown executions",
        steps: [
            { t: "Run a broker sync", d: "Open the Trades page and press SYNC WITH BROKER — this pulls the broker's full deal history so every UNKNOWN execution reaches a terminal state.", link: { to: "/trades", label: "Open Trades" } },
            { t: "Wait for reconciliation", d: "Once every execution is confirmed opened or cancelled against broker truth, this blocker clears automatically." },
        ],
    },
    AUTHORITY_REDUCED: {
        title: "Restore full execution authority",
        steps: [
            { t: "Read the authority strip", d: "The strip under the header lists exactly which domain reduced authority (broker, execution, risk…) and why." },
            { t: "Resolve or wait", d: "Most reductions restore automatically once the underlying condition clears; identity issues are fixed from the Accounts page.", link: { to: "/accounts", label: "Open Accounts" } },
        ],
    },
    NO_ENABLED_ACCOUNTS: {
        title: "Enable a trading account",
        steps: [
            { t: "Enable an account", d: "Use the account switcher in the header, or open the Accounts page and toggle trading ON for the account you want the bot to run on.", link: { to: "/accounts", label: "Open Accounts" } },
        ],
    },
    NO_BOTS_ENABLED: {
        title: "Turn a bot on",
        steps: [
            { t: "Start the bot", d: "Open Bot Config, pick a risk profile, and press START BOT once you're ready to trade.", link: { to: "/bot", label: "Open Bot Config" } },
        ],
    },
};

export function UnblockTour({ code, reason, onClose }) {
    const [step, setStep] = useState(0);
    const navigate = useNavigate();
    const tour = TOURS[code];
    if (!tour) return null;
    const s = tour.steps[step];
    const last = step === tour.steps.length - 1;

    const go = (to) => { onClose(); navigate(to); };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm p-4"
             data-testid="unblock-tour" onClick={onClose}>
            <div onClick={(e) => e.stopPropagation()}
                 className="w-full max-w-lg bg-[#0A0A0A] border border-[#FFD700]/40 shadow-2xl">
                <div className="border-b border-[#1F1F1F] px-5 py-3 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                        <LifeBuoy className="w-4 h-4 text-[#FFD700]" />
                        <span className="font-display text-lg text-white">{tour.title}</span>
                    </div>
                    <button onClick={onClose} data-testid="unblock-tour-close"
                            className="text-[#52525B] hover:text-white">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                {reason && (
                    <div className="px-5 pt-3 font-mono text-[10px] text-[#FF3B30]"
                         data-testid="unblock-tour-reason">
                        {reason}
                    </div>
                )}
                <div className="px-5 py-4">
                    <div className="font-mono text-[10px] tracking-widest text-[#52525B] mb-1.5">
                        STEP {step + 1} OF {tour.steps.length}
                    </div>
                    <div className="text-white font-medium mb-1" data-testid="unblock-tour-step-title">{s.t}</div>
                    <div className="text-sm text-[#A1A1AA] leading-relaxed">{s.d}</div>
                    {s.link && (
                        <button onClick={() => go(s.link.to)}
                                data-testid="unblock-tour-link"
                                className="mt-3 flex items-center gap-1.5 font-mono text-xs tracking-widest text-[#FFD700] border border-[#FFD700]/40 hover:bg-[#FFD700]/10 px-3 py-1.5">
                            {s.link.label} <ArrowRight className="w-3 h-3" />
                        </button>
                    )}
                </div>
                <div className="border-t border-[#1F1F1F] px-5 py-3 flex items-center justify-between">
                    <div className="flex items-center gap-1.5">
                        {tour.steps.map((_, i) => (
                            <span key={i} className={`w-1.5 h-1.5 rounded-full ${i === step ? "bg-[#FFD700]" : "bg-[#2A2A2A]"}`} />
                        ))}
                    </div>
                    <div className="flex items-center gap-2">
                        {step > 0 && (
                            <button onClick={() => setStep(step - 1)} data-testid="unblock-tour-back"
                                    className="font-mono text-xs tracking-widest text-[#A1A1AA] hover:text-white px-3 py-1.5">
                                BACK
                            </button>
                        )}
                        {last ? (
                            <button onClick={onClose} data-testid="unblock-tour-done"
                                    className="flex items-center gap-1.5 font-mono text-xs tracking-widest text-[#00FF41] border border-[#00FF41]/40 hover:bg-[#00FF41]/10 px-3 py-1.5">
                                <CheckCircle2 className="w-3.5 h-3.5" /> DONE
                            </button>
                        ) : (
                            <button onClick={() => setStep(step + 1)} data-testid="unblock-tour-next"
                                    className="flex items-center gap-1.5 font-mono text-xs tracking-widest text-[#FFD700] border border-[#FFD700]/40 hover:bg-[#FFD700]/10 px-3 py-1.5">
                                NEXT <ArrowRight className="w-3 h-3" />
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}
