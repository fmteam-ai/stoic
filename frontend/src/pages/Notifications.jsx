import { useEffect, useState } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    Bell, Send, Save as FloppyDisk, CheckCircle2, ExternalLink, KeyRound,
    AlertTriangle, MessageSquare,
} from "lucide-react";

const EVENT_LABELS = {
    trade_opened: { label: "Trade Opened", desc: "When a new trade is filled on MT5 / paper engine", color: "#00FF41" },
    trade_closed: { label: "Trade Closed", desc: "When SL/TP hits or manual close", color: "#A1A1AA" },
    breakeven: { label: "Break-Even Set", desc: "When SL auto-shifts to entry after +1R", color: "#FFD700" },
    partial_close: { label: "Partial Close at TP1", desc: "When 50% closes at the first target", color: "#00FF41" },
    trail: { label: "SL Trailed", desc: "Each time trailing SL moves (can be frequent — opt-in)", color: "#A1A1AA" },
    circuit_breaker: { label: "Circuit Breaker Tripped", desc: "When daily drawdown limit auto-stops the bot", color: "#FF3B30" },
    high_conf_signal: { label: "High-Confidence Signal", desc: "When AI generates a BUY/SELL with confidence ≥75%", color: "#FFD700" },
};

export default function Notifications() {
    const [cfg, setCfg] = useState(null);
    const [token, setToken] = useState("");
    const [chatId, setChatId] = useState("");
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [err, setErr] = useState("");

    const load = async () => {
        try {
            const { data } = await api.get("/notifications/telegram");
            setCfg(data);
            setChatId(data.telegram_chat_id || "");
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    };
    useEffect(() => { load(); }, []);

    const save = async () => {
        setSaving(true); setErr("");
        try {
            const payload = {
                telegram_enabled: cfg.telegram_enabled,
                telegram_chat_id: chatId.trim() || null,
                alerts: cfg.alerts,
            };
            if (token.trim()) payload.telegram_bot_token = token.trim();
            const { data } = await api.put("/notifications/telegram", payload);
            setCfg(data);
            setToken("");
            toast.success("Notification settings saved");
        } catch (e) { setErr(formatApiError(e)); }
        finally { setSaving(false); }
    };

    const sendTest = async () => {
        setTesting(true); setErr("");
        try {
            await api.post("/notifications/telegram/test");
            toast.success("Test message sent — check your Telegram", { duration: 5000 });
        } catch (e) { setErr(formatApiError(e)); }
        finally { setTesting(false); }
    };

    const toggleAlert = (key) => {
        setCfg({ ...cfg, alerts: { ...cfg.alerts, [key]: !cfg.alerts[key] } });
    };

    if (loading) return <AppLayout><div className="p-8 font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div></AppLayout>;

    return (
        <AppLayout>
            <PageHeader
                title="Notifications"
                subtitle="Push alerts via Telegram for trade events, profit protection actions and circuit breakers."
                testid="notifications-header"
                action={
                    <div className={`flex items-center gap-2 font-mono text-[10px] px-3 py-2 border tracking-widest ${
                        cfg?.telegram_enabled && cfg?.has_token ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#1F1F1F] text-[#52525B]"
                    }`}>
                        <Bell className="w-3 h-3" /> {cfg?.telegram_enabled && cfg?.has_token ? "ACTIVE" : "INACTIVE"}
                    </div>
                }
            />

            <div className="p-4 md:p-8 space-y-6 max-w-4xl">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="notif-error">{err}</div>}

                {/* Setup guide */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 01</div>
                        <div className="font-display font-bold text-lg tracking-tight">Setup Your Telegram Bot</div>
                    </div>
                    <ol className="p-5 space-y-3 text-sm text-[#A1A1AA] leading-relaxed">
                        <li className="flex gap-3">
                            <span className="font-mono text-[10px] text-[#FFD700] tracking-widest border border-[#FFD700]/40 px-2 py-0.5 shrink-0 h-fit mt-0.5">STEP 1</span>
                            <div>Open Telegram, message <a href="https://t.me/BotFather" target="_blank" rel="noreferrer" className="text-[#FFD700] hover:underline inline-flex items-center gap-1">@BotFather <ExternalLink className="w-3 h-3" /></a>, send <code className="font-mono bg-[#121212] px-1 py-0.5 text-[#FFD700]">/newbot</code>, and pick a name (e.g. <em>MyStoicAlerts</em>). BotFather replies with a <strong>bot token</strong> — copy it.</div>
                        </li>
                        <li className="flex gap-3">
                            <span className="font-mono text-[10px] text-[#FFD700] tracking-widest border border-[#FFD700]/40 px-2 py-0.5 shrink-0 h-fit mt-0.5">STEP 2</span>
                            <div>Open your new bot in Telegram and send it any message (e.g. <code className="font-mono bg-[#121212] px-1 py-0.5">/start</code>). This is required for it to be allowed to message you.</div>
                        </li>
                        <li className="flex gap-3">
                            <span className="font-mono text-[10px] text-[#FFD700] tracking-widest border border-[#FFD700]/40 px-2 py-0.5 shrink-0 h-fit mt-0.5">STEP 3</span>
                            <div>Get your <strong>chat ID</strong> — easiest way: message <a href="https://t.me/userinfobot" target="_blank" rel="noreferrer" className="text-[#FFD700] hover:underline inline-flex items-center gap-1">@userinfobot <ExternalLink className="w-3 h-3" /></a>, it replies with your numeric ID.</div>
                        </li>
                        <li className="flex gap-3">
                            <span className="font-mono text-[10px] text-[#FFD700] tracking-widest border border-[#FFD700]/40 px-2 py-0.5 shrink-0 h-fit mt-0.5">STEP 4</span>
                            <div>Paste both below, save, then click <strong>Send Test</strong>. You should get a test message on Telegram within ~2 seconds.</div>
                        </li>
                    </ol>
                </div>

                {/* Credentials */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 02</div>
                        <div className="font-display font-bold text-lg tracking-tight">Bot Token & Chat ID</div>
                    </div>
                    <div className="p-5 space-y-4">
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5 flex items-center gap-2">
                                <KeyRound className="w-3 h-3" /> BOT TOKEN
                                {cfg?.has_token && <span className="text-[#00FF41]">· stored encrypted</span>}
                            </label>
                            <input type="password" autoComplete="new-password" value={token}
                                onChange={e => setToken(e.target.value)}
                                data-testid="telegram-token-input"
                                placeholder={cfg?.has_token ? "•••••••••• (leave blank to keep existing)" : "1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"}
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                        </div>
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5 flex items-center gap-2">
                                <MessageSquare className="w-3 h-3" /> CHAT ID
                            </label>
                            <input value={chatId} onChange={e => setChatId(e.target.value)}
                                data-testid="telegram-chatid-input"
                                placeholder="e.g. 123456789"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                        </div>
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">ENABLE TELEGRAM ALERTS</label>
                            <button onClick={() => setCfg({ ...cfg, telegram_enabled: !cfg.telegram_enabled })}
                                data-testid="telegram-enabled-toggle"
                                className={`w-full px-3 py-2 text-sm font-mono tracking-widest border transition-colors ${
                                    cfg.telegram_enabled ? "bg-[#00FF41]/10 border-[#00FF41] text-[#00FF41]" : "bg-[#0A0A0A] border-[#1F1F1F] text-[#A1A1AA]"
                                }`}>
                                {cfg.telegram_enabled ? "● ENABLED" : "○ DISABLED"}
                            </button>
                        </div>
                        <div className="flex gap-2 justify-end pt-2">
                            <button onClick={sendTest} disabled={testing || !cfg.has_token}
                                data-testid="telegram-test-button"
                                title={!cfg.has_token ? "Save a token first" : "Send a test message"}
                                className="px-4 py-2 border border-[#FFD700]/50 text-[#FFD700] hover:bg-[#FFD700]/10 disabled:opacity-40 text-xs tracking-widest flex items-center gap-2 transition-colors">
                                <Send className="w-3.5 h-3.5" /> {testing ? "SENDING…" : "SEND TEST"}
                            </button>
                            <button onClick={save} disabled={saving}
                                data-testid="telegram-save-button"
                                className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium px-6 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                                <FloppyDisk className="w-3.5 h-3.5" /> {saving ? "SAVING…" : "SAVE"}
                            </button>
                        </div>
                    </div>
                </div>

                {/* Alert types */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]">
                    <div className="px-5 py-3 border-b border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 03</div>
                        <div className="font-display font-bold text-lg tracking-tight">Which Events Trigger Alerts</div>
                    </div>
                    <div className="p-5 space-y-2">
                        {Object.entries(EVENT_LABELS).map(([key, meta]) => {
                            const on = cfg.alerts[key];
                            return (
                                <button key={key} onClick={() => toggleAlert(key)}
                                    data-testid={`alert-toggle-${key}`}
                                    className="w-full flex items-center justify-between gap-3 p-3 bg-[#050505] border border-[#1F1F1F] hover:border-[#333333] transition-colors text-left">
                                    <div className="flex-1">
                                        <div className="font-display font-bold text-sm flex items-center gap-2" style={{ color: on ? meta.color : "#A1A1AA" }}>
                                            {on && <CheckCircle2 className="w-3.5 h-3.5" />}
                                            {meta.label}
                                        </div>
                                        <div className="text-xs text-[#A1A1AA] mt-0.5">{meta.desc}</div>
                                    </div>
                                    <div className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border shrink-0 ${
                                        on ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#52525B]"
                                    }`}>{on ? "● ON" : "○ OFF"}</div>
                                </button>
                            );
                        })}
                    </div>
                </div>

                <div className="border border-[#FFB000]/30 bg-[#FFB000]/5 p-4 flex items-start gap-3" data-testid="security-note">
                    <AlertTriangle className="w-4 h-4 text-[#FFB000] shrink-0 mt-0.5" />
                    <div className="text-xs text-[#A1A1AA] leading-relaxed">
                        <span className="text-[#FFB000] font-mono text-[10px] tracking-widest mr-1">SECURITY ·</span>
                        Your bot token is encrypted at rest with AES-256-GCM and never written to logs.
                        Treat the token like a password: anyone with it can send messages to your chat.
                        Revoke from BotFather if you suspect it leaked.
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
