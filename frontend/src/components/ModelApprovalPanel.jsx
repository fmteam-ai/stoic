import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { useAuth } from "@/context/AuthContext";

const short = (d) => (d ? `${String(d).slice(0, 12)}…` : "—");

export function ModelApprovalPanel() {
    const { user } = useAuth();
    const [d, setD] = useState(null);
    const [busy, setBusy] = useState(null);
    const [note, setNote] = useState("");
    const [problems, setProblems] = useState([]);
    const [signHint, setSignHint] = useState(null);

    const load = useCallback(() => {
        api.get("/ml/ensemble").then(r => setD(r.data)).catch(() => setD(null));
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);

    if (user?.role !== "admin" || !d) return null;
    const cand = d.candidate, prod = d.production, manifest = d.manifest || {};
    const approvals = cand?.approvals || [];
    const manifestNamesCandidate = cand && manifest.models && Object.values(manifest.models).includes(cand.digest);

    const run = async (kind) => {
        setBusy(kind); setProblems([]);
        try {
            const r = await api.post(`/ml/candidate/${kind}`, { note });
            if (kind === "approve") { setSignHint(r.data.sign_hint); toast.success(`Approval recorded (${r.data.approvals.length}/2)`); }
            else { setSignHint(null); toast.success(`Promoted — production ${short(r.data.production?.digest)}`); }
            load();
        } catch (e) {
            const det = e.response?.data?.detail;
            setProblems(det?.problems || [det?.code || formatApiError(e)]);
            toast.error(det?.code ? det.code.replaceAll("_", " ") : formatApiError(e));
        } finally { setBusy(null); }
    };

    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="model-approval-panel">
            <div className="flex items-baseline justify-between gap-3 flex-wrap">
                <h3 className="font-mono text-xs tracking-[0.25em] text-[#A1A1AA]">MODEL GOVERNANCE · TWO-ADMIN PROMOTION</h3>
                <span className="font-mono text-[10px] text-[#52525B]">manifest {manifest.signed ? "SIGNED" : "UNSIGNED"} · build {short(manifest.running_build)}</span>
            </div>
            <div className="grid sm:grid-cols-2 gap-4 mt-3 font-mono text-[11px]">
                <div data-testid="model-approval-production">
                    <div className="text-[#52525B] text-[9px] tracking-widest">PRODUCTION</div>
                    {prod ? (
                        <div className="text-[#00FF41]">ACTIVE {short(prod.digest)}<div className="text-[#71717A] text-[10px]">
                            {prod.n_trades} trades · activated {String(prod.activated_at || "").slice(0, 16)} · approvals {(prod.approvals || []).map(a => a.email).join(", ")}</div></div>
                    ) : <div className="text-[#71717A]">none</div>}
                </div>
                <div data-testid="model-approval-candidate">
                    <div className="text-[#52525B] text-[9px] tracking-widest">CANDIDATE</div>
                    {cand ? (
                        <div className="text-[#FFD700]">{String(cand.status).toUpperCase().replaceAll("_", " ")} {short(cand.digest)}
                            <div className="text-[#71717A] text-[10px]">{cand.n_trades} trades · holdout AUC {Object.entries(cand.aucs || {}).map(([k, v]) => `${k.split("_")[0]} ${v}`).join(" / ")}</div>
                            <div className="text-[#A1A1AA] text-[10px] mt-1" data-testid="model-approval-count">approvals {approvals.length}/2 · {approvals.map(a => a.email).join(", ") || "none yet"}</div>
                            <div className="text-[10px] mt-1" data-testid="model-approval-manifest-state">
                                signed manifest names this digest: <span className={manifestNamesCandidate ? "text-[#00FF41]" : "text-[#FF3B30]"}>{manifestNamesCandidate ? "YES" : "NO — sign after 2 approvals"}</span>
                            </div>
                        </div>
                    ) : <div className="text-[#71717A]">none pending — training produces the next candidate</div>}
                </div>
            </div>
            {cand && (
                <div className="mt-3 flex flex-wrap items-center gap-2">
                    <input value={note} onChange={e => setNote(e.target.value)} placeholder="review note (recorded in the audit chain)"
                        className="bg-black border border-[#262626] px-2 py-1 font-mono text-[11px] text-[#E4E4E7] flex-1 min-w-[200px]"
                        data-testid="model-approval-note" />
                    <button onClick={() => run("approve")} disabled={busy !== null}
                        className="px-3 py-1 font-mono text-[11px] tracking-widest border border-[#FFD700] text-[#FFD700] hover:bg-[#FFD700]/10 transition-colors disabled:opacity-40"
                        data-testid="model-approve-button">{busy === "approve" ? "…" : "APPROVE (STEP-UP MFA)"}</button>
                    <button onClick={() => run("promote")} disabled={busy !== null || approvals.length < 2 || !manifestNamesCandidate}
                        className="px-3 py-1 font-mono text-[11px] tracking-widest border border-[#00FF41] text-[#00FF41] hover:bg-[#00FF41]/10 transition-colors disabled:opacity-40"
                        data-testid="model-promote-button">{busy === "promote" ? "…" : "PROMOTE (STEP-UP MFA)"}</button>
                </div>
            )}
            {signHint && (
                <pre className="mt-2 p-2 bg-black border border-[#262626] text-[10px] text-[#A1A1AA] whitespace-pre-wrap break-all" data-testid="model-sign-hint">
                    {"# sign the manifest with the release signer, then PROMOTE:\n"}{signHint}
                </pre>
            )}
            {problems.length > 0 && (
                <ul className="mt-2 text-[10px] font-mono text-[#FF3B30] space-y-0.5" data-testid="model-approval-problems">
                    {problems.map((p, i) => <li key={i}>✗ {String(p)}</li>)}
                </ul>
            )}
        </section>
    );
}
