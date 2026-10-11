import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { ShieldCheck, Upload, Loader2 } from "lucide-react";

/** Phase 3 — the active RULES policy (schema stoic.policy-rules/v1) and signed-file upload: no release needed.
 *  Real-money admissions are approved per account (ADMIT button on the environments table / account card). */
export function RulesPolicyPanel() {
    const [pol, setPol] = useState(undefined);
    const [text, setText] = useState("");
    const [busy, setBusy] = useState(false);
    const [pending, setPending] = useState([]);
    const load = async () => {
        try {
            const { data } = await api.get("/authority/policy");
            setPol(data.active);
            const env = await api.get("/admin/account-environments");
            setPending((env.data.accounts || []).filter((a) => a.admission?.state === "pending_operator"));
        } catch (e) { setPol(null); toast.error(formatApiError(e)); }
    };
    useEffect(() => { load(); }, []);
    const upload = async () => {
        setBusy(true);
        try {
            const policy = JSON.parse(text);
            const { data } = await api.post("/authority/policy/upload", { policy });
            toast.success(`Rules policy ${data.policy_version} active until ${String(data.expires_at).slice(0, 10)}`);
            setText(""); load();
        } catch (e) { toast.error(e instanceof SyntaxError ? "Not valid JSON" : formatApiError(e)); }
        finally { setBusy(false); }
    };
    const approve = async (id) => {
        setBusy(true);
        try { await api.post(`/authority/admission/${id}/approve`); toast.success("Real-money account admitted"); load(); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    const [deploying, setDeploying] = useState(false);
    const deployLatest = async () => {
        if (!window.confirm("Deploy the latest GitHub release to production now? (deploy-production workflow: backup → signed release → rollback on failure)")) return;
        setDeploying(true);
        try { const { data } = await api.post("/admin/deploy/latest"); toast.success(`Deploying ${data.tag} — watch the workflow`, { description: data.watch }); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setDeploying(false); }
    };
    return (
        <section className="rounded-xl border border-white/10 bg-black/40 p-4 space-y-3" data-testid="rules-policy-panel">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="font-mono text-xs text-white/60">Operator shortcuts</div>
                <Button size="sm" variant="outline" data-testid="deploy-latest-btn" disabled={deploying} onClick={deployLatest} title="Dispatches .github/workflows/deploy-production.yml with the newest release tag (step-up; needs GITHUB_DISPATCH_TOKEN on the server)">
                    {deploying ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : null} DEPLOY LATEST RELEASE
                </Button>
            </div>
            <div className="flex items-center gap-2 font-mono text-sm text-white"><ShieldCheck className="h-4 w-4 text-[#00FF41]" /> RULES POLICY <span className="text-white/40">· signed · no release needed</span></div>
            {pol === undefined ? <div className="text-xs text-white/50">loading…</div> : pol === null ? (
                <div className="text-xs text-[#FFB000]" data-testid="rules-policy-none">No rules policy active — accounts are governed by the legacy per-account expectation. Upload a signed policy (scripts/sign_rules_policy.py) or commit it under policies/.</div>
            ) : (
                <div className="text-xs font-mono text-white/80 grid grid-cols-2 md:grid-cols-4 gap-2" data-testid="rules-policy-active">
                    <div>version <span className="text-white">{pol.policy_version}</span>{pol.expired && <span className="text-[#FF3B30]"> EXPIRED</span>}</div>
                    <div>{pol.demo_only ? "DEMO ONLY" : "real money allowed (operator approval)"}</div>
                    <div>cap <span className="text-white">{pol.max_accounts_per_user}</span> account(s)/user</div>
                    <div>brokers <span className="text-white">{(pol.allowed_brokers || []).join(", ")}</span></div>
                    <div>risk caps <span className="text-white">{Object.entries(pol.risk_caps || {}).map(([k, v]) => `${k}=${v}`).join(" ") || "—"}</span></div>
                    <div>expires <span className="text-white">{String(pol.expires_at).slice(0, 10)}</span></div>
                    <div className="col-span-2">activated {String(pol.activated_at).slice(0, 16)} by {pol.activated_by} ({pol.source})</div>
                </div>
            )}
            {pending.length > 0 && (
                <div className="rounded-lg border border-[#FFB000]/30 p-3 space-y-1" data-testid="admission-pending">
                    <div className="font-mono text-xs text-[#FFB000] uppercase">real-money accounts waiting for operator approval ({pending.length})</div>
                    {pending.map((a) => (
                        <div key={a.account_id} className="flex items-center justify-between text-xs font-mono">
                            <span>{a.label} · {a.broker} #{a.account_number} · {a.effective}</span>
                            <Button size="sm" data-testid={`admit-${a.account_id}`} disabled={busy} onClick={() => approve(a.account_id)}>APPROVE (password + authenticator)</Button>
                        </div>
                    ))}
                </div>
            )}
            <div className="space-y-2">
                <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} placeholder='{"schema":"stoic.policy-rules/v1", … "signature_hex":"…"}'
                    data-testid="rules-policy-upload-text" className="w-full bg-black border border-white/10 p-2 text-xs font-mono text-white" />
                <Button size="sm" data-testid="rules-policy-upload" disabled={busy || !text.trim()} onClick={upload}>
                    {busy ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Upload className="h-3 w-3 mr-1" />} ACTIVATE SIGNED POLICY (step-up)
                </Button>
            </div>
        </section>
    );
}
