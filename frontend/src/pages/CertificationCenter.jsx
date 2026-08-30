import { useCallback, useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import {
    Award, BadgeCheck, Copy, Loader2, RefreshCw, ShieldCheck,
    Link as LinkIcon, XCircle,
} from "lucide-react";

const TONE = {
    GREEN: "text-[#00FF41] border-[#00FF41]/40",
    YELLOW: "text-[#FFD700] border-[#FFD700]/40",
    RED: "text-[#FF3B30] border-[#FF3B30]/40",
    NO_DATA: "text-[#52525B] border-[#1F1F1F]",
};
const TIER_LABEL = {
    CERTIFIED_A: "CERTIFIED · TIER A", CERTIFIED_B: "CERTIFIED · TIER B",
    PROVISIONAL: "PROVISIONAL", UNCERTIFIED: "UNCERTIFIED",
};
const PILLAR_LABEL = {
    risk_truth: "Risk Truth", position_truth: "Position Truth",
    execution_alpha: "Execution Alpha", digital_twin: "Digital Twin",
    strategy_decay: "Strategy Decay", broker_intel: "Broker Intelligence",
};

function PillarCard({ p }) {
    return (
        <div className={`border p-4 ${TONE[p.status] || TONE.NO_DATA}`}
            data-testid={`pillar-${p.pillar}`}>
            <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">
                    {PILLAR_LABEL[p.pillar] || p.pillar}
                </span>
                <span className="font-mono text-lg font-bold" data-testid={`pillar-${p.pillar}-score`}>
                    {p.score != null ? p.score : "—"}
                </span>
            </div>
            <div className="h-1 bg-[#1F1F1F] mb-2">
                {p.score != null && (
                    <div className="h-full bg-current" style={{ width: `${p.score}%` }} />
                )}
            </div>
            <div className="text-xs text-[#71717A]">{p.detail}</div>
        </div>
    );
}

export default function CertificationCenter() {
    const [accounts, setAccounts] = useState([]);
    const [accountId, setAccountId] = useState("");
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [issuing, setIssuing] = useState(false);

    useEffect(() => {
        api.get("/accounts").then(r => {
            const list = r.data?.accounts || r.data || [];
            setAccounts(list);
            if (list.length && !accountId) setAccountId(list[0].id);
        }).catch(() => setAccounts([]));
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const load = useCallback(async () => {
        if (!accountId) return;
        setLoading(true);
        try {
            setData((await api.get(`/certification/center?account_id=${accountId}`)).data);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setLoading(false); }
    }, [accountId]);

    useEffect(() => { load(); }, [load]);

    const issue = async () => {
        setIssuing(true);
        try {
            const { data: cert } = await api.post("/certification/center/issue", { account_id: accountId });
            toast.success(`Certificate ${cert.cert_id} issued — ${TIER_LABEL[cert.tier] || cert.tier}`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setIssuing(false); }
    };

    const revoke = async (certId) => {
        try {
            await api.post("/certification/center/revoke", { cert_id: certId, reason: "revoked by owner" });
            toast.success(`${certId} revoked`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const copyLink = async (certId) => {
        const url = `${window.location.origin}/certificate/${certId}`;
        try { await navigator.clipboard.writeText(url); toast.success("Public verification link copied"); }
        catch { toast.error("Clipboard blocked — copy manually: " + url); }
    };

    const tierTone = data?.tier?.startsWith("CERTIFIED") ? "text-[#00FF41] border-[#00FF41]/40 bg-[#00FF41]/5"
        : data?.tier === "PROVISIONAL" ? "text-[#FFD700] border-[#FFD700]/40 bg-[#FFD700]/5"
        : "text-[#52525B] border-[#1F1F1F] bg-transparent";

    return (
        <AppLayout>
            <div data-testid="certification-center-page">
                <PageHeader title="Certification Center"
                    subtitle="Six proof pillars, one grade — issue public, hash-chained certificates"
                    testid="certification-center-header"
                    action={
                        <div className="flex items-center gap-2">
                            <select value={accountId} onChange={e => setAccountId(e.target.value)}
                                data-testid="cert-account-select"
                                className="bg-[#0A0A0A] border border-[#1F1F1F] text-xs text-[#A1A1AA] px-2 py-1.5 font-mono max-w-[220px]">
                                {accounts.map(a => (
                                    <option key={a.id} value={a.id}>{a.label}</option>
                                ))}
                            </select>
                            <button onClick={load} disabled={loading} data-testid="cert-refresh-btn"
                                className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/40 flex items-center gap-1.5">
                                <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> RESCORE
                            </button>
                        </div>
                    }
                />

                {!accounts.length && (
                    <div className="border border-[#1F1F1F] p-6 text-sm text-[#71717A]" data-testid="cert-no-accounts">
                        Connect an MT5 account first — then certify it here.
                    </div>
                )}

                {data && (
                    <>
                        <div className={`border p-5 mb-4 flex items-center justify-between ${tierTone}`}
                            data-testid="cert-tier-banner">
                            <div className="flex items-center gap-4">
                                <Award className="w-10 h-10" />
                                <div>
                                    <div className="text-2xl font-mono font-bold tracking-widest" data-testid="cert-tier">
                                        {TIER_LABEL[data.tier] || data.tier}
                                    </div>
                                    <div className="text-xs text-[#71717A]">
                                        overall {data.overall_score ?? "—"} / 100 · {data.pillars_scored}/{data.pillars_total} pillars scored
                                    </div>
                                </div>
                            </div>
                            <button onClick={issue}
                                disabled={issuing || !data.issuable}
                                data-testid="cert-issue-btn"
                                title={data.issuable
                                    ? "Issue a public, hash-chained certificate for this account"
                                    : `Blocked — mandatory pillars unscored: ${(data.missing_mandatory || []).join(", ") || "insufficient evidence"}`}
                                className="px-4 py-2 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed">
                                {issuing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <BadgeCheck className="w-3.5 h-3.5" />}
                                ISSUE PUBLIC CERTIFICATE
                            </button>
                        </div>

                        {data.certification_note && (
                            <div className="border border-[#FFD700]/40 bg-[#FFD700]/5 p-3 mb-4 text-xs font-mono text-[#FFD700]"
                                data-testid="cert-not-live-banner">
                                {data.certification_note}
                                {(data.missing_mandatory || []).length > 0 &&
                                    ` · unscored mandatory pillars: ${data.missing_mandatory.join(", ")}`}
                            </div>
                        )}

                        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mb-6"
                            data-testid="cert-pillar-grid">
                            {(data.pillars || []).map(p => <PillarCard key={p.pillar} p={p} />)}
                        </div>

                        <div className="border border-[#1F1F1F] p-4" data-testid="cert-issued-list">
                            <div className="flex items-center gap-2 mb-3">
                                <ShieldCheck className="w-4 h-4 text-[#00FF41]" />
                                <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">Issued certificates</span>
                            </div>
                            {!(data.certificates || []).length && (
                                <div className="text-xs text-[#52525B]" data-testid="cert-list-empty">
                                    None yet — issue one to get a public verification link.
                                </div>
                            )}
                            {(data.certificates || []).map(c => (
                                <div key={c.cert_id}
                                    className="flex flex-wrap items-center justify-between gap-2 py-2 border-b border-[#141414] last:border-0 text-xs"
                                    data-testid={`cert-row-${c.cert_id}`}>
                                    <div className="font-mono">
                                        <span className="text-[#FAFAFA]">{c.cert_id}</span>
                                        <span className="text-[#52525B] ml-2">#{c.seq} · {TIER_LABEL[c.tier] || c.tier} · {c.overall_score ?? "—"}/100</span>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <span className={`font-mono ${c.valid ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                            {c.valid ? "VALID" : (c.revoked ? "REVOKED" : "EXPIRED")}
                                        </span>
                                        <button onClick={() => copyLink(c.cert_id)} title="Copy public link"
                                            data-testid={`cert-copy-${c.cert_id}`}
                                            className="p-1 border border-[#1F1F1F] hover:border-[#00FF41]/40 text-[#A1A1AA]">
                                            <Copy className="w-3.5 h-3.5" />
                                        </button>
                                        <a href={`/certificate/${c.cert_id}`} target="_blank" rel="noreferrer"
                                            data-testid={`cert-open-${c.cert_id}`}
                                            className="p-1 border border-[#1F1F1F] hover:border-[#00FF41]/40 text-[#A1A1AA]">
                                            <LinkIcon className="w-3.5 h-3.5" />
                                        </a>
                                        {c.valid && (
                                            <button onClick={() => revoke(c.cert_id)} title="Revoke"
                                                data-testid={`cert-revoke-${c.cert_id}`}
                                                className="p-1 border border-[#1F1F1F] hover:border-[#FF3B30]/40 text-[#FF3B30]">
                                                <XCircle className="w-3.5 h-3.5" />
                                            </button>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
