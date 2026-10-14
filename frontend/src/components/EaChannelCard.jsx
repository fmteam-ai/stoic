import { useCallback, useEffect, useState } from "react";
import { GitBranch, ArrowUpCircle, Undo2, CheckCircle2, XCircle } from "lucide-react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";

const short = (h) => (h ? `${String(h).slice(0, 10)}…` : "—");
const shas = (m) => (m && Object.keys(m).length ? Object.entries(m).map(([n, s]) => `${n} ${short(s)}`).join(" · ") : "—");

// v1.60.35 (4) — EA release channel: candidate → stable promotion with the server-side readiness gate
// (/ops/releases.readiness), step-up re-auth on POST (handled by the api interceptor), and one-click rollback.
export function EaChannelCard() {
    const [d, setD] = useState(null);
    const [busy, setBusy] = useState(null);
    const load = useCallback(() => api.get("/ops/releases").then((r) => setD(r.data)).catch(() => setD(null)), []);
    useEffect(() => { load(); }, [load]);
    const act = async (kind) => {
        setBusy(kind);
        try {
            await api.post(`/ops/releases/${kind}`);
            toast.success(kind === "promote" ? "Candidate promoted to stable" : "Rolled back to previous stable (channel pinned)");
            load();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setBusy(null); }
    };
    const st = d?.state || {};
    const rd = d?.readiness || {};
    const soak = (rd.checks || []).find((c) => c.id === "soak");
    const row = "flex justify-between gap-3 py-1.5 border-b border-[#141414] last:border-0 font-mono text-[11px]";
    const btn = "px-3 py-1.5 font-mono text-[10px] tracking-widest inline-flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="ea-channel-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <GitBranch className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">EA Release Channel</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">{st.pinned ? "PINNED" : "AUTO-SOAK"}</span>
            </div>
            <div className="px-4 py-2">
                <div className={row}><span className="text-[#71717A]">STABLE</span><span className="text-[#E4E4E7] text-right" data-testid="ea-channel-stable">{shas(st.stable)}</span></div>
                <div className={row}><span className="text-[#71717A]">CANDIDATE</span><span className="text-[#E4E4E7] text-right" data-testid="ea-channel-candidate">{shas(st.candidate)}</span></div>
                <div className={row}><span className="text-[#71717A]">SOAK</span>
                    <span className="text-[#E4E4E7]">{soak?.elapsed_hours != null ? `${soak.elapsed_hours} / ${soak.required_hours} h` : "—"}</span></div>
                <ul className="mt-2 space-y-1" data-testid="ea-channel-checks">
                    {(rd.checks || []).map((c) => (
                        <li key={c.id} className={`font-mono text-[11px] flex items-center gap-2 ${c.ok ? "text-[#A1A1AA]" : "text-[#FFB000]"}`}>
                            {c.ok ? <CheckCircle2 className="w-3 h-3 text-[#00FF41]" /> : <XCircle className="w-3 h-3" />}
                            {c.label}{c.id === "ea_signed" && c.ea_version ? ` (EA ${c.ea_version})` : ""}
                        </li>
                    ))}
                </ul>
                <div className="mt-3 flex flex-wrap gap-2">
                    <button onClick={() => act("promote")} disabled={!rd.eligible || busy !== null}
                        title={rd.eligible ? "Admin step-up required" : (rd.blocking || []).join("; ")}
                        className={`${btn} bg-[#00FF41] text-black`} data-testid="ea-channel-promote">
                        <ArrowUpCircle className="w-3.5 h-3.5" /> {busy === "promote" ? "PROMOTING…" : "PROMOTE TO STABLE"}
                    </button>
                    <button onClick={() => act("rollback")} disabled={!rd.can_rollback || busy !== null}
                        title={rd.can_rollback ? `Back to ${shas(rd.previous_stable)} — pins the channel` : "no previous stable recorded"}
                        className={`${btn} border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10`} data-testid="ea-channel-rollback">
                        <Undo2 className="w-3.5 h-3.5" /> {busy === "rollback" ? "ROLLING BACK…" : "ROLLBACK TO PREVIOUS STABLE"}
                    </button>
                </div>
                <div className="mt-2 font-mono text-[9px] tracking-widest text-[#52525B]">ADMIN ONLY · STEP-UP RE-AUTH · AUDITED (WHO · WHEN · FROM → TO)</div>
                {d?.history?.length > 0 && (
                    <ul className="mt-2 space-y-0.5" data-testid="ea-channel-history">
                        {d.history.slice(0, 4).map((h) => (
                            <li key={h.event_id} className="font-mono text-[10px] text-[#52525B]">
                                {new Date(h.at).toLocaleString()} · {h.event}{h.detail?.by ? ` · ${h.detail.by}` : ""}
                            </li>
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}

export default EaChannelCard;
