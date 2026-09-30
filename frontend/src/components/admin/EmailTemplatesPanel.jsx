import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";
import { Loader2, Mail, Send } from "lucide-react";
import { Input } from "@/components/ui/input";

export function EmailTemplatesPanel() {
    const { user } = useAuth();
    const [data, setData] = useState(null);
    const [selected, setSelected] = useState(null);
    const [preview, setPreview] = useState(null);
    const [recipient, setRecipient] = useState(user?.email || "");
    const [sending, setSending] = useState(false);

    useEffect(() => {
        api.get("/admin/integrations/email-templates").then(({ data }) => { setData(data); if (data.templates[0]) setSelected(data.templates[0].id); })
            .catch((e) => toast.error(formatApiError(e)));
    }, []);
    useEffect(() => { if (user?.email && !recipient) setRecipient(user.email); }, [user, recipient]);
    useEffect(() => {
        if (!selected) return;
        setPreview(null);
        api.get(`/admin/integrations/email-templates/${selected}`).then(({ data }) => setPreview(data))
            .catch((e) => toast.error(formatApiError(e)));
    }, [selected]);

    const send = async () => {
        setSending(true);
        try {
            const { data } = await api.post(`/admin/integrations/email-templates/${selected}/send`, { recipient });
            toast.success(`Sent "${data.subject}" to ${data.recipient}`);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setSending(false); }
    };

    if (!data) return <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /></div>;
    const groups = data.templates.reduce((acc, t) => { (acc[t.category] ||= []).push(t); return acc; }, {});

    return (
        <div data-testid="email-templates-panel" className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
            <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <Mail className="w-4 h-4 text-[#00FF41]" />
                    <h3 className="text-sm font-semibold text-[#E4E4E7]">Transactional e-mails — preview & test send</h3>
                </div>
                <span className="text-[11px] font-mono text-[#52525B]" data-testid="email-templates-sender">
                    from {data.sender}{!data.configured && <span className="text-[#FF3B30]"> · RESEND_API_KEY missing</span>}</span>
            </div>
            <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
                <div className="space-y-3">
                    {Object.entries(groups).map(([cat, list]) => (
                        <div key={cat}>
                            <div className="text-[10px] font-mono tracking-widest text-[#52525B] mb-1">{cat.toUpperCase()}</div>
                            {list.map(t => (
                                <button key={t.id} data-testid={`email-template-${t.id}`} onClick={() => setSelected(t.id)}
                                    className={`w-full text-left px-2 py-1.5 text-xs border-l-2 transition-colors ${selected === t.id
                                        ? "border-[#00FF41] text-[#E4E4E7] bg-[#0F0F0F]" : "border-transparent text-[#A1A1AA] hover:text-[#E4E4E7]"}`}>
                                    {t.label}
                                </button>
                            ))}
                        </div>
                    ))}
                </div>
                <div className="min-w-0">
                    {!preview ? <div className="flex justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /></div> : (
                        <>
                            <div className="text-[11px] text-[#71717A] mb-1">{data.templates.find(t => t.id === selected)?.description}</div>
                            <div className="text-xs font-mono text-[#E4E4E7] mb-2" data-testid="email-template-subject">Subject: {preview.subject}</div>
                            <iframe title="email preview" data-testid="email-template-preview" sandbox="" srcDoc={preview.html}
                                className="w-full h-[420px] bg-[#050505] border border-[#1F1F1F]" />
                            <div className="flex flex-wrap items-center gap-2 mt-3">
                                <Input data-testid="email-template-recipient" value={recipient} onChange={(e) => setRecipient(e.target.value)}
                                    placeholder="recipient@example.com" className="h-8 max-w-xs bg-[#0F0F0F] border-[#1F1F1F] font-mono text-xs" />
                                <button data-testid="email-template-send" onClick={send} disabled={sending || !recipient.includes("@") || !data.configured}
                                    className="px-2.5 py-1.5 text-[10px] font-mono tracking-widest bg-[#00FF41] text-black disabled:opacity-40 hover:bg-[#00FF41]/90 flex items-center gap-1.5">
                                    {sending ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />} SEND TEST
                                </button>
                                <span className="text-[10px] font-mono text-[#52525B]">sample data · subject prefixed [PREVIEW] · max 10 / 10 min</span>
                            </div>
                        </>
                    )}
                </div>
            </div>
        </div>
    );
}
