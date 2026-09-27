import { useState } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { Send, Mail, CheckCircle2, XCircle, Radio } from "lucide-react";

const fmt = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }) : null);

function ChannelRow({ id, icon: Icon, label, hint, disabled, disabledReason, last, busy, onSend }) {
    return (
        <div className="flex flex-col sm:flex-row sm:items-center gap-3 p-3 bg-[#050505] border border-[#1F1F1F]" data-testid={`alert-test-row-${id}`}>
            <div className="flex-1 min-w-0">
                <div className="font-display font-bold text-sm flex items-center gap-2 text-[#FAFAFA]">
                    <Icon className="w-3.5 h-3.5 text-[#FFD700]" /> {label}
                </div>
                <div className="text-xs text-[#A1A1AA] mt-0.5">{hint}</div>
                {last?.at && (
                    <div className={`font-mono text-[10px] tracking-widest mt-1.5 flex items-center gap-1 ${last.ok ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                        data-testid={`alert-test-last-${id}`}>
                        {last.ok ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                        LAST TEST {last.ok ? "DELIVERED" : `FAILED · ${(last.error || "").toUpperCase()}`} · {fmt(last.at)}
                    </div>
                )}
            </div>
            <button onClick={onSend} disabled={busy || disabled}
                data-testid={`alert-test-send-${id}`}
                title={disabled ? disabledReason : `Send a test ${label.toLowerCase()}`}
                className="px-4 py-2 border border-[#FFD700]/50 text-[#FFD700] hover:bg-[#FFD700]/10 disabled:opacity-40 text-xs tracking-widest flex items-center gap-2 transition-colors shrink-0 justify-center">
                <Send className="w-3.5 h-3.5" /> {busy ? "SENDING…" : "SEND TEST"}
            </button>
        </div>
    );
}

export const AlertTestPanel = ({ cfg, userEmail, onResult }) => {
    const [busy, setBusy] = useState(null);

    const run = async (channel) => {
        setBusy(channel);
        try {
            const { data } = await api.post(`/notifications/${channel}/test`);
            toast.success(channel === "email" ? `Test email sent to ${data.delivered_to}` : "Test message sent — check your Telegram", { duration: 5000 });
            onResult?.(channel, { ok: true, at: new Date().toISOString() });
        } catch (e) {
            const msg = formatApiError(e);
            toast.error(msg);
            onResult?.(channel, { ok: false, at: new Date().toISOString(), error: e?.response?.data?.detail?.code || "failed" });
        } finally { setBusy(null); }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="alert-test-panel">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Radio className="w-4 h-4 text-[#FFD700]" />
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 03 · CHANNEL CHECK</div>
                    <div className="font-display font-bold text-lg tracking-tight">Confirm your alerts arrive</div>
                </div>
            </div>
            <div className="p-5 space-y-2">
                <p className="text-xs text-[#A1A1AA] leading-relaxed mb-3">
                    Fire a harmless test on each channel now — so an outage notice or circuit-breaker alert never becomes the first message you hope arrives.
                </p>
                <ChannelRow id="telegram" icon={Send} label="Telegram" hint="Push message to your configured chat"
                    disabled={!cfg?.has_token} disabledReason="Save a bot token first"
                    last={cfg?.last_test?.telegram} busy={busy === "telegram"} onSend={() => run("telegram")} />
                <ChannelRow id="email" icon={Mail} label="Email" hint={`Delivered to ${userEmail || "your account email"}`}
                    disabled={!cfg?.email_configured} disabledReason="Email delivery is not configured on this server"
                    last={cfg?.last_test?.email} busy={busy === "email"} onSend={() => run("email")} />
            </div>
        </div>
    );
};
