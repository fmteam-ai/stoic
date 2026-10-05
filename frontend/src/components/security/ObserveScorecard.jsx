import { useCallback, useEffect, useState } from "react";
import { ClipboardCheck, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const VERDICT = {
    safe_to_enforce: { label: "SAFE TO ENFORCE", cls: "border-[#00FF41]/60 text-[#00FF41] bg-[#00FF41]/10" },
    review: { label: "REVIEW", cls: "border-[#FF3B30]/60 text-[#FF3B30] bg-[#FF3B30]/10" },
    insufficient_data: { label: "INSUFFICIENT DATA", cls: "border-[#1F1F1F] text-[#71717A]" },
};

// Observe scorecard — per rule: proposals in the window and how many would have hit a
// real user (login from the IP, owner logged in, EA kept heartbeating, bot traded…).
export function ObserveScorecard({ status, onToggleRule, busy }) {
    const [days, setDays] = useState(14);
    const [d, setD] = useState(null);
    const [open, setOpen] = useState(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try { setD((await api.get(`/admin/security/scorecard?days=${days}`)).data); }
        catch { setD(null); }
        finally { setLoading(false); }
    }, [days]);

    useEffect(() => { load(); }, [load]);

    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="security-scorecard">
            <div className="px-4 py-2.5 border-b border-[#1F1F1F] flex flex-wrap items-center gap-2">
                <ClipboardCheck className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">Observe scorecard · would a rule have hit a real user?</span>
                <div className="ml-auto flex items-center gap-2">
                    {[7, 14, 30].map((n) => (
                        <button key={n} onClick={() => setDays(n)} data-testid={`security-scorecard-days-${n}`}
                                className={`px-2 py-0.5 font-mono text-[10px] border ${days === n ? "border-[#00FF41]/60 text-[#00FF41]" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>{n}D</button>
                    ))}
                    <button onClick={load} disabled={loading} className="text-[#52525B] hover:text-white" data-testid="security-scorecard-refresh"><RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /></button>
                </div>
            </div>
            {!d ? (
                <div className="px-4 py-3 font-mono text-[10px] text-[#52525B]">{loading ? "building…" : "scorecard unavailable"}</div>
            ) : (
                <table className="w-full font-mono text-[10px]">
                    <thead>
                        <tr className="text-[#52525B] tracking-widest text-left">
                            <th className="px-3 py-1.5">RULE</th><th className="px-2 py-1.5 text-right">PROPOSALS</th><th className="px-2 py-1.5 text-right">WOULD DO</th>
                            <th className="px-2 py-1.5 text-right">DONE</th><th className="px-2 py-1.5 text-right">REFUSED</th><th className="px-2 py-1.5 text-right">REAL-USER HITS</th>
                            <th className="px-2 py-1.5">VERDICT</th><th className="px-2 py-1.5 text-right">ENFORCE</th>
                        </tr>
                    </thead>
                    <tbody>
                        {d.rules.map((r) => {
                            const v = VERDICT[r.verdict] || VERDICT.insufficient_data;
                            const isOpen = open === r.rule;
                            return [
                                <tr key={r.rule} className="border-t border-[#1F1F1F] hover:bg-white/[0.02] cursor-pointer" onClick={() => setOpen(isOpen ? null : r.rule)} data-testid={`security-scorecard-row-${r.rule}`}>
                                    <td className="px-3 py-1.5 text-white"><span className="text-[#00FF41]">{r.rule}</span> <span className="text-[#71717A]">{r.title}</span></td>
                                    <td className="px-2 py-1.5 text-right text-[#E4E4E7]">{r.proposals}</td>
                                    <td className="px-2 py-1.5 text-right text-[#E4E4E7]">{r.would_have_done}</td>
                                    <td className="px-2 py-1.5 text-right text-[#E4E4E7]">{r.done}</td>
                                    <td className="px-2 py-1.5 text-right text-[#E4E4E7]">{r.refused}</td>
                                    <td className={`px-2 py-1.5 text-right ${r.real_user_hits ? "text-[#FF3B30]" : "text-[#E4E4E7]"}`} data-testid={`security-scorecard-hits-${r.rule}`}>{r.real_user_hits}{r.false_positive_rate != null ? ` (${Math.round(r.false_positive_rate * 100)}%)` : ""}</td>
                                    <td className="px-2 py-1.5"><span className={`px-1.5 py-0.5 border tracking-widest ${v.cls}`} title={r.why} data-testid={`security-scorecard-verdict-${r.rule}`}>{v.label}</span></td>
                                    <td className="px-2 py-1.5 text-right">
                                        <button disabled={busy} onClick={(e) => { e.stopPropagation(); onToggleRule(r.rule); }} data-testid={`security-scorecard-toggle-${r.rule}`}
                                                className={`px-2 py-0.5 border tracking-widest ${r.enabled ? "border-[#00FF41]/60 text-[#00FF41]" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>
                                            {r.enabled ? "ON" : "OFF"}
                                        </button>
                                    </td>
                                </tr>,
                                isOpen && (
                                    <tr key={`${r.rule}-x`} className="border-t border-[#1F1F1F] bg-black/40" data-testid={`security-scorecard-detail-${r.rule}`}>
                                        <td colSpan={8} className="px-4 py-2 text-[#A1A1AA]">
                                            <div>{r.why} · {r.distinct_targets} distinct target(s) · action <span className="text-white">{r.action}</span></div>
                                            {Array.isArray(r.checklist) && (
                                                <ul className="mt-1.5 grid sm:grid-cols-2 gap-x-4 gap-y-0.5" data-testid={`security-scorecard-checklist-${r.rule}`}>
                                                    {r.checklist.map((c) => (
                                                        <li key={c.id} className={c.ok ? "text-[#00FF41]/90" : "text-[#FFB000]"}>{c.ok ? "✓" : "✗"} {c.label}</li>
                                                    ))}
                                                    <li className={`sm:col-span-2 tracking-widest ${r.promotable ? "text-[#00FF41]" : "text-[#71717A]"}`} data-testid={`security-scorecard-promotable-${r.rule}`}>
                                                        {r.promotable ? "PROMOTION CHECKLIST COMPLETE — enabling is a deliberate step-up action" : "PROMOTION BLOCKED — keep observing"}
                                                    </li>
                                                </ul>
                                            )}
                                            {r.examples.length ? (
                                                <ul className="mt-1 space-y-0.5">
                                                    {r.examples.map((ex, i) => (
                                                        <li key={i} className="text-[#FF3B30]/90">{String(ex.at).slice(0, 16)} · {ex.target} · {ex.status.replace(/_/g, " ")} → {ex.evidence}</li>
                                                    ))}
                                                </ul>
                                            ) : <div className="text-[#52525B] mt-1">no real-user hit found in ±{d.window_h} h around any proposal</div>}
                                        </td>
                                    </tr>
                                ),
                            ];
                        })}
                    </tbody>
                </table>
            )}
            <div className="px-4 py-2 border-t border-[#1F1F1F] font-mono text-[10px] text-[#52525B]">
                Mode {status?.mode?.toUpperCase()} · a verdict needs ≥ {d?.min_proposals ?? 5} proposals · evidence for you, never an automatic switch · enabling a rule asks for step-up
            </div>
        </div>
    );
}

export default ObserveScorecard;
