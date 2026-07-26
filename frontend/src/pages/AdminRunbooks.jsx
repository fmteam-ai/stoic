import { useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import DOMPurify from "dompurify";
import api, { formatApiError } from "@/lib/api";
import { renderMarkdown } from "@/lib/markdown";
import { Loader2, ShieldCheck, CheckCircle2, AlertTriangle } from "lucide-react";
import { toast } from "sonner";

export default function AdminRunbooks() {
    const [data, setData] = useState(null);
    const [active, setActive] = useState("incident-response");
    const [chain, setChain] = useState(null);

    useEffect(() => {
        api.get("/admin/runbooks").then(r => setData(r.data))
            .catch(e => toast.error(formatApiError(e)));
    }, []);

    const verifyChain = async () => {
        setChain("loading");
        try { setChain((await api.get("/admin/audit/verify")).data); }
        catch (e) { setChain(null); toast.error(formatApiError(e)); }
    };

    const doc = data?.runbooks?.find(r => r.id === active);
    return (
        <AppLayout>
            <PageHeader title="Admin · Runbooks" subtitle="Incident response, backups and the security checklist owners."
                testid="admin-runbooks-header"
                action={
                    <button onClick={verifyChain} data-testid="audit-verify-btn"
                        className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5">
                        <ShieldCheck className="w-3.5 h-3.5" /> VERIFY AUDIT CHAIN
                    </button>
                } />

            {chain && chain !== "loading" && (
                <div data-testid="audit-verify-result"
                    className={`mb-5 border px-4 py-3 text-sm flex items-center gap-2 ${
                        chain.ok ? "border-[#00FF41]/40 bg-[#00FF41]/5 text-[#00FF41]"
                                 : "border-[#FF3B30]/40 bg-[#FF3B30]/5 text-[#FF3B30]"}`}>
                    {chain.ok ? <CheckCircle2 className="w-4 h-4" /> : <AlertTriangle className="w-4 h-4" />}
                    {chain.ok
                        ? `Audit chain intact — ${chain.chained_entries} chained entries verified (${chain.legacy_unchained_entries} legacy pre-chain entries).`
                        : `TAMPERING DETECTED — ${chain.anomalies.length} anomalies (first at seq ${chain.anomalies[0]?.seq}).`}
                </div>
            )}
            {chain === "loading" && <div className="mb-5 text-xs font-mono text-[#52525B]">VERIFYING…</div>}

            {!data ? (
                <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>
            ) : (
                <>
                    <div className="flex flex-wrap gap-2 mb-5">
                        {data.runbooks.map(r => (
                            <button key={r.id} onClick={() => setActive(r.id)}
                                data-testid={`runbook-tab-${r.id}`}
                                className={`px-3 py-1.5 text-[10px] font-mono tracking-widest uppercase border ${
                                    active === r.id ? "border-[#00FF41]/50 bg-[#00FF41]/10 text-[#00FF41]"
                                                    : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                                {r.title}
                            </button>
                        ))}
                    </div>
                    {doc && (
                        <article className="bg-[#0A0A0A] border border-[#1F1F1F] p-6 sm:p-10 max-w-4xl"
                            data-testid="runbook-content">
                            <div dangerouslySetInnerHTML={{
                                __html: DOMPurify.sanitize(renderMarkdown(doc.markdown),
                                    { USE_PROFILES: { html: true } }),
                            }} />
                        </article>
                    )}
                </>
            )}
        </AppLayout>
    );
}
