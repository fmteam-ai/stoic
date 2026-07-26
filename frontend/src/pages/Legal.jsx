import { useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Loader2, FileText, ShieldAlert } from "lucide-react";
import DOMPurify from "dompurify";
import api, { formatApiError } from "@/lib/api";
import { renderMarkdown } from "@/lib/markdown";

const META = {
    privacy: { title: "Privacy Policy", subtitle: "What we collect, why, and your rights." },
    risk: { title: "Risk Disclosure", subtitle: "Read this before enabling live trading." },
};

export default function Legal({ kind }) {
    const [data, setData] = useState(null);
    const [err, setErr] = useState("");
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let cancel = false;
        setLoading(true); setErr(""); setData(null);
        api.get(`/legal/${kind}`)
            .then(r => { if (!cancel) setData(r.data); })
            .catch(e => { if (!cancel) setErr(formatApiError(e)); })
            .finally(() => { if (!cancel) setLoading(false); });
        return () => { cancel = true; };
    }, [kind]);

    const meta = META[kind] || META.privacy;
    return (
        <AppLayout>
            <PageHeader title={meta.title} subtitle={meta.subtitle} testid={`legal-${kind}-header`} />
            <div className="max-w-3xl">
                {loading ? (
                    <div className="flex items-center justify-center min-h-[30vh]">
                        <Loader2 className="w-6 h-6 animate-spin text-[#52525B]" />
                    </div>
                ) : err ? (
                    <div className="border border-red-900/40 bg-red-950/20 text-red-300 p-4 flex items-start gap-2"
                        data-testid={`legal-${kind}-error`}>
                        <ShieldAlert className="w-4 h-4 mt-0.5" />
                        <div className="text-sm">{err}</div>
                    </div>
                ) : (
                    <article className="bg-[#0A0A0A] border border-[#1F1F1F] p-6 sm:p-10"
                        data-testid={`legal-${kind}-content`}>
                        <div className="flex items-center gap-2 text-[#52525B] font-mono text-[10px] tracking-widest mb-6">
                            <FileText className="w-3 h-3" />
                            VERSION {data?.version} · DRAFT — HAVE LEGAL COUNSEL REVIEW
                        </div>
                        <div dangerouslySetInnerHTML={{
                            __html: DOMPurify.sanitize(renderMarkdown(data?.markdown || ""),
                                { USE_PROFILES: { html: true } }),
                        }} />
                    </article>
                )}
            </div>
        </AppLayout>
    );
}
