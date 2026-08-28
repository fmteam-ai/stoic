import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { Loader2, Zap } from "lucide-react";

const box = "border border-[#1F1F1F] bg-[#0A0A0A] p-4 font-mono text-xs";
const btn = "border border-[#1F1F1F] px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-[#A1A1AA] hover:border-[#00FF41] hover:text-[#00FF41] disabled:opacity-40 transition-colors";

function StatusDot({ ok }) {
    return <span className={ok ? "text-[#00FF41]" : "text-[#FF3B30]"}>{ok ? "✓" : "✗"}</span>;
}

export function PammStrategyPanel({ programId, onChanged }) {
    const [meta, setMeta] = useState(null);
    const [current, setCurrent] = useState(null);
    const [pick, setPick] = useState(null);
    const [riskProfile, setRiskProfile] = useState("controlled");
    const [nitro, setNitro] = useState(null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        if (!programId) return;
        try {
            const [s, c] = await Promise.all([
                api.get("/pamm/strategies"),
                api.get(`/pamm/programs/${programId}/strategy`),
            ]);
            setMeta(s.data);
            setCurrent(c.data);
            const cur = c.data.assignment;
            if (cur) { setPick(cur.strategy_id); setRiskProfile(cur.risk_profile_id); }
        } catch (e) { toast.error(formatApiError(e)); }
    }, [programId]);

    useEffect(() => { load(); }, [load]);

    useEffect(() => {
        if (pick === "nitro_scalper" || pick === "fast_scalp") {
            api.get("/pamm/strategies/nitro-eligibility")
                .then(r => setNitro(r.data)).catch(() => setNitro(null));
        }
    }, [pick]);

    const run = async (fn, label) => {
        setBusy(true);
        try {
            const out = await fn();
            toast.success(`${label} ok`);
            await load();
            onChanged?.();
            return out;
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const assign = () => run(() => api.post(`/pamm/programs/${programId}/strategy`,
        { mode: "SINGLE", strategy_id: pick, risk_profile_id: riskProfile }), "Assign");
    const validate = () => run(() => api.post(`/pamm/programs/${programId}/strategy/validate`), "Validate");
    const activate = () => run(() => api.post(`/pamm/programs/${programId}/strategy/activate`), "Activate");
    const suspendA = () => run(() => api.post(`/pamm/programs/${programId}/strategy/suspend`, { reason: "manual" }), "Suspend");

    if (!meta || !current) return (
        <div className={box} data-testid="pamm-strategy-panel">
            <Loader2 className="h-4 w-4 animate-spin text-[#52525B]" />
        </div>
    );

    const a = current.assignment;
    const defn = meta.strategies.find(s => s.strategy_id === (a?.strategy_id || pick));
    const lv = a?.last_validation;

    return (
        <div className={box + " space-y-4"} data-testid="pamm-strategy-panel">
            <div className="flex items-center justify-between">
                <span className="uppercase tracking-widest text-[#52525B]">Strategy Profile</span>
                <span className="text-[#52525B]" data-testid="pamm-strategy-mode">MODE: {current.mode}</span>
            </div>

            <div className="flex gap-3 text-[11px]" data-testid="pamm-strategy-mode-picker">
                {["SINGLE", "MULTI", "DYNAMIC_AI"].map(m => (
                    <label key={m} className={m === "SINGLE" ? "text-[#E4E4E7]" : "text-[#3F3F46]"}>
                        <input type="radio" checked={m === "SINGLE"} disabled={m !== "SINGLE"} readOnly className="mr-1 accent-[#00FF41]" />
                        {m}{m !== "SINGLE" && " (soon)"}
                    </label>
                ))}
            </div>

            <div className="grid grid-cols-2 gap-2" data-testid="pamm-strategy-options">
                {meta.strategies.map(s => (
                    <button key={s.strategy_id} disabled={busy || !!a}
                        onClick={() => setPick(s.strategy_id)}
                        data-testid={`pamm-strategy-option-${s.strategy_id}`}
                        className={`border p-2 text-left transition-colors ${(a?.strategy_id || pick) === s.strategy_id ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#3F3F46]"}`}>
                        <div className="uppercase tracking-wider">{s.display_name}</div>
                        <div className="text-[10px] text-[#52525B]">
                            v{s.version} · {s.characteristics.frequency} freq · {s.characteristics.holding_time} hold
                        </div>
                    </button>
                ))}
            </div>

            {defn && (
                <div className="space-y-1 text-[11px] text-[#A1A1AA]" data-testid="pamm-strategy-detail">
                    <div className="flex justify-between"><span>Version</span><span className="text-[#E4E4E7]">{a?.strategy_version || defn.version} · {(a?.strategy_hash || defn.strategy_hash).slice(0, 10)}</span></div>
                    <div className="flex justify-between"><span>Latency sensitivity</span><span>{defn.characteristics.latency_sensitivity}</span></div>
                    <div className="flex justify-between"><span>Spread sensitivity</span><span>{defn.characteristics.spread_sensitivity}</span></div>
                    <div className="flex justify-between"><span>Certification</span><span data-testid="pamm-strategy-cert-status">{a?.certification_status || "—"}</span></div>
                    <div className="flex justify-between"><span>Status</span><span className="text-[#E4E4E7]" data-testid="pamm-strategy-status">{a?.status || "NOT ASSIGNED"}</span></div>
                </div>
            )}

            {(pick === "nitro_scalper" || pick === "fast_scalp") && nitro && (
                <div className="border border-[#1F1F1F] p-2 space-y-1 text-[11px]" data-testid="pamm-nitro-eligibility">
                    <div className="flex items-center justify-between">
                        <span className="flex items-center gap-1 text-[#FFB000]"><Zap className="h-3 w-3" /> NITRO ELIGIBILITY</span>
                        <span className={nitro.status === "NITRO_ENABLED" ? "text-[#00FF41]" : nitro.status === "NITRO_REDUCED" ? "text-[#FFB000]" : "text-[#FF3B30]"}>
                            {nitro.score}/100 · {nitro.status.replace("NITRO_", "")}
                        </span>
                    </div>
                    {Object.entries(nitro.components).map(([k, v]) => (
                        <div key={k} className="flex justify-between text-[#52525B]">
                            <span>{k.replace(/_/g, " ")}</span><span>{v}</span>
                        </div>
                    ))}
                </div>
            )}

            {lv && (
                <div className="space-y-0.5 text-[11px]" data-testid="pamm-strategy-validation">
                    {lv.checks.map(c => (
                        <div key={c.key} className="flex justify-between text-[#A1A1AA]">
                            <span>{c.key.replace(/_/g, " ")}</span><StatusDot ok={c.ok} />
                        </div>
                    ))}
                </div>
            )}

            <div className="flex flex-wrap gap-2">
                {!a && <>
                    <select value={riskProfile} onChange={e => setRiskProfile(e.target.value)}
                        data-testid="pamm-strategy-risk-select"
                        className="bg-[#0A0A0A] border border-[#1F1F1F] px-2 py-1.5 text-[11px] text-[#A1A1AA]">
                        {(meta.risk_profiles || []).map(rp => (
                            <option key={rp.risk_profile_id} value={rp.risk_profile_id}>{rp.display_name} risk</option>
                        ))}
                    </select>
                    <button onClick={assign} disabled={busy || !pick} className={btn} data-testid="pamm-strategy-assign-button">Assign</button>
                </>}
                {a && a.status !== "ACTIVE" && <>
                    <button onClick={validate} disabled={busy} className={btn} data-testid="pamm-strategy-validate-button">Validate</button>
                    <button onClick={activate} disabled={busy || !lv?.passed} className={btn} data-testid="pamm-strategy-activate-button">Activate</button>
                </>}
                {a?.status === "ACTIVE" &&
                    <button onClick={suspendA} disabled={busy} className={btn} data-testid="pamm-strategy-suspend-button">Suspend</button>}
            </div>
            <div className="text-[10px] text-[#3F3F46]">
                PAMM chooses the profile · the strategy decides · PAMM risk governs capital · Execution Authority is the only path to MT5. No assignment = LEGACY (unchanged behavior).
            </div>
        </div>
    );
}
