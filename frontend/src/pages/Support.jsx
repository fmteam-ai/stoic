import { useEffect, useState, useCallback } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import {
    LifeBuoy, Plus, Loader2, Send, ArrowLeft, CheckCircle2, Clock, XCircle,
} from "lucide-react";

const CATEGORIES = [
    { id: "billing", label: "Billing" },
    { id: "technical", label: "Technical" },
    { id: "account", label: "Account" },
    { id: "other", label: "Other" },
];

export const STATUS_PILL = {
    open: { cls: "text-[#FFB000] border-[#FFB000]/40 bg-[#FFB000]/10", label: "OPEN", icon: Clock },
    answered: { cls: "text-[#00FF41] border-[#00FF41]/40 bg-[#00FF41]/10", label: "ANSWERED", icon: CheckCircle2 },
    closed: { cls: "text-[#52525B] border-[#1F1F1F] bg-[#0F0F0F]", label: "CLOSED", icon: XCircle },
};

export function TicketThread({ ticket, onReply, onClose, busy, isAdmin }) {
    const [msg, setMsg] = useState("");
    const pill = STATUS_PILL[ticket.status] || STATUS_PILL.open;
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="ticket-thread">
            <div className="p-4 border-b border-[#1F1F1F] flex flex-wrap items-center gap-3">
                <div className="flex-1 min-w-[200px]">
                    <div className="font-display text-white">{ticket.subject}</div>
                    <div className="text-[10px] font-mono text-[#52525B] tracking-widest uppercase mt-0.5">
                        {ticket.category} · {ticket.user_email} · {new Date(ticket.created_at).toLocaleString()}
                    </div>
                </div>
                <span className={`px-2 py-0.5 border text-[10px] font-mono tracking-widest ${pill.cls}`}>{pill.label}</span>
                {ticket.status !== "closed" && (
                    <button onClick={onClose} disabled={busy} data-testid="ticket-close-btn"
                        className="px-3 py-1.5 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#FF3B30]/50 hover:text-[#FF3B30]">
                        CLOSE TICKET
                    </button>
                )}
            </div>
            <div className="p-4 space-y-3 max-h-[50vh] overflow-y-auto">
                {(ticket.messages || []).map((m, i) => (
                    <div key={i} data-testid={`ticket-msg-${i}`}
                        className={`p-3 border text-sm ${m.by === "admin"
                            ? "border-[#00FF41]/30 bg-[#00FF41]/5"
                            : "border-[#1F1F1F] bg-[#0F0F0F]"}`}>
                        <div className="text-[10px] font-mono tracking-widest text-[#52525B] mb-1 uppercase">
                            {m.by === "admin" ? "STOIC SUPPORT" : m.author_email} · {new Date(m.at).toLocaleString()}
                        </div>
                        <div className="text-[#D4D4D8] whitespace-pre-wrap">{m.body}</div>
                    </div>
                ))}
            </div>
            {ticket.status !== "closed" && (
                <div className="p-4 border-t border-[#1F1F1F] flex gap-2">
                    <textarea value={msg} onChange={e => setMsg(e.target.value)} rows={2}
                        data-testid="ticket-reply-input"
                        placeholder={isAdmin ? "Reply as support…" : "Write a reply…"}
                        className="flex-1 bg-[#0F0F0F] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-2 text-sm resize-none" />
                    <button disabled={busy || msg.trim().length < 2}
                        onClick={() => { onReply(msg.trim()); setMsg(""); }}
                        data-testid="ticket-reply-btn"
                        className="px-4 bg-[#00FF41] text-black text-xs font-mono tracking-widest disabled:opacity-40 flex items-center gap-2">
                        <Send className="w-3.5 h-3.5" /> SEND
                    </button>
                </div>
            )}
        </div>
    );
}

function NewTicketForm({ onCreated }) {
    const [category, setCategory] = useState("technical");
    const [subject, setSubject] = useState("");
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);

    const submit = async () => {
        setBusy(true);
        try {
            const { data } = await api.post("/support/tickets", { category, subject, message });
            toast.success("Ticket opened — we'll reply by email and here.");
            onCreated(data);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-5 space-y-4" data-testid="new-ticket-form">
            <div className="flex flex-wrap gap-2">
                {CATEGORIES.map(c => (
                    <button key={c.id} onClick={() => setCategory(c.id)}
                        data-testid={`ticket-cat-${c.id}`}
                        className={`px-3 py-1.5 text-[10px] font-mono tracking-widest uppercase border ${
                            category === c.id ? "border-[#00FF41]/50 bg-[#00FF41]/10 text-[#00FF41]"
                                              : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                        {c.label}
                    </button>
                ))}
            </div>
            <input value={subject} onChange={e => setSubject(e.target.value)} maxLength={200}
                data-testid="ticket-subject-input" placeholder="Subject (e.g. Refund request for annual plan)"
                className="w-full bg-[#0F0F0F] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-2.5 text-sm" />
            <textarea value={message} onChange={e => setMessage(e.target.value)} rows={5} maxLength={5000}
                data-testid="ticket-message-input"
                placeholder="Describe the issue — include account/trade IDs, dates, and what you expected to happen."
                className="w-full bg-[#0F0F0F] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-2.5 text-sm resize-none" />
            <button onClick={submit} disabled={busy || subject.trim().length < 3 || message.trim().length < 10}
                data-testid="ticket-submit-btn"
                className="px-5 py-2.5 bg-[#00FF41] text-black text-xs font-mono tracking-widest disabled:opacity-40">
                {busy ? "OPENING…" : "OPEN TICKET"}
            </button>
        </div>
    );
}

export default function Support() {
    const [tickets, setTickets] = useState(null);
    const [active, setActive] = useState(null); // full ticket with thread
    const [creating, setCreating] = useState(false);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        try { setTickets((await api.get("/support/tickets")).data); }
        catch (e) { toast.error(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const openTicket = async (id) => {
        try { setActive((await api.get(`/support/tickets/${id}`)).data); }
        catch (e) { toast.error(formatApiError(e)); }
    };
    const reply = async (msg) => {
        setBusy(true);
        try { setActive((await api.post(`/support/tickets/${active.id}/reply`, { message: msg })).data); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    const closeTicket = async () => {
        setBusy(true);
        try {
            await api.post(`/support/tickets/${active.id}/close`);
            setActive({ ...active, status: "closed" });
            load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    return (
        <AppLayout>
            <PageHeader title="Support" subtitle="Billing, technical and account help — we reply in-thread and by email."
                action={!creating && !active ? (
                    <button onClick={() => setCreating(true)} data-testid="support-new-ticket-btn"
                        className="px-3 py-1.5 text-xs font-mono tracking-widest bg-[#00FF41] text-black flex items-center gap-1.5">
                        <Plus className="w-3.5 h-3.5" /> NEW TICKET
                    </button>
                ) : null} />

            {(creating || active) && (
                <button onClick={() => { setCreating(false); setActive(null); load(); }}
                    data-testid="support-back"
                    className="flex items-center gap-2 text-xs font-mono tracking-widest text-[#00FF41] mb-4 hover:underline">
                    <ArrowLeft className="w-3.5 h-3.5" /> BACK TO TICKETS
                </button>
            )}

            {creating ? (
                <NewTicketForm onCreated={(t) => { setCreating(false); setActive(t); load(); }} />
            ) : active ? (
                <TicketThread ticket={active} onReply={reply} onClose={closeTicket} busy={busy} />
            ) : tickets === null ? (
                <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>
            ) : tickets.length === 0 ? (
                <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-10 text-center" data-testid="support-empty">
                    <LifeBuoy className="w-8 h-8 text-[#52525B] mx-auto mb-3" />
                    <div className="text-sm text-[#A1A1AA] mb-4">No tickets yet. Billing question, EA problem, account request — we're here.</div>
                    <button onClick={() => setCreating(true)} data-testid="support-empty-new-btn"
                        className="px-4 py-2 bg-[#00FF41] text-black text-xs font-mono tracking-widest">OPEN YOUR FIRST TICKET</button>
                </div>
            ) : (
                <div className="space-y-2" data-testid="support-ticket-list">
                    {tickets.map(t => {
                        const pill = STATUS_PILL[t.status] || STATUS_PILL.open;
                        return (
                            <button key={t.id} onClick={() => openTicket(t.id)}
                                data-testid={`support-ticket-${t.id}`}
                                className="w-full text-left bg-[#0A0A0A] border border-[#1F1F1F] hover:border-[#00FF41]/40 px-4 py-3 flex items-center gap-4 transition">
                                <div className="flex-1 min-w-0">
                                    <div className="text-sm text-white truncate">{t.subject}</div>
                                    <div className="text-[10px] font-mono text-[#52525B] tracking-widest uppercase mt-0.5">
                                        {t.category} · {t.message_count} message{t.message_count !== 1 ? "s" : ""} · {new Date(t.updated_at).toLocaleString()}
                                    </div>
                                </div>
                                <span className={`px-2 py-0.5 border text-[10px] font-mono tracking-widest ${pill.cls}`}>{pill.label}</span>
                            </button>
                        );
                    })}
                </div>
            )}
        </AppLayout>
    );
}
