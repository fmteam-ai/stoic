import { useCallback, useEffect, useState } from "react";
import { ShieldCheck, ShieldAlert, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const short = (h) => (h ? `${String(h).split("@").pop().slice(0, 19)}…${String(h).slice(-6)}` : "—");

// v1.60.35 (1) — renders release_gate.evaluate() from GET /admin/release-gate verbatim: ONE source of truth.
// No client-side re-derivation of the verdict.
export function ReleaseGateCard() {
    const [d, setD] = useState(null);
    const [err, setErr] = useState(null);
    const load = useCallback(() => {
        api.get("/admin/release-gate").then((r) => { setD(r.data); setErr(null); })
            .catch((e) => setErr(e?.response?.data?.detail || "unavailable"));
    }, []);
    useEffect(() => { load(); }, [load]);
    const ok = d?.ok === true;
    const row = "flex justify-between gap-3 py-1.5 border-b border-[#141414] last:border-0 font-mono text-[11px]";
    return (
        <div className={`border ${ok ? "border-[#00FF41]/30" : "border-[#FF3B30]/30"} bg-[#0A0A0A]`} data-testid="release-gate-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                {ok ? <ShieldCheck className="w-3.5 h-3.5 text-[#00FF41]" /> : <ShieldAlert className="w-3.5 h-3.5 text-[#FF3B30]" />}
                <span className="font-display font-bold text-sm">Release Gate</span>
                <span className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${ok ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}
                    data-testid="release-gate-verdict">
                    {d ? (ok ? "PASS" : "FAIL") : "…"}
                </span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    {d?.enforced ? "ENFORCED · LIVE ACCOUNTS" : "NOT ENFORCED (non-production)"}
                </span>
                <button onClick={load} className="text-[#52525B] hover:text-white" data-testid="release-gate-refresh" aria-label="Refresh">
                    <RefreshCw className="w-3 h-3" />
                </button>
            </div>
            <div className="px-4 py-2">
                {err && <div className="font-mono text-[11px] text-[#FF3B30]" data-testid="release-gate-error">{String(err)}</div>}
                {d && (
                    <>
                        <div className={row}><span className="text-[#71717A]">LOCK COMMIT</span><span className="text-[#E4E4E7]" data-testid="release-gate-lock-commit">{d.lock_commit ? String(d.lock_commit).slice(0, 12) : "—"}</span></div>
                        <div className={row}><span className="text-[#71717A]">RUNNING DIGEST</span><span className="text-[#E4E4E7]">{short(d.image_digest)}</span></div>
                        <div className={row}><span className="text-[#71717A]">LOCKED DIGEST</span><span className="text-[#E4E4E7]">{short(d.locked_backend_digest)}</span></div>
                        <div className={row}><span className="text-[#71717A]">LOCK ↔ BUILD</span>
                            <span className={d.lock_binding?.ok ? "text-[#00FF41]" : "text-[#FFB000]"}>{d.lock_binding ? (d.lock_binding.ok ? "bound" : d.lock_binding.reason) : "n/a"}</span></div>
                        {d.failures?.length > 0 && (
                            <ul className="mt-2 space-y-1" data-testid="release-gate-failures">
                                {d.failures.map((f, i) => (
                                    <li key={i} className="font-mono text-[11px] text-[#FF3B30] flex gap-2"><span>✕</span><span className="break-words">{f}</span></li>
                                ))}
                            </ul>
                        )}
                        {d.accounts?.length > 0 && (
                            <div className="mt-2 flex flex-wrap gap-1.5 items-center" data-testid="release-gate-accounts">
                                <span className="font-mono text-[10px] text-[#71717A]">
                                    {d.accounts.filter((a) => a.allowed).length}/{d.accounts.length} accounts trade
                                </span>
                                {d.accounts.filter((a) => !a.allowed).slice(0, 12).map((a) => (
                                    <span key={a.account_id} title={a.reason}
                                        className="font-mono text-[10px] px-1.5 py-0.5 border border-[#FF3B30]/30 text-[#FF3B30]">
                                        {a.label} · {a.environment} · CLOSE-ONLY
                                    </span>
                                ))}
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

export default ReleaseGateCard;
