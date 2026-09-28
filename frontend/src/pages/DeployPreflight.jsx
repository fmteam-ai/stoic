import { useCallback, useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { DeployWatchCard } from "@/components/DeployWatchCard";
import { SignerHealthRow } from "@/components/SignerHealthRow";
import api, { formatApiError } from "@/lib/api";
import { CheckCircle2, Loader2, RefreshCw, ShieldAlert, XCircle } from "lucide-react";

const PILL = {
    pass: ["PASS", "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10"],
    warn: ["WARN", "border-[#FFB000]/40 text-[#FFB000] bg-[#FFB000]/10"],
    fail: ["FAIL", "border-[#FF3B30]/40 text-[#FF3B30] bg-[#FF3B30]/10"],
};

const VERDICT = {
    ready: {
        icon: CheckCircle2, cls: "border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]",
        title: "READY TO DEPLOY", sub: "Every production guardrail passes — the deploy will boot.",
    },
    ready_with_warnings: {
        icon: ShieldAlert, cls: "border-[#FFB000]/40 bg-[#FFB000]/10 text-[#FFB000]",
        title: "READY — WITH WARNINGS", sub: "The deploy will boot, but review the warnings below.",
    },
    will_crash: {
        icon: XCircle, cls: "border-[#FF3B30]/40 bg-[#FF3B30]/10 text-[#FF3B30]",
        title: "DEPLOY WILL BOUNCE", sub: "Production refuses to boot until every FAIL below is fixed in the Secrets tab.",
    },
};

export default function DeployPreflight() {
    const [data, setData] = useState(null);
    const [health, setHealth] = useState(null);
    const [err, setErr] = useState("");
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        setBusy(true);
        setErr("");
        try { setData((await api.get("/ops/deploy-preflight", { params: { signer_health: true } })).data); }
        catch (e) { setErr(formatApiError(e)); }
        finally { setBusy(false); }
        try { setHealth((await api.get("/health")).data); } catch { /* provenance optional */ }
    }, []);
    useEffect(() => { load(); }, [load]);

    const v = VERDICT[data?.verdict] || VERDICT.will_crash;
    const VIcon = v.icon;
    const signerCheck = data?.checks?.find((c) => c.id === "release_signer_health");
    const rows = (data?.checks || []).filter((c) => c.id !== "release_signer_health");

    return (
        <AppLayout>
            <div className="space-y-6" data-testid="deploy-preflight-page">
                <PageHeader
                    title="Deploy Preflight"
                    subtitle="Checks every APP_ENV=production startup guardrail before you publish — so deploys never bounce."
                    testid="preflight-header"
                    action={
                        <button onClick={load} disabled={busy} data-testid="preflight-refresh-button"
                            className="px-4 py-2 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] hover:text-white transition disabled:opacity-50 flex items-center gap-2">
                            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} RE-RUN
                        </button>
                    }
                />
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="preflight-error">{err}</div>}
                {!data && !err && <div className="text-[#52525B] font-mono text-xs">Running checks…</div>}
                {data && (
                    <>
                        <div className={`border p-4 flex items-start gap-3 ${v.cls}`} data-testid="preflight-verdict">
                            <VIcon className="w-6 h-6 shrink-0" />
                            <div>
                                <div className="font-display font-bold tracking-widest" data-testid="preflight-verdict-title">{v.title}</div>
                                <div className="text-xs mt-0.5 opacity-90">{v.sub}</div>
                                <div className="font-mono text-[10px] mt-1 opacity-70">
                                    ENVIRONMENT: {data.environment?.toUpperCase()} · {data.fail_count} FAIL · {data.warn_count} WARN
                                </div>
                            </div>
                        </div>
                        <p className="text-xs text-[#71717A] leading-relaxed border border-[#1F1F1F] bg-[#0A0A0A] p-3 font-mono">
                            {data.note}
                        </p>
                        <SignerHealthRow check={signerCheck} />
                        <DeployWatchCard localSha={health?.build_sha || ""} />
                        <div className="border border-[#1F1F1F] bg-[#0A0A0A] divide-y divide-[#1F1F1F]" data-testid="preflight-checks">
                            {rows.map((c) => {
                                const [label, cls] = PILL[c.status] || PILL.warn;
                                return (
                                    <div key={c.id} className="p-4 flex flex-wrap gap-3 items-start" data-testid={`preflight-check-${c.id}`}>
                                        <span className={`font-mono text-[10px] tracking-widest px-2 py-1 border shrink-0 ${cls}`}
                                            data-testid={`preflight-status-${c.id}`}>{label}</span>
                                        <div className="flex-1 min-w-[260px]">
                                            <div className="font-mono text-sm text-white">{c.label}</div>
                                            <div className="font-mono text-[11px] text-[#71717A] mt-1">
                                                current: <span className={c.status === "fail" ? "text-[#FF3B30]" : "text-white"}>{c.current}</span>
                                                {" · "}required: <span className="text-[#00FF41]">{c.required}</span>
                                            </div>
                                            {c.status !== "pass" && (
                                                <div className="text-xs text-[#A1A1AA] mt-1.5 leading-relaxed">{c.fix}</div>
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                        <p className="text-[10px] text-[#52525B] font-mono">
                            After deploying, open this page ON PRODUCTION to confirm the live environment passes with its real Secrets.
                        </p>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
