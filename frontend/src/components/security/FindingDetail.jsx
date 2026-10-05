import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { X, Loader2 } from "lucide-react";

export const SEV_CLS = {
    critical: "border-[#FF3B30]/60 text-[#FF3B30]",
    high: "border-[#FFB000]/60 text-[#FFB000]",
    medium: "border-[#3B82F6]/60 text-[#3B82F6]",
    low: "border-[#1F1F1F] text-[#71717A]",
};

export const SevPill = ({ sev }) => (
    <span className={`shrink-0 px-1.5 py-0.5 border font-mono text-[9px] tracking-widest ${SEV_CLS[sev] || SEV_CLS.low}`} data-testid={`sev-pill-${sev}`}>{(sev || "").toUpperCase()}</span>
);

const Field = ({ k, children, testid }) => (
    <div className="py-1.5 border-b border-[#141414] last:border-0" data-testid={testid}>
        <div className="font-mono text-[9px] tracking-widest text-[#52525B] uppercase">{k}</div>
        <div className="text-xs text-[#E4E4E7] whitespace-pre-wrap break-words">{children}</div>
    </div>
);

export function FindingDetail({ id, onChanged, onClose }) {
    const [f, setF] = useState(null);
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        if (!id) { setF(null); return; }
        let live = true;
        api.get(`/admin/security/findings/${id}`).then(r => live && setF(r.data)).catch(e => toast.error(formatApiError(e)));
        return () => { live = false; };
    }, [id]);

    const act = async (status) => {
        setBusy(true);
        try {
            const { data } = await api.post(`/admin/security/findings/${id}/status`, { status });
            setF(data); toast.success(`Finding marked ${status.replace("_", " ")}`); onChanged?.();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };

    const box = "bg-[#0A0A0A] border border-[#1F1F1F]";
    if (!id) return <div className={`${box} p-6 text-xs text-[#52525B]`} data-testid="finding-detail-empty">Select a finding to see the full report.</div>;
    if (!f) return <div className={`${box} p-6 flex justify-center`}><Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /></div>;
    const isOpen = ["open", "contained", "acknowledged"].includes(f.status);
    const b = "px-2.5 py-1 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] disabled:opacity-40";
    return (
        <div className={box} data-testid="finding-detail">
            <div className="px-4 py-2.5 border-b border-[#1F1F1F] flex items-center gap-2">
                <SevPill sev={f.severity} />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase truncate">{f.check_id} · {f.area}</span>
                <button onClick={onClose} className="ml-auto text-[#52525B] hover:text-white" data-testid="finding-detail-close"><X className="w-3.5 h-3.5" /></button>
            </div>
            <div className="p-4 max-h-[560px] overflow-y-auto">
                <div className="font-display text-sm text-white mb-2" data-testid="finding-title">{f.title}</div>
                <Field k="What happened" testid="finding-what">{f.what_happened}</Field>
                <Field k="Why it matters">{f.why_it_matters}</Field>
                <Field k="Action taken" testid="finding-action-taken">{f.action_taken}</Field>
                <Field k="Status / Fixed" testid="finding-status">{f.status.toUpperCase()} · fixed: {f.fixed}{f.status_note ? ` · ${f.status_note}` : ""}</Field>
                <Field k="Solution"><ol className="list-decimal ml-4 space-y-0.5">{(f.solution || []).map((s, i) => <li key={i}>{s}</li>)}</ol></Field>
                <Field k="Verify">{f.verify}</Field>
                <Field k="Timeline" testid="finding-timeline">
                    first {String(f.first_seen).slice(0, 19)}Z · last {String(f.last_seen).slice(0, 19)}Z · ×{f.occurrences}
                    {f.alert_count ? ` · alerted ${f.alert_count}× (last ${String(f.last_alert_at).slice(11, 16)}Z)` : " · not alerted"}
                    {f.resolved_at ? ` · resolved ${String(f.resolved_at).slice(0, 16)}Z by ${f.resolved_by}` : ""}
                </Field>
                <Field k="Evidence (masked)"><pre className="text-[10px] text-[#A1A1AA] overflow-x-auto" data-testid="finding-evidence">{JSON.stringify(f.evidence, null, 1)}</pre></Field>
                {isOpen && (
                    <div className="flex flex-wrap gap-2 pt-3" data-testid="finding-actions">
                        {f.status !== "acknowledged" && <button className={b} disabled={busy} onClick={() => act("acknowledged")} data-testid="finding-ack-btn">ACKNOWLEDGE</button>}
                        <button className={b} disabled={busy} onClick={() => act("resolved")} data-testid="finding-resolve-btn">MARK RESOLVED</button>
                        <button className={b} disabled={busy} onClick={() => act("false_positive")} data-testid="finding-fp-btn">FALSE POSITIVE</button>
                        <button className={`${b} opacity-40 cursor-not-allowed`} disabled title="Containment actions arrive with SA4 (enforce mode)" data-testid="finding-undo-btn">UNDO ACTION</button>
                    </div>
                )}
            </div>
        </div>
    );
}
