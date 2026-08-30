import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { EquityCurve, StatTiles, IntegrityStamp, AccountsTable } from "@/components/VerifiedPerf";
import { AttestationSeal } from "@/components/AttestationSeal";
import { toast } from "sonner";
import { Share2, Copy, XCircle } from "lucide-react";

export default function VerifiedPerformance() {
    const [d, setD] = useState(null);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/performance/verified");
            setD(data);
        } catch (e) { setErr(formatApiError(e)); }
    }, []);

    useEffect(() => { load(); }, [load]);

    const shareUrl = d?.share
        ? `${window.location.origin}/p/${d.share.share_id}` : null;

    const createShare = async () => {
        try {
            await api.post("/performance/share");
            await load();
            toast.success("Public share link created");
        } catch (e) { toast.error(formatApiError(e)); }
    };
    const revokeShare = async () => {
        try {
            await api.delete("/performance/share");
            await load();
            toast.success("Share link revoked");
        } catch (e) { toast.error(formatApiError(e)); }
    };
    const copy = () => {
        try {
            navigator.clipboard?.writeText(shareUrl)
                ?.then(() => toast.success("Link copied"))
                ?.catch(() => toast.error("Copy blocked — copy manually"));
        } catch {
            toast.error("Copy blocked — copy manually");
        }
    };

    return (
        <AppLayout>
            <PageHeader title="Verified Performance"
                subtitle="Broker-truth track record — computed only from reconciled broker deals, never estimates."
                testid="verified-perf-header" />
            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}
                {d && (
                    <>
                        <IntegrityStamp integrity={d.integrity} />
                        <AttestationSeal attestation={d.attestation} blocked={d.attestation_blocked} />
                        <StatTiles overall={d.overall} maxDrawdown={d.max_drawdown} />
                        <EquityCurve curve={d.equity_curve} />
                        <AccountsTable accounts={d.accounts} />

                        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="share-controls">
                            <div className="flex items-center gap-2 mb-2">
                                <Share2 className="w-3.5 h-3.5 text-[#0099FF]" />
                                <span className="font-display font-bold text-sm">Public Share Link</span>
                            </div>
                            <p className="text-xs text-[#A1A1AA] mb-3">
                                Read-only, account labels masked. Revoke any time.
                            </p>
                            {shareUrl ? (
                                <div className="flex items-center gap-2 flex-wrap">
                                    <code className="font-mono text-xs px-3 py-2 bg-[#050505] border border-[#1F1F1F] break-all"
                                        data-testid="share-url">{shareUrl}</code>
                                    <button onClick={copy} data-testid="share-copy"
                                        className="flex items-center gap-1 px-3 py-2 border border-[#1F1F1F] hover:border-[#333] text-xs font-mono tracking-widest">
                                        <Copy className="w-3 h-3" /> COPY
                                    </button>
                                    <button onClick={revokeShare} data-testid="share-revoke"
                                        className="flex items-center gap-1 px-3 py-2 border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 text-xs font-mono tracking-widest">
                                        <XCircle className="w-3 h-3" /> REVOKE
                                    </button>
                                </div>
                            ) : (
                                <button onClick={createShare} data-testid="share-create"
                                    className="px-4 py-2 bg-[#00FF41] text-black font-display font-bold text-xs tracking-widest hover:bg-[#00E53A]">
                                    CREATE SHARE LINK
                                </button>
                            )}
                        </div>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
