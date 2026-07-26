import { useEffect, useState, useCallback } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, ArrowLeft, Inbox } from "lucide-react";
import { TicketThread, STATUS_PILL } from "@/pages/Support";

const FILTERS = ["all", "open", "answered", "closed"];

export default function AdminSupport() {
    const [data, setData] = useState(null);
    const [filter, setFilter] = useState("open");
    const [active, setActive] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async (f = filter) => {
        try {
            const qs = f === "all" ? "" : `?status=${f}`;
            setData((await api.get(`/support/admin/tickets${qs}`)).data);
        } catch (e) { toast.error(formatApiError(e)); }
    }, [filter]);
    useEffect(() => { load(filter); }, [filter, load]);

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
            <PageHeader title="Admin · Support Queue"
                subtitle="Reply in-thread — the user is notified by email." testid="admin-support-header" />

            {active ? (
                <>
                    <button onClick={() => { setActive(null); load(); }} data-testid="admin-support-back"
                        className="flex items-center gap-2 text-xs font-mono tracking-widest text-[#00FF41] mb-4 hover:underline">
                        <ArrowLeft className="w-3.5 h-3.5" /> BACK TO QUEUE
                    </button>
                    <TicketThread ticket={active} onReply={reply} onClose={closeTicket} busy={busy} isAdmin />
                </>
            ) : (
                <>
                    <div className="flex flex-wrap gap-2 mb-5" data-testid="admin-support-filters">
                        {FILTERS.map(f => (
                            <button key={f} onClick={() => setFilter(f)}
                                data-testid={`admin-support-filter-${f}`}
                                className={`px-3 py-1.5 text-[10px] font-mono tracking-widest uppercase border ${
                                    filter === f ? "border-[#00FF41]/50 bg-[#00FF41]/10 text-[#00FF41]"
                                                 : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                                {f}{f !== "all" && data?.counts ? ` · ${data.counts[f] ?? 0}` : ""}
                            </button>
                        ))}
                    </div>
                    {data === null ? (
                        <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>
                    ) : data.tickets.length === 0 ? (
                        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-10 text-center" data-testid="admin-support-empty">
                            <Inbox className="w-8 h-8 text-[#52525B] mx-auto mb-3" />
                            <div className="text-sm text-[#A1A1AA]">Queue is clear.</div>
                        </div>
                    ) : (
                        <div className="space-y-2" data-testid="admin-support-list">
                            {data.tickets.map(t => {
                                const pill = STATUS_PILL[t.status] || STATUS_PILL.open;
                                return (
                                    <button key={t.id} onClick={() => openTicket(t.id)}
                                        data-testid={`admin-ticket-${t.id}`}
                                        className="w-full text-left bg-[#0A0A0A] border border-[#1F1F1F] hover:border-[#00FF41]/40 px-4 py-3 flex items-center gap-4 transition">
                                        <div className="flex-1 min-w-0">
                                            <div className="text-sm text-white truncate">{t.subject}</div>
                                            <div className="text-[10px] font-mono text-[#52525B] tracking-widest uppercase mt-0.5">
                                                {t.user_email} · {t.category} · {t.message_count} msg · {new Date(t.updated_at).toLocaleString()}
                                            </div>
                                        </div>
                                        <span className={`px-2 py-0.5 border text-[10px] font-mono tracking-widest ${pill.cls}`}>{pill.label}</span>
                                    </button>
                                );
                            })}
                        </div>
                    )}
                </>
            )}
        </AppLayout>
    );
}
