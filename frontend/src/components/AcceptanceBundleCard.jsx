import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, ShieldCheck, ShieldAlert } from "lucide-react";

/* A13 P1-01 / P0-02 — signed operational acceptance bundle + release gate, bound to the running release. */
export function AcceptanceBundleCard() {
    const [s, setS] = useState(null);
    const [gate, setGate] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        try {
            const [a, g] = await Promise.all([api.get("/admin/acceptance/current"), api.get("/admin/release-gate")]);
            setS(a.data); setGate(g.data);
        } catch (e) { toast.error(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const generate = async () => {
        setBusy(true);
        try {
            const { data } = await api.post("/admin/acceptance/bundle");
            toast[data.verdict === "PASS" ? "success" : "warning"](`Acceptance bundle ${data.verdict} · ${data.failures.length} failure(s)`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };

    const b = s?.latest;
    const ok = s?.valid_for_release;
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="acceptance-bundle-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-3">
                <div className="font-mono text-[10px] tracking-widest text-[#71717A]">04 · LIVE AUTHORITY GATES (real money only — Part 2 of the A13 audit)</div>
                <button onClick={generate} disabled={busy} data-testid="acceptance-bundle-generate"
                        className="font-mono text-[10px] tracking-widest px-3 py-1.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-50 transition-colors">
                    {busy ? <Loader2 className="h-3 w-3 animate-spin inline" /> : "GENERATE + SIGN BUNDLE"}
                </button>
            </div>
            <div className="px-4 py-3 space-y-3 text-sm">
                <div className="flex items-start gap-3" data-testid="release-gate-row">
                    {gate?.ok ? <ShieldCheck className="h-4 w-4 text-[#00FF41] mt-0.5" /> : <ShieldAlert className="h-4 w-4 text-[#FFB020] mt-0.5" />}
                    <div>
                        <div className="text-[#E4E4E7]">Release gate · {gate?.ok ? "authoritative, digest-pinned, signed" : "NOT authoritative"}{gate && !gate.enforced && <span className="text-[#71717A]"> · enforced in production only</span>}</div>
                        {gate?.failures?.length > 0 && <ul className="font-mono text-[10px] text-[#FFB020] mt-1 space-y-0.5">{gate.failures.map((f) => <li key={f}>· {f}</li>)}</ul>}
                        <div className="font-mono text-[10px] text-[#52525B] mt-1">lock {String(gate?.lock_commit || "-").slice(0, 12)} · image {String(gate?.image_digest || "not injected").slice(0, 24)}</div>
                        {gate?.accounts?.length > 0 && (
                            <ul className="font-mono text-[10px] mt-2 space-y-0.5 max-h-32 overflow-y-auto pr-2" data-testid="release-gate-accounts">
                                {/* N101-2 — per-account verdict: attested DEMO trades before an authoritative release, LIVE stays close-only */}
                                {gate.accounts.map((a) => (
                                    <li key={a.account_id} className={a.allowed ? "text-[#00FF41]" : "text-[#FFB020]"} data-testid={`release-gate-account-${a.account_id}`}>
                                        · {a.label} · {a.environment}: {a.allowed ? "allowed" : "blocked"} — {a.reason}
                                    </li>
                                ))}
                            </ul>
                        )}
                    </div>
                </div>
                <div className="flex items-start gap-3" data-testid="acceptance-bundle-row">
                    {ok ? <ShieldCheck className="h-4 w-4 text-[#00FF41] mt-0.5" /> : <ShieldAlert className="h-4 w-4 text-[#FFB020] mt-0.5" />}
                    <div className="flex-1">
                        <div className="text-[#E4E4E7]">Operational acceptance bundle · {b ? `${b.verdict} · ${String(b.created_at).slice(0, 16).replace("T", " ")} UTC · by ${b.created_by}` : "none generated"}{s && !s.required && <span className="text-[#71717A]"> · not required outside production</span>}</div>
                        <div className="font-mono text-[10px] text-[#71717A] mt-1" data-testid="acceptance-bundle-reason">{s?.reason} · {b?.account_ids?.length ?? 0} enabled account(s) evaluated · build {String(b?.build_sha || "-").slice(0, 7)} vs running {String(s?.release?.build_sha || "-").slice(0, 7)}</div>
                        {s?.accounts?.length > 0 && (
                            <div className="mt-2" data-testid="acceptance-bundle-accounts-wrap">
                                <div className="font-mono text-[10px] text-[#71717A]" data-testid="acceptance-bundle-accounts-summary">
                                    {s.accounts.filter((a) => a.gate_applies).length} live account(s) · {s.accounts.filter((a) => a.gate_applies && a.covered).length} covered · {s.accounts.filter((a) => !a.gate_applies).length} demo/paper (gate does not apply)
                                </div>
                                <ul className="font-mono text-[10px] mt-1 space-y-0.5 max-h-40 overflow-y-auto pr-2" data-testid="acceptance-bundle-accounts">
                                    {s.accounts.map((a) => (
                                        <li key={a.account_id} className={a.gate_applies ? (a.covered ? "text-[#00FF41]" : "text-[#FFB020]") : "text-[#52525B]"} data-testid={`acceptance-account-${a.account_id}`}>
                                            · {a.label} · {a.environment}{a.gate_applies ? ` · ${a.covered ? "COVERED" : `NOT COVERED — ${a.reason}`}` : " · gate does not apply (not real money)"}
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        )}
                        {b?.failures?.length > 0 && <ul className="font-mono text-[10px] text-[#FF4D4D] mt-1 space-y-0.5" data-testid="acceptance-bundle-failures">{b.failures.slice(0, 8).map((f) => <li key={f}>· {f}</li>)}</ul>}
                        {b && <div className="font-mono text-[10px] text-[#52525B] mt-1 break-all">digest {b.digest} · signed: verdict, accounts, release, expiry + evidence</div>}
                    </div>
                </div>
                <div className="font-mono text-[10px] text-[#52525B]">Live (real-money) authority stays CLOSE_ONLY until the release gate passes AND a PASSING bundle covers the account for this exact build/digest. Attested DEMO and paper accounts are exempt from both gates (N101-2).</div>
            </div>
        </section>
    );
}
