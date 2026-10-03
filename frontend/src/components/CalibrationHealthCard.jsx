import { Crosshair } from "lucide-react";

const STATE = {
    active: { tone: "good", label: "ACTIVE", note: "Platt calibration fitted under the standard convention and applied to p_win." },
    legacy_ignored: { tone: "warn", label: "LEGACY · IGNORED", note: "Fitted before the C1 fix (inverted convention). Raw model probability is used until the model worker refits." },
    pending: { tone: "neutral", label: "PENDING REFIT", note: "No calibration yet (too few samples or not trained)." },
};
const TONES = {
    neutral: "border-[#1F1F1F] text-[#A1A1AA]",
    good: "border-[#00FF41]/40 text-[#00FF41]",
    warn: "border-[#FFB000]/40 text-[#FFB000]",
    bad: "border-[#FF3B30]/40 text-[#FF3B30]",
};
const fmt = (v, d = 3) => (v == null ? "—" : Number(v).toFixed(d));
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "never");

function Row({ m }) {
    const st = STATE[m.state] || STATE.pending;
    return (
        <div className="grid grid-cols-[1fr_auto] lg:grid-cols-[1.4fr_auto_1fr_1fr_1fr] gap-x-3 gap-y-1 items-center px-4 py-2 border-t border-[#1F1F1F]"
            data-testid={`calibration-row-${m.family}-${m.key}`}>
            <div className="min-w-0">
                <div className="font-mono text-xs truncate" title={m.key}>{m.label || m.key}</div>
                <div className="font-mono text-[9px] tracking-widest text-[#52525B]">{m.family.toUpperCase()} · {m.backend || "—"} · n={m.n_samples ?? "—"} · {when(m.trained_at)}</div>
            </div>
            <span className={`px-2 py-0.5 border font-mono text-[10px] tracking-widest whitespace-nowrap ${TONES[st.tone]}`}
                title={st.note} data-testid={`calibration-state-${m.family}-${m.key}`}>{st.label}</span>
            <div className="font-mono text-[10px] text-[#A1A1AA] col-span-2 lg:col-span-1 truncate" title={m.note}>{m.note}</div>
            <div className="font-mono text-[10px] text-[#A1A1AA] hidden lg:block">Brier {fmt(m.brier_raw, 4)} → {fmt(m.brier_calibrated, 4)}</div>
            <div className="font-mono text-[10px] text-[#A1A1AA] hidden lg:block">ECE {fmt(m.ece_raw)} → {fmt(m.ece_calibrated)}</div>
        </div>
    );
}

export function CalibrationHealthCard({ data }) {
    if (!data || data.detail) return null;   // admin-only endpoint — hidden for non-admins
    const c = data.counts || {};
    const headTone = c.legacy_ignored ? "warn" : c.active ? "good" : "neutral";
    const headLabel = c.legacy_ignored ? `${c.legacy_ignored} LEGACY IGNORED` : c.active ? `${c.active} ACTIVE` : "NO MODELS";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="calibration-health-card">
            <div className="px-4 py-3 flex items-center gap-2 flex-wrap">
                <Crosshair className="w-3.5 h-3.5 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Calibration Health</span>
                <span className={`px-2 py-0.5 border font-mono text-[10px] tracking-widest ${TONES[headTone]}`} data-testid="calibration-summary">{headLabel}</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    PLATT · {String(data.convention || "standard").toUpperCase()} · {c.active ?? 0} active · {c.legacy_ignored ?? 0} legacy · {c.pending ?? 0} pending
                </span>
            </div>
            {(data.models || []).length === 0 ? (
                <div className="px-4 py-3 border-t border-[#1F1F1F] font-mono text-xs text-[#52525B]" data-testid="calibration-empty">
                    No trained models yet — calibration appears after the first learned-meta or scalp model fit.
                </div>
            ) : (data.models || []).map((m) => <Row key={`${m.family}-${m.key}`} m={m} />)}
        </div>
    );
}
