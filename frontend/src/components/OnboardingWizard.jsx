import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import api from "@/lib/api";
import { StoicMark } from "@/components/StoicLogo";
import {
    X, ChevronRight, ChevronLeft, ShieldCheck, Wallet, Download, Rocket,
    CheckCircle2, Eye,
} from "lucide-react";

const RISK_QUIZ = [
    { q: "How much trading experience do you have?",
      opts: [["None / beginner", 0], ["Some — I've traded before", 1], ["Experienced trader", 2]] },
    { q: "A losing week would make you…",
      opts: [["Very uncomfortable — protect capital first", 0], ["Fine, if the process is sound", 1], ["Unbothered — drawdowns are the cost of returns", 2]] },
    { q: "What's the goal?",
      opts: [["Steady, small, compounding gains", 0], ["Balanced growth", 1], ["Maximum growth, higher swings accepted", 2]] },
];

function riskFromScore(score) {
    if (score <= 1) return "low";
    if (score <= 4) return "medium";
    return "high";
}

const STEPS = ["welcome", "risk", "broker", "ea", "demo"];

export default function OnboardingWizard() {
    const [state, setState] = useState(null); // {status, step}
    const [step, setStep] = useState(0);
    const [answers, setAnswers] = useState([]);
    const [savedRisk, setSavedRisk] = useState(null);

    useEffect(() => {
        api.get("/onboarding")
            .then(r => {
                setState(r.data);
                setStep(Math.min(r.data.step || 0, STEPS.length - 1));
                if (r.data.risk_level) setSavedRisk(r.data.risk_level);
            })
            .catch(() => setState({ status: "done" }));
    }, []);

    const persist = useCallback((payload) => {
        api.put("/onboarding", payload).catch(() => {});
    }, []);

    const go = (n) => {
        const next = Math.max(0, Math.min(n, STEPS.length - 1));
        setStep(next);
        persist({ step: next });
    };
    const skip = () => { setState(s => ({ ...s, status: "skipped" })); persist({ status: "skipped" }); };
    const finish = () => { setState(s => ({ ...s, status: "done" })); persist({ status: "done" }); };

    if (!state || state.status !== "pending") return null;

    const answer = (qi, val) => {
        const next = [...answers]; next[qi] = val; setAnswers(next);
        if (next.filter(v => v !== undefined).length === RISK_QUIZ.length) {
            const risk = riskFromScore(next.reduce((a, b) => a + b, 0));
            setSavedRisk(risk);
            persist({ risk_level: risk });
        }
    };

    const stepName = STEPS[step];
    return (
        <div className="fixed inset-0 z-[80] bg-black/85 backdrop-blur-sm flex items-center justify-center p-4"
            data-testid="onboarding-wizard">
            <div className="w-full max-w-2xl bg-[#0A0A0A] border border-[#1F1F1F] max-h-[90vh] overflow-y-auto">
                {/* header */}
                <div className="flex items-center justify-between p-5 border-b border-[#1F1F1F]">
                    <div className="flex items-center gap-3">
                        <StoicMark className="w-6 h-6" />
                        <div className="font-mono text-[10px] tracking-widest text-[#52525B]">
                            SETUP · STEP {step + 1} / {STEPS.length}
                        </div>
                    </div>
                    <button onClick={skip} data-testid="wizard-skip"
                        className="text-[#52525B] hover:text-white flex items-center gap-1 text-[10px] font-mono tracking-widest">
                        SKIP FOR NOW <X className="w-4 h-4" />
                    </button>
                </div>
                {/* progress */}
                <div className="flex gap-1 px-5 pt-4">
                    {STEPS.map((s, i) => (
                        <div key={s} className={`h-1 flex-1 ${i <= step ? "bg-[#00FF41]" : "bg-[#1F1F1F]"}`} />
                    ))}
                </div>

                <div className="p-6 sm:p-8">
                    {stepName === "welcome" && (
                        <div data-testid="wizard-step-welcome">
                            <h2 className="font-display text-2xl text-white mb-3">Welcome to STOIC</h2>
                            <p className="text-sm text-[#A1A1AA] leading-6 mb-4">
                                In the next 2 minutes we'll set your risk profile, connect your broker,
                                install the trading bridge and start you safely in <strong className="text-white">Demo mode</strong>.
                            </p>
                            <div className="space-y-2 text-sm text-[#A1A1AA]">
                                <div className="flex gap-2"><ShieldCheck className="w-4 h-4 text-[#00FF41] mt-0.5 flex-shrink-0" /> Your funds stay with your broker — STOIC is software, never a custodian.</div>
                                <div className="flex gap-2"><Eye className="w-4 h-4 text-[#00FF41] mt-0.5 flex-shrink-0" /> Every trade is explainable — you can always see why the bot acted.</div>
                                <div className="flex gap-2"><CheckCircle2 className="w-4 h-4 text-[#00FF41] mt-0.5 flex-shrink-0" /> Safety rails (drawdown limits, cooldowns, exposure caps) run in every mode.</div>
                            </div>
                            <p className="text-[11px] text-[#52525B] mt-5">
                                Trading leveraged products is risky. Read the <Link to="/risk-disclosure" className="text-[#FFB000] hover:underline" onClick={skip}>Risk Disclosure</Link> before going live.
                            </p>
                        </div>
                    )}

                    {stepName === "risk" && (
                        <div data-testid="wizard-step-risk">
                            <h2 className="font-display text-2xl text-white mb-3">Your risk profile</h2>
                            <p className="text-sm text-[#A1A1AA] mb-5">Three quick questions — we'll preset the bot's risk level. You can change it any time in Bot Config.</p>
                            <div className="space-y-5">
                                {RISK_QUIZ.map((item, qi) => (
                                    <div key={qi}>
                                        <div className="text-sm text-white mb-2">{qi + 1}. {item.q}</div>
                                        <div className="flex flex-col sm:flex-row gap-2">
                                            {item.opts.map(([label, val]) => (
                                                <button key={label} onClick={() => answer(qi, val)}
                                                    data-testid={`wizard-quiz-${qi}-${val}`}
                                                    className={`flex-1 px-3 py-2 text-xs border text-left transition ${
                                                        answers[qi] === val
                                                            ? "border-[#00FF41]/60 bg-[#00FF41]/10 text-[#00FF41]"
                                                            : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                                                    {label}
                                                </button>
                                            ))}
                                        </div>
                                    </div>
                                ))}
                            </div>
                            {savedRisk && (
                                <div className="mt-5 border border-[#00FF41]/30 bg-[#00FF41]/5 px-4 py-3 text-sm text-[#00FF41]"
                                    data-testid="wizard-risk-result">
                                    Profile set: <strong className="uppercase">{savedRisk}</strong> — applied to your bot configuration.
                                </div>
                            )}
                        </div>
                    )}

                    {stepName === "broker" && (
                        <div data-testid="wizard-step-broker">
                            <h2 className="font-display text-2xl text-white mb-3 flex items-center gap-2"><Wallet className="w-5 h-5 text-[#00FF41]" /> Connect your broker</h2>
                            <ol className="list-decimal ml-5 space-y-2 text-sm text-[#A1A1AA] mb-4">
                                <li>Open your MT5 terminal and note the <strong className="text-white">account number</strong> and <strong className="text-white">server name</strong> (top-left of the terminal).</li>
                                <li>Go to <strong className="text-white">MT5 Accounts</strong> and click ADD ACCOUNT.</li>
                                <li>Enter both values exactly — identity verification depends on the match.</li>
                            </ol>
                            <p className="text-[11px] text-[#52525B] mb-4">Tip: a demo account from your broker is perfect for the first week.</p>
                            <Link to="/accounts" onClick={() => persist({ step: 3 })} data-testid="wizard-goto-accounts"
                                className="inline-block px-4 py-2 border border-[#00FF41]/50 text-[#00FF41] text-xs font-mono tracking-widest hover:bg-[#00FF41]/10">
                                OPEN MT5 ACCOUNTS →
                            </Link>
                            <p className="text-[11px] text-[#52525B] mt-3">The wizard remembers where you left off — come back any time.</p>
                        </div>
                    )}

                    {stepName === "ea" && (
                        <div data-testid="wizard-step-ea">
                            <h2 className="font-display text-2xl text-white mb-3 flex items-center gap-2"><Download className="w-5 h-5 text-[#00FF41]" /> Install the STOIC EA</h2>
                            <ol className="list-decimal ml-5 space-y-2 text-sm text-[#A1A1AA] mb-4">
                                <li>Download <code className="bg-[#1F1F1F] text-[#FFD700] px-1.5 rounded text-[11px]">EmergentTradingBridge.mq5</code> from the Accounts page.</li>
                                <li>MT5: File → Open Data Folder → <code className="bg-[#1F1F1F] text-[#FFD700] px-1.5 rounded text-[11px]">MQL5/Experts</code>, copy it there.</li>
                                <li>MetaEditor (F4) → open the file → compile with <strong className="text-white">F7</strong>.</li>
                                <li>Drag the EA onto any chart, paste your <strong className="text-white">bridge token</strong>, enable Allow Algorithmic Trading.</li>
                            </ol>
                            <p className="text-sm text-[#A1A1AA]">Within ~60s your account flips to <strong className="text-[#00FF41]">CONNECTED</strong> on the Accounts page.</p>
                        </div>
                    )}

                    {stepName === "demo" && (
                        <div data-testid="wizard-step-demo">
                            <h2 className="font-display text-2xl text-white mb-3 flex items-center gap-2"><Rocket className="w-5 h-5 text-[#00FF41]" /> Start in Demo mode</h2>
                            <p className="text-sm text-[#A1A1AA] leading-6 mb-4">
                                Open <strong className="text-white">Bot Config</strong>, confirm your risk level
                                {savedRisk && <> (<span className="text-[#00FF41] uppercase">{savedRisk}</span>)</>} and toggle the bot ON.
                                In Demo mode the bot trades with zero capital at risk while you watch how it thinks.
                            </p>
                            <div className="space-y-2 text-sm text-[#A1A1AA] mb-5">
                                <div className="flex gap-2"><CheckCircle2 className="w-4 h-4 text-[#00FF41] mt-0.5 flex-shrink-0" /> The Dashboard BOT PULSE shows every decision live.</div>
                                <div className="flex gap-2"><CheckCircle2 className="w-4 h-4 text-[#00FF41] mt-0.5 flex-shrink-0" /> Run Demo for at least a week before considering live modes.</div>
                                <div className="flex gap-2"><CheckCircle2 className="w-4 h-4 text-[#00FF41] mt-0.5 flex-shrink-0" /> Help Center → step-by-step guides whenever you're stuck.</div>
                            </div>
                            <Link to="/bot" onClick={finish} data-testid="wizard-goto-bot"
                                className="inline-block px-4 py-2 bg-[#00FF41] text-black text-xs font-mono tracking-widest mr-3">
                                OPEN BOT CONFIG →
                            </Link>
                        </div>
                    )}
                </div>

                {/* footer nav */}
                <div className="flex items-center justify-between p-5 border-t border-[#1F1F1F]">
                    <button onClick={() => go(step - 1)} disabled={step === 0} data-testid="wizard-prev"
                        className="px-4 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] disabled:opacity-30 flex items-center gap-1.5 hover:border-[#52525B]">
                        <ChevronLeft className="w-3.5 h-3.5" /> BACK
                    </button>
                    {step < STEPS.length - 1 ? (
                        <button onClick={() => go(step + 1)} data-testid="wizard-next"
                            className="px-5 py-2 text-xs font-mono tracking-widest bg-[#00FF41] text-black flex items-center gap-1.5">
                            {stepName === "risk" && !savedRisk ? "SKIP QUIZ" : "CONTINUE"} <ChevronRight className="w-3.5 h-3.5" />
                        </button>
                    ) : (
                        <button onClick={finish} data-testid="wizard-finish"
                            className="px-5 py-2 text-xs font-mono tracking-widest bg-[#00FF41] text-black">
                            FINISH SETUP
                        </button>
                    )}
                </div>
            </div>
        </div>
    );
}
