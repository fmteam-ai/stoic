import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { Loader2, ShieldCheck } from "lucide-react";

const box = "border border-[#1F1F1F] bg-[#0A0A0A] p-4 font-mono text-xs";
const btn = "border border-[#1F1F1F] px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-[#A1A1AA] hover:border-[#00FF41] hover:text-[#00FF41] disabled:opacity-40 transition-colors";

const PIPELINE = ["DRAFT", "VALIDATING", "REPLAY", "SHADOW", "DEMO", "CANARY", "CERTIFIED", "LIVE"];

function stageColor(stage, state) {
    const cur = PIPELINE.indexOf(state);
    const idx = PIPELINE.indexOf(stage);
    if (state === "REVOKED") return "text-[#FF3B30]";
    if (idx < cur) return "text-[#00FF41]";
    if (idx === cur) return stage === "LIVE" ? "text-[#00FF41]" : "text-[#FFB000]";
    return "text-[#3F3F46]";
}

export function PammCertificationPanel({ programId, isAdmin }) {
    const [data, setData] = useState(null);
    const [evid, setEvid] = useState(null);
    const [busy, setBusy] = useState(false);
    const [metricsJson, setMetricsJson] = useState("");

    const load = useCallback(async () => {
        if (!programId) return;
        try {
            const r = await api.get(`/pamm/programs/${programId}/certification`);
            setData(r.data);
            if (r.data.campaign) {
                const e = await api.get(`/pamm/programs/${programId}/certification/evidence`).catch(() => null);
                setEvid(e?.data || null);
            } else setEvid(null);
        } catch (e) { toast.error(formatApiError(e)); }
    }, [programId]);

    useEffect(() => { load(); }, [load]);

    const run = async (fn, label) => {
        setBusy(true);
        try { await fn(); toast.success(`${label} ok`); await load(); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const startC = () => run(() => api.post(`/pamm/programs/${programId}/certification/start`), "Campaign start");
    const evaluate = () => run(() => api.post(`/pamm/programs/${programId}/certification/evaluate`), "Evaluate");
    const advance = () => run(() => api.post(`/pamm/programs/${programId}/certification/advance`), "Advance");
    const revoke = () => run(() => api.post(`/pamm/programs/${programId}/certification/revoke`, { reason: "manual" }), "Revoke");
    const record = () => {
        let metrics;
        try { metrics = JSON.parse(metricsJson); }
        catch { toast.error("Checkpoint metrics must be valid JSON"); return; }
        return run(() => api.post(`/pamm/programs/${programId}/certification/checkpoint`, { metrics }), "Checkpoint");
    };

    if (!data) return (
        <div className={box} data-testid="pamm-cert-panel">
            <Loader2 className="h-4 w-4 animate-spin text-[#52525B]" />
        </div>
    );

    const camp = data.campaign;
    const state = camp?.state;
    const evalR = camp?.last_evaluation || data.evaluation_preview;

    return (
        <div className={box + " space-y-4"} data-testid="pamm-cert-panel">
            <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5 uppercase tracking-widest text-[#52525B]">
                    <ShieldCheck className="h-3.5 w-3.5" /> Certification Campaign
                </span>
                {camp && <span className="text-[#E4E4E7]" data-testid="pamm-cert-state">{state}</span>}
            </div>

            <div className="flex flex-wrap items-center gap-1 text-[10px]" data-testid="pamm-cert-pipeline">
                {PIPELINE.map((s, i) => (
                    <span key={s} className="flex items-center gap-1">
                        <span className={stageColor(s, state || "DRAFT")}>{s}</span>
                        {i < PIPELINE.length - 1 && <span className="text-[#27272A]">→</span>}
                    </span>
                ))}
            </div>

            {!camp && (
                <div className="text-[11px] text-[#52525B]">
                    No campaign. A PAMM × strategy combo earns LIVE status only through
                    REPLAY → SHADOW → DEMO → CANARY evidence.
                </div>
            )}

            {camp && (
                <div className="space-y-1 text-[11px] text-[#A1A1AA]" data-testid="pamm-cert-detail">
                    <div className="flex justify-between"><span>Identity</span><span className="text-[#E4E4E7]">{camp.identity.identity_hash.slice(0, 12)} · {camp.identity.strategy_id} v{camp.identity.strategy_version}</span></div>
                    <div className="flex justify-between"><span>Environment</span><span>{camp.identity.execution_environment}</span></div>
                    {camp.cert_id && <div className="flex justify-between"><span>Certificate</span><span className="text-[#00FF41]" data-testid="pamm-cert-id">{camp.cert_id}</span></div>}
                    {evid && <div className="flex justify-between"><span>Evidence chain</span>
                        <span data-testid="pamm-cert-chain">{evid.count} records · <span className={evid.chain_valid ? "text-[#00FF41]" : "text-[#FF3B30]"}>{evid.chain_valid ? "CHAIN OK" : "CHAIN BROKEN"}</span></span></div>}
                </div>
            )}

            {evalR?.checks && (
                <div className="space-y-0.5 text-[11px]" data-testid="pamm-cert-checks">
                    <div className="text-[#52525B] uppercase tracking-wider">{evalR.stage} gate · {evalR.passed ? <span className="text-[#00FF41]">PASS</span> : <span className="text-[#FF3B30]">FAIL</span>}</div>
                    {evalR.checks.map(c => (
                        <div key={c.key} className="flex justify-between text-[#A1A1AA]">
                            <span>{c.key.replace(/_/g, " ")}</span>
                            <span>
                                <span className="text-[#52525B] mr-2">{String(c.actual ?? "—")} / {String(c.required)}</span>
                                <span className={c.ok ? "text-[#00FF41]" : "text-[#FF3B30]"}>{c.ok ? "✓" : "✗"}</span>
                            </span>
                        </div>
                    ))}
                </div>
            )}

            {isAdmin && (
                <div className="space-y-2">
                    {camp && ["REPLAY", "SHADOW", "DEMO", "CANARY"].includes(state) && (
                        <textarea value={metricsJson} onChange={e => setMetricsJson(e.target.value)}
                            placeholder='Checkpoint metrics JSON, e.g. {"decisions_replayed": 250, "determinism_ok": true}'
                            data-testid="pamm-cert-metrics-input" rows={2}
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] p-2 text-[10px] text-[#A1A1AA] placeholder:text-[#3F3F46]" />
                    )}
                    <div className="flex flex-wrap gap-2">
                        {!camp && <button onClick={startC} disabled={busy} className={btn} data-testid="pamm-cert-start-button">Start Campaign</button>}
                        {camp && ["REPLAY", "SHADOW", "DEMO", "CANARY"].includes(state) && <>
                            <button onClick={record} disabled={busy || !metricsJson} className={btn} data-testid="pamm-cert-checkpoint-button">Record Checkpoint</button>
                            <button onClick={evaluate} disabled={busy} className={btn} data-testid="pamm-cert-evaluate-button">Evaluate</button>
                        </>}
                        {camp && state !== "LIVE" && state !== "REVOKED" && <button onClick={advance} disabled={busy} className={btn} data-testid="pamm-cert-advance-button">Advance →</button>}
                        {camp && state !== "REVOKED" && <button onClick={revoke} disabled={busy} className={btn + " hover:border-[#FF3B30] hover:text-[#FF3B30]"} data-testid="pamm-cert-revoke-button">Revoke</button>}
                    </div>
                </div>
            )}
            <div className="text-[10px] text-[#3F3F46]">
                Missing evidence FAILS the gate — certification runs on evidence, never assumptions. Advancement requires admin step-up MFA.
            </div>
        </div>
    );
}
