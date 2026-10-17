import { useEffect, useState } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { BadgeCheck, XCircle, CheckCircle2, Download, RefreshCw, Wrench } from "lucide-react";
import { toast } from "sonner";

function FixAction({ check, accountId, onCertify, onRequestSync, busy }) {
    const action = check.fix?.action;
    const cls = "inline-flex items-center gap-1 font-mono text-[10px] tracking-widest px-1.5 py-0.5 border transition-colors";
    if (action === "download_ea") {
        return (
            <a href={`${API}/ea-script`} target="_blank" rel="noopener noreferrer" download="EmergentTradingBridge.mq5"
                data-testid={`cert-fix-download-ea-${check.key}-${accountId}`}
                className={`${cls} border-[#00BFFF]/40 text-[#00BFFF] hover:bg-[#00BFFF]/10`}>
                <Download className="w-3 h-3" /> DOWNLOAD EA
            </a>
        );
    }
    if (action === "request_sync") {
        return (
            <button onClick={onRequestSync} disabled={busy}
                data-testid={`cert-fix-request-sync-${accountId}`}
                className={`${cls} border-[#00BFFF]/40 text-[#00BFFF] hover:bg-[#00BFFF]/10 disabled:opacity-50`}>
                <RefreshCw className={`w-3 h-3 ${busy ? "animate-spin" : ""}`} /> REQUEST SYNC
            </button>
        );
    }
    if (action === "certify") {
        return (
            <button onClick={onCertify} data-testid={`cert-fix-certify-${accountId}`}
                className={`${cls} border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10`}>
                <BadgeCheck className="w-3 h-3" /> CERTIFY NOW
            </button>
        );
    }
    return null;
}

export function AccountCertification({ accountId, item = null, onChanged = null }) {
    const [items, setItems] = useState(null);
    const [busy, setBusy] = useState(false);

    const reload = async () => {
        if (onChanged) { await onChanged(); return; }
        const { data } = await api.get("/accounts/certification");
        setItems(data.items || []);
    };

    useEffect(() => {
        if (item) return undefined;                 // page-owned data (Accounts.jsx polls every 30 s and passes it down)
        let dead = false;
        api.get("/accounts/certification")
            .then(({ data }) => { if (!dead) setItems(data.items || []); })
            .catch(() => { if (!dead) setItems([]); });
        return () => { dead = true; };
    }, [accountId, item]);

    const cert = item || items?.find(i => i.account_id === accountId);
    if (!cert) return null;

    const certify = async () => {
        try {
            await api.post(`/accounts/${accountId}/certify`);
            toast.success("Demo certification stamped");
            await reload();
        } catch (e) {
            toast.error("Certification blocked", { description: formatApiError(e) });
        }
    };

    const requestSync = async () => {
        setBusy(true);
        try {
            await api.post(`/accounts/${accountId}/request-sync`);
            toast.success("Broker sync requested", { description: "The EA picks it up on its next poll." });
            await reload();
        } catch (e) {
            toast.error("Sync request failed", { description: formatApiError(e) });
        } finally {
            setBusy(false);
        }
    };

    const failing = cert.checks.filter(c => !c.ok);

    return (
        <div className="mt-3 border border-[#1F1F1F] bg-black/40 p-3"
            data-testid={`certification-${accountId}`}>
            <div className="flex items-center gap-2 mb-2">
                <BadgeCheck className={`w-3.5 h-3.5 ${cert.certified ? "text-[#00FF41]" : "text-[#FFB000]"}`} />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                    GO-LIVE CERTIFICATION
                </span>
                <span className={`font-mono text-[10px] tracking-widest px-1.5 border ${
                    cert.certified
                        ? "border-[#00FF41]/40 text-[#00FF41]"
                        : "border-[#FFB000]/40 text-[#FFB000]"}`}
                    data-testid={`certification-verdict-${accountId}`}>
                    {cert.certified ? "CERTIFIED" : `${cert.passed}/${cert.total} CHECKS`}
                </span>
                {cert.can_certify && !cert.certified && (
                    <button onClick={certify}
                        data-testid={`certify-btn-${accountId}`}
                        className="ml-auto font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10">
                        CERTIFY NOW
                    </button>
                )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-4 gap-y-1">
                {cert.checks.map(c => (
                    <div key={c.key} className="flex items-center gap-1.5 font-mono text-[10px]"
                        data-testid={`cert-${c.key}-${accountId}`}
                        title={c.hint || undefined}>
                        {c.ok
                            ? <CheckCircle2 className="w-3 h-3 text-[#00FF41] shrink-0" />
                            : <XCircle className="w-3 h-3 text-[#FF3B30] shrink-0" />}
                        <span className="text-[#52525B]">{c.label}</span>
                        <span className={c.ok ? "text-[#A1A1AA]" : "text-[#FF3B30]"}>{c.value}</span>
                    </div>
                ))}
            </div>
            {failing.length > 0 && (
                <div className="mt-3 border-t border-[#1F1F1F] pt-2 space-y-1.5" data-testid={`cert-fixes-${accountId}`}>
                    <div className="flex items-center gap-1.5 font-mono text-[10px] tracking-widest text-[#FFB000]">
                        <Wrench className="w-3 h-3" /> HOW TO CLEAR {failing.length} FAILING CHECK{failing.length > 1 ? "S" : ""}
                    </div>
                    {failing.map(c => (
                        <div key={c.key} className="flex flex-col sm:flex-row sm:items-start gap-1 sm:gap-3 font-mono text-[10px]"
                            data-testid={`cert-fix-${c.key}-${accountId}`}>
                            <span className="text-[#FF3B30] shrink-0 sm:w-44">{c.label}</span>
                            <span className="text-[#A1A1AA] flex-1" data-testid={`cert-fix-text-${c.key}-${accountId}`}>
                                {c.fix?.text || c.hint}
                            </span>
                            <FixAction check={c} accountId={accountId} busy={busy}
                                onCertify={certify} onRequestSync={requestSync} />
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
