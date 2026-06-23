import { useEffect, useRef, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { useLiveStream } from "@/lib/useLiveStream";
import { toast } from "sonner";
import {
    Send, Sparkles, ShieldAlert, Bell, X, Clock, CheckCircle2,
    Bot, MessageSquare
} from "lucide-react";

const EXAMPLE_COMMANDS = [
    "if Bitcoin drops 4% disable my high-risk bots and move gold stops to break-even",
    "close all my open trades right now",
    "switch my risk level to low",
    "if gold rises 2% from here, disable all bots",
    "enable high-risk bots only",
];

const EXAMPLE_STRATEGIES = [
    "Conservative gold scalping during London session, max 2 trades",
    "Aggressive Bitcoin swing trading with auto-execute",
    "Balanced multi-asset trend follower on XAUUSD and BTCUSD",
];

function Pill({ children, className = "" }) {
    return (
        <span className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${className}`}>
            {children}
        </span>
    );
}

export default function RiskCommander() {
    const [mode, setMode] = useState("command"); // 'command' | 'strategy'
    const [prompt, setPrompt] = useState("");
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState("");
    const [history, setHistory] = useState([]); // {id, role:user|ai, content, receipts?}

    // Monotonic counter for chat-row keys. Avoids index-as-key (stale after edits/deletes).
    const msgIdRef = useRef(0);
    const newMsg = (m) => ({ id: ++msgIdRef.current, ...m });
    const [triggers, setTriggers] = useState([]);
    const [compiledPreview, setCompiledPreview] = useState(null);
    const { lastEvent } = useLiveStream();

    const loadTriggers = async () => {
        try {
            const { data } = await api.get("/nl/triggers");
            setTriggers(data);
        } catch (e) {
            console.warn("[risk-commander] failed to load triggers", e?.message);
        }
    };

    useEffect(() => { loadTriggers(); }, []);

    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "trigger_fired") {
            const p = lastEvent.payload || {};
            toast.error(`Trigger fired · ${p.symbol} ${p.condition} ${p.threshold_pct}%`, {
                description: `Δ ${p.change_pct}%, actions executed.`,
            });
            loadTriggers();
        }
        if (lastEvent.type === "nl_command_executed") {
            // ignore — we already handled UI ourselves
        }
    }, [lastEvent]);

    const submit = async () => {
        if (!prompt.trim()) return;
        setErr(""); setLoading(true);
        const userMsg = newMsg({ role: "user", content: prompt });
        setHistory(h => [...h, userMsg]);
        try {
            if (mode === "command") {
                const { data } = await api.post("/nl/command", { prompt });
                if (data.clarification_needed) {
                    setHistory(h => [...h, newMsg({ role: "ai", content: data.clarification_needed, isQuestion: true })]);
                } else {
                    setHistory(h => [...h, newMsg({
                        role: "ai",
                        content: data.summary,
                        receipts: data.receipts,
                    })]);
                    toast.success("Command executed", { description: data.summary });
                    await loadTriggers();
                }
            } else {
                const { data } = await api.post("/nl/strategy", { prompt });
                if (data.compiled?.clarification_needed) {
                    setHistory(h => [...h, newMsg({ role: "ai", content: data.compiled.clarification_needed, isQuestion: true })]);
                } else {
                    setCompiledPreview(data.compiled);
                    setHistory(h => [...h, newMsg({
                        role: "ai",
                        content: data.compiled.notes || "Strategy compiled — review below.",
                        compiled: data.compiled,
                    })]);
                }
            }
            setPrompt("");
        } catch (e) {
            setErr(formatApiError(e));
        } finally { setLoading(false); }
    };

    const applyStrategy = async () => {
        if (!compiledPreview) return;
        try {
            await api.post("/nl/strategy/apply", { compiled: compiledPreview });
            toast.success("Strategy applied to bot config");
            setCompiledPreview(null);
        } catch (e) {
            toast.error("Apply failed", { description: formatApiError(e) });
        }
    };

    const cancelTrigger = async (id) => {
        try {
            await api.delete(`/nl/triggers/${id}`);
            await loadTriggers();
        } catch (e) {
            toast.error("Couldn't cancel trigger", { description: formatApiError(e) });
        }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Risk Commander"
                subtitle="Speak to your bot — natural-language strategies & circuit breakers."
                testid="risk-commander-header"
                action={
                    <div className="flex items-center gap-2">
                        <button onClick={() => setMode("command")}
                            data-testid="mode-command"
                            className={`px-3 py-2 text-xs font-mono tracking-widest border transition-colors ${
                                mode === "command"
                                    ? "border-[#00FF41] text-[#00FF41] bg-[#00FF41]/5"
                                    : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                            }`}>
                            <ShieldAlert className="w-3.5 h-3.5 inline mr-1" /> COMMAND
                        </button>
                        <button onClick={() => setMode("strategy")}
                            data-testid="mode-strategy"
                            className={`px-3 py-2 text-xs font-mono tracking-widest border transition-colors ${
                                mode === "strategy"
                                    ? "border-[#FFB000] text-[#FFB000] bg-[#FFB000]/5"
                                    : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                            }`}>
                            <Sparkles className="w-3.5 h-3.5 inline mr-1" /> STRATEGY
                        </button>
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-6 max-w-4xl">
                {err && (
                    <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono"
                        data-testid="risk-commander-error">
                        {err}
                    </div>
                )}

                {/* Active triggers */}
                {triggers.length > 0 && (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="active-triggers-panel">
                        <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                            <Bell className="w-4 h-4 text-[#FFB000]" />
                            <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                ACTIVE CONDITIONAL TRIGGERS · {triggers.length}
                            </span>
                        </div>
                        <div className="divide-y divide-[#1F1F1F]">
                            {triggers.map(t => (
                                <div key={t.id} className="px-5 py-3 flex items-center gap-3" data-testid={`trigger-${t.id}`}>
                                    <Pill className="text-[#FFB000] border-[#FFB000]/40">{t.symbol}</Pill>
                                    <span className="font-mono text-xs text-[#A1A1AA]">
                                        IF {t.condition?.toUpperCase()} ≥ {t.threshold_pct}%
                                    </span>
                                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                        baseline ${t.baseline_price?.toFixed?.(2) || "pending"}
                                    </span>
                                    <span className="flex-1 font-mono text-[10px] text-[#52525B] tracking-widest truncate">
                                        THEN: {(t.then || []).map(a => a.type).join(" → ")}
                                    </span>
                                    <button onClick={() => cancelTrigger(t.id)}
                                        data-testid={`cancel-trigger-${t.id}`}
                                        className="text-[#52525B] hover:text-[#FF3B30]">
                                        <X className="w-4 h-4" />
                                    </button>
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Chat history */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] min-h-[300px]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <MessageSquare className="w-4 h-4 text-[#00FF41]" />
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                            {mode === "command" ? "RISK COMMAND CHAT" : "STRATEGY BUILDER CHAT"}
                        </span>
                    </div>
                    <div className="p-5 space-y-4">
                        {history.length === 0 && (
                            <div className="font-mono text-xs text-[#52525B] tracking-widest text-center py-8">
                                {mode === "command"
                                    ? "Speak to your portfolio. Try: \"if Bitcoin drops 3%, disable my high-risk bots\""
                                    : "Describe your trading strategy. Try: \"Conservative gold scalping during London hours\""}
                            </div>
                        )}
                        {history.map((m, i) => (
                            <div key={m.id} className={`flex gap-3 ${m.role === "user" ? "justify-end" : ""}`}
                                data-testid={`msg-${i}`}>
                                {m.role !== "user" && (
                                    <div className="w-6 h-6 rounded-none bg-[#00FF41]/20 text-[#00FF41] flex items-center justify-center shrink-0">
                                        <Bot className="w-3.5 h-3.5" />
                                    </div>
                                )}
                                <div className={`max-w-[80%] ${m.role === "user" ? "" : ""}`}>
                                    <div className={`p-3 border text-sm leading-relaxed ${
                                        m.role === "user"
                                            ? "bg-[#121212] border-[#1F1F1F] text-white"
                                            : m.isQuestion
                                                ? "bg-[#FFB000]/5 border-[#FFB000]/30 text-[#FFB000]"
                                                : "bg-[#00FF41]/5 border-[#00FF41]/30 text-white"
                                    }`}>
                                        {m.content}
                                    </div>
                                    {m.receipts?.length > 0 && (
                                        <div className="mt-2 space-y-1" data-testid={`receipts-${i}`}>
                                            {m.receipts.map((r, j) => (
                                                <div key={`${m.id}-r${j}`} className="font-mono text-[10px] text-[#A1A1AA] flex items-center gap-2">
                                                    <CheckCircle2 className="w-3 h-3 text-[#00FF41]" />
                                                    <span className="text-[#00FF41]">{r.type}</span>
                                                    <span className="text-[#52525B]">·</span>
                                                    <span className="text-[#A1A1AA] truncate">{JSON.stringify(r.result || r.error)}</span>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                    {m.compiled && (
                                        <div className="mt-2 p-3 border border-[#FFB000]/30 bg-[#FFB000]/5" data-testid={`compiled-${i}`}>
                                            <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-2">COMPILED STRATEGY</div>
                                            <pre className="font-mono text-[10px] text-[#A1A1AA] whitespace-pre-wrap">{JSON.stringify(m.compiled, null, 2)}</pre>
                                        </div>
                                    )}
                                </div>
                            </div>
                        ))}
                    </div>
                </div>

                {/* Strategy apply button */}
                {compiledPreview && (
                    <div className="border border-[#FFB000]/40 bg-[#FFB000]/5 p-4 flex items-center justify-between"
                        data-testid="apply-strategy-panel">
                        <div className="text-sm">
                            <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-1">
                                READY TO APPLY
                            </div>
                            <div>Compiled strategy is ready. Applying will overwrite your current bot configuration.</div>
                        </div>
                        <div className="flex gap-2">
                            <button onClick={() => setCompiledPreview(null)}
                                data-testid="dismiss-strategy"
                                className="px-3 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]">
                                DISMISS
                            </button>
                            <button onClick={applyStrategy}
                                data-testid="apply-strategy-button"
                                className="px-4 py-2 text-xs font-mono tracking-widest bg-[#FFB000] text-black hover:bg-[#E59E00]">
                                APPLY TO BOT
                            </button>
                        </div>
                    </div>
                )}

                {/* Input */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="p-3 flex gap-2">
                        <input
                            value={prompt}
                            onChange={e => setPrompt(e.target.value)}
                            onKeyDown={e => e.key === "Enter" && !loading && submit()}
                            data-testid="risk-commander-input"
                            placeholder={mode === "command"
                                ? "Tell your bot what to do…"
                                : "Describe your strategy…"}
                            className="flex-1 bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none transition-colors" />
                        <button
                            onClick={submit}
                            disabled={loading || !prompt.trim()}
                            data-testid="risk-commander-send"
                            className="px-4 py-2 text-xs font-mono tracking-widest bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-40 text-black flex items-center gap-1.5">
                            <Send className="w-3.5 h-3.5" />
                            {loading ? "PROCESSING…" : "SEND"}
                        </button>
                    </div>
                    <div className="px-3 pb-3 flex flex-wrap gap-1.5">
                        {(mode === "command" ? EXAMPLE_COMMANDS : EXAMPLE_STRATEGIES).map((ex, i) => (
                            <button key={ex} onClick={() => setPrompt(ex)}
                                data-testid={`example-${i}`}
                                className="font-mono text-[10px] text-[#A1A1AA] hover:text-[#00FF41] bg-[#121212] border border-[#1F1F1F] px-2 py-1 transition-colors">
                                <Clock className="w-3 h-3 inline mr-1" />{ex}
                            </button>
                        ))}
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
