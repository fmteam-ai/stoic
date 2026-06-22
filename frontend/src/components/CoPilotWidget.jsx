import { useState, useEffect, useRef } from "react";
import { useAuth } from "@/context/AuthContext";
import api, { formatApiError } from "@/lib/api";
import { captureViewport, collectContext } from "@/lib/diagnostics";
import { Bot, MessageCircle, X, Send, Sparkles, Loader2, Bug, CheckCircle2 } from "lucide-react";

const STORAGE_KEY = "copilot_session_id";

const QUICK_ACTIONS = [
    "Why is my bot in HOLD?",
    "Explain my last signal",
    "What's my drawdown today?",
    "Is my bot performing well?",
    "Co-Pilot, file me a screenshot bug report",
];

// Match anything that *sounds* like a bug-report request.
const BUG_INTENT = /\b(file|submit|send|report|log)\b.*\b(bug|issue|problem|broken|glitch|screenshot)\b|\bsomething.*(wrong|broken|weird|off)\b|\breport this\b/i;

/**
 * Floating AI Co-Pilot widget — bottom-right on every authenticated page.
 * Grounded in the user's live state via /api/copilot/chat.
 */
export function CoPilotWidget() {
    const { user } = useAuth();
    const [open, setOpen] = useState(false);
    const [messages, setMessages] = useState([]);
    const [input, setInput] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const sessionRef = useRef(localStorage.getItem(STORAGE_KEY));
    const scrollRef = useRef(null);

    // Bug-report flow state
    const [bugDraft, setBugDraft] = useState(null); // { description, screenshot } | null
    const [capturing, setCapturing] = useState(false);
    const [submittingBug, setSubmittingBug] = useState(false);

    const beginBugCapture = async () => {
        // Close the widget so the screenshot doesn't show the chat panel
        setOpen(false);
        setCapturing(true);
        // Let the panel fully unmount before snap
        await new Promise((r) => setTimeout(r, 350));
        const screenshot = await captureViewport();
        const ctx = collectContext();
        setCapturing(false);
        setBugDraft({ description: "", screenshot, ...ctx });
        setOpen(true);
    };

    const submitBug = async () => {
        if (!bugDraft || !bugDraft.description.trim()) return;
        setSubmittingBug(true);
        try {
            const { data } = await api.post("/bugs", {
                description: bugDraft.description.trim(),
                screenshot: bugDraft.screenshot,
                url: bugDraft.url,
                user_agent: bugDraft.user_agent,
                viewport: bugDraft.viewport,
                console_logs: bugDraft.console_logs,
                via: "copilot",
            });
            setMessages((prev) => [...prev, {
                role: "assistant",
                content: `Bug filed (id: ${data.id.slice(-6)}). Thanks — our team can see your screenshot, URL, and the last ${bugDraft.console_logs?.length || 0} console events. We'll review it shortly.`,
                ts: new Date().toISOString(),
                isBugReceipt: true,
            }]);
            setBugDraft(null);
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setSubmittingBug(false);
        }
    };

    // Load prior session messages when widget opens for the first time
    useEffect(() => {
        if (!open || !user || messages.length || !sessionRef.current) return;
        let cancel = false;
        (async () => {
            try {
                const { data } = await api.get(`/copilot/sessions/${sessionRef.current}`);
                if (!cancel) setMessages(data.messages || []);
            } catch (e) {
                // Session not found — start fresh
                localStorage.removeItem(STORAGE_KEY);
                sessionRef.current = null;
                console.warn("[copilot] previous session gone, starting fresh", e?.message);
            }
        })();
        return () => { cancel = true; };
    }, [open, user, messages.length]);

    // Auto-scroll on new messages
    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
    }, [messages, busy]);

    if (!user || !user.id) return null;

    const send = async (textOverride) => {
        const text = (textOverride ?? input).trim();
        if (!text || busy) return;
        setErr("");
        // Intercept bug-report intent: skip Claude, run capture flow.
        if (BUG_INTENT.test(text)) {
            setMessages((prev) => [
                ...prev,
                { role: "user", content: text, ts: new Date().toISOString() },
                {
                    role: "assistant",
                    content: "Got it — capturing your current screen + recent console events. One sec…",
                    ts: new Date().toISOString(),
                },
            ]);
            setInput("");
            await beginBugCapture();
            return;
        }
        const userMsg = { role: "user", content: text, ts: new Date().toISOString() };
        setMessages((prev) => [...prev, userMsg]);
        setInput("");
        setBusy(true);
        try {
            const { data } = await api.post("/copilot/chat", {
                message: text,
                session_id: sessionRef.current,
            });
            sessionRef.current = data.session_id;
            localStorage.setItem(STORAGE_KEY, data.session_id);
            setMessages((prev) => [...prev, {
                role: "assistant", content: data.answer, ts: new Date().toISOString(),
            }]);
        } catch (e) {
            setErr(formatApiError(e));
            setMessages((prev) => [...prev, {
                role: "assistant", content: "Sorry — I couldn't reach the AI backend just now. Try again in a moment.",
                error: true, ts: new Date().toISOString(),
            }]);
        } finally {
            setBusy(false);
        }
    };

    const clearSession = () => {
        localStorage.removeItem(STORAGE_KEY);
        sessionRef.current = null;
        setMessages([]);
    };

    return (
        <>
            {/* Capturing overlay */}
            {capturing && (
                <div data-testid="copilot-capturing" data-no-capture
                    className="fixed inset-0 z-[100] bg-black/40 flex items-center justify-center pointer-events-none">
                    <div className="bg-[#0A0A0A] border border-[#00FF41]/40 px-6 py-4 flex items-center gap-3">
                        <Loader2 className="w-5 h-5 animate-spin text-[#00FF41]" />
                        <span className="font-mono text-xs tracking-widest text-[#00FF41]">CAPTURING SCREEN…</span>
                    </div>
                </div>
            )}

            {/* Launcher */}
            {!open && !capturing && (
                <button
                    onClick={() => setOpen(true)}
                    data-testid="copilot-launcher"
                    data-no-capture
                    className="fixed bottom-24 right-6 z-40 w-14 h-14 bg-[#00FF41] hover:bg-[#00E53A] text-black shadow-[0_0_20px_rgba(0,255,65,0.4)] flex items-center justify-center transition-all hover:scale-105"
                    aria-label="Open AI Co-Pilot"
                >
                    <MessageCircle className="w-6 h-6" />
                    <span className="absolute -top-1 -right-1 w-3 h-3 bg-[#FFB000] rounded-full animate-pulse" />
                </button>
            )}

            {/* Panel */}
            {open && (
                <div
                    data-testid="copilot-panel"
                    data-no-capture
                    className="fixed bottom-0 right-0 md:bottom-20 md:right-6 z-50 w-full md:w-[420px] h-[88vh] md:h-[600px] bg-[#0A0A0A] border border-[#1F1F1F] md:shadow-[0_0_40px_rgba(0,0,0,0.6)] flex flex-col"
                >
                    {/* Header */}
                    <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center justify-between bg-[#050505]">
                        <div className="flex items-center gap-2">
                            <div className="w-7 h-7 bg-[#00FF41]/20 text-[#00FF41] flex items-center justify-center">
                                <Bot className="w-4 h-4" />
                            </div>
                            <div>
                                <div className="font-display font-bold text-sm tracking-tight">AI Co-Pilot</div>
                                <div className="font-mono text-[9px] text-[#52525B] tracking-widest">
                                    GROUNDED IN YOUR LIVE STATE · CLAUDE 4.5
                                </div>
                            </div>
                        </div>
                        <div className="flex items-center gap-1">
                            <button
                                onClick={clearSession}
                                data-testid="copilot-clear"
                                title="Start a new conversation"
                                className="px-2 py-1 text-[10px] font-mono tracking-widest text-[#52525B] hover:text-[#FFB000] transition-colors"
                            >
                                NEW
                            </button>
                            <button
                                onClick={() => setOpen(false)}
                                data-testid="copilot-close"
                                className="p-1 text-[#A1A1AA] hover:text-white transition-colors"
                            >
                                <X className="w-4 h-4" />
                            </button>
                        </div>
                    </div>

                    {/* Messages */}
                    <div
                        ref={scrollRef}
                        data-testid="copilot-messages"
                        className="flex-1 overflow-y-auto p-4 space-y-3 bg-[#050505]"
                    >
                        {messages.length === 0 && (
                            <div className="space-y-4">
                                <div className="text-center py-6">
                                    <Sparkles className="w-8 h-8 text-[#00FF41] mx-auto mb-2" />
                                    <div className="font-display font-bold text-lg tracking-tight mb-1">
                                        Hi {user.name || user.email?.split("@")[0]} 👋
                                    </div>
                                    <p className="text-sm text-[#A1A1AA] leading-relaxed px-4">
                                        I&apos;m your AI Co-Pilot. I can see your live signals, trades,
                                        regime, and bot config. Ask me anything.
                                    </p>
                                </div>
                                <div className="space-y-1.5">
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest px-1">
                                        TRY ASKING
                                    </div>
                                    {QUICK_ACTIONS.map((q) => (
                                        <button
                                            key={q}
                                            onClick={() => send(q)}
                                            data-testid={`copilot-quick-${q.slice(0, 18).replace(/\s+/g, "-")}`}
                                            className="w-full text-left px-3 py-2 text-xs bg-[#0A0A0A] border border-[#1F1F1F] hover:border-[#00FF41]/40 hover:bg-[#00FF41]/5 transition-colors"
                                        >
                                            {q}
                                        </button>
                                    ))}
                                </div>
                            </div>
                        )}

                        {messages.map((m, i) => (
                            <div
                                key={`${m.ts}-${i}`}
                                data-testid={`copilot-msg-${i}`}
                                className={`flex gap-2 ${m.role === "user" ? "justify-end" : ""}`}
                            >
                                {m.role !== "user" && (
                                    <div className="w-6 h-6 bg-[#00FF41]/20 text-[#00FF41] flex items-center justify-center shrink-0">
                                        <Bot className="w-3.5 h-3.5" />
                                    </div>
                                )}
                                <div
                                    className={`max-w-[85%] px-3 py-2 text-sm leading-relaxed whitespace-pre-wrap ${
                                        m.role === "user"
                                            ? "bg-[#121212] border border-[#1F1F1F]"
                                            : m.error
                                                ? "bg-[#FF3B30]/10 border border-[#FF3B30]/30 text-[#FF3B30]"
                                                : "bg-[#00FF41]/5 border border-[#00FF41]/20"
                                    }`}
                                >
                                    {m.content}
                                </div>
                            </div>
                        ))}

                        {busy && (
                            <div className="flex gap-2" data-testid="copilot-typing">
                                <div className="w-6 h-6 bg-[#00FF41]/20 text-[#00FF41] flex items-center justify-center shrink-0">
                                    <Bot className="w-3.5 h-3.5" />
                                </div>
                                <div className="bg-[#00FF41]/5 border border-[#00FF41]/20 px-3 py-2 flex items-center gap-2">
                                    <Loader2 className="w-3 h-3 animate-spin text-[#00FF41]" />
                                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                        ANALYSING YOUR STATE…
                                    </span>
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Bug-draft preview */}
                    {bugDraft && (
                        <div data-testid="bug-draft" className="border-t border-[#1F1F1F] bg-[#FFB000]/5 p-3 space-y-2">
                            <div className="flex items-center gap-2">
                                <Bug className="w-4 h-4 text-[#FFB000]" />
                                <span className="font-mono text-[10px] text-[#FFB000] tracking-widest">
                                    BUG REPORT · REVIEW &amp; SEND
                                </span>
                            </div>
                            {bugDraft.screenshot && (
                                <div className="border border-[#1F1F1F] bg-[#050505] p-1">
                                    <img src={bugDraft.screenshot} alt="captured screen"
                                        data-testid="bug-screenshot-preview"
                                        className="w-full h-24 object-cover object-top opacity-90" />
                                </div>
                            )}
                            <textarea
                                value={bugDraft.description}
                                onChange={(e) => setBugDraft({ ...bugDraft, description: e.target.value })}
                                data-testid="bug-description"
                                placeholder="Describe what's broken or unexpected…"
                                rows={3}
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFB000] px-2 py-1.5 text-xs font-mono outline-none resize-none" />
                            <div className="flex items-center justify-between gap-2">
                                <span className="font-mono text-[9px] text-[#52525B] tracking-widest">
                                    {(bugDraft.console_logs || []).length} CONSOLE EVENTS · {bugDraft.url?.split("/").pop() || "unknown"}
                                </span>
                                <div className="flex gap-1.5">
                                    <button onClick={() => setBugDraft(null)}
                                        data-testid="bug-cancel"
                                        className="px-2.5 py-1.5 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]">
                                        CANCEL
                                    </button>
                                    <button onClick={submitBug}
                                        disabled={submittingBug || !bugDraft.description.trim()}
                                        data-testid="bug-submit"
                                        className="px-3 py-1.5 text-[10px] font-mono tracking-widest bg-[#FFB000] text-black hover:bg-[#E59E00] disabled:opacity-40 flex items-center gap-1">
                                        {submittingBug ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
                                        SEND
                                    </button>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Input */}
                    <div className="border-t border-[#1F1F1F] p-3 bg-[#0A0A0A]">
                        {err && (
                            <div className="mb-2 text-[10px] font-mono text-[#FF3B30] tracking-widest" data-testid="copilot-error">
                                {err}
                            </div>
                        )}
                        <div className="flex gap-2">
                            <input
                                value={input}
                                onChange={(e) => setInput(e.target.value)}
                                onKeyDown={(e) => e.key === "Enter" && send()}
                                disabled={busy}
                                data-testid="copilot-input"
                                placeholder="Ask anything about your bot…"
                                className="flex-1 bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none transition-colors disabled:opacity-50"
                            />
                            <button
                                onClick={() => send()}
                                disabled={busy || !input.trim()}
                                data-testid="copilot-send"
                                className="px-3 py-2 bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-40 text-black flex items-center"
                            >
                                <Send className="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </>
    );
}
