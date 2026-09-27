import { useEffect, useState } from "react";
import api from "@/lib/api";
import { GitBranch, Snowflake, ArrowRight } from "lucide-react";

const STAGE_NAMES = {
    live_trades: "LIVE TRADES", replay: "REPLAY", shadow: "SHADOW",
    validation: "VALIDATION", approval: "APPROVAL", production: "PRODUCTION",
};

const statusColor = (s) =>
    !s ? "#71717A"
        : s === "promoted" || s === "versioned_update" ? "#00FF41"
        : s === "candidate_ready_for_review" ? "#FFD700"
        : s === "rejected" ? "#FF3B30"
        : s.startsWith("failed") ? "#FF3B30"
        : "#FFD700";

export const LearningPipelineCard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/ml/learning-pipeline")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d) return null;
    const lastRun = d.recent_runs?.[0];
    const ml = lastRun?.stages?.ml_ensemble;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="learning-pipeline-card">
            <div className="flex items-center gap-2 flex-wrap">
                <GitBranch className="w-4 h-4 text-[#A78BFA]" />
                <span className="font-display font-bold text-sm text-white">Continuous Learning Pipeline</span>
                {d.freeze.frozen ? (
                    <span className="flex items-center gap-1 font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#38BDF8]/40 text-[#38BDF8]"
                        data-testid="learning-freeze-badge">
                        <Snowflake className="w-3 h-3" /> LEARNING FROZEN
                    </span>
                ) : (
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#00FF41]/40 text-[#00FF41]"
                        data-testid="learning-freeze-badge">
                        GATE OPEN
                    </span>
                )}
                <span className="font-mono text-[9px] text-[#52525B] ml-auto">
                    {d.rollback_versions} ROLLBACK SNAPSHOTS
                </span>
            </div>
            <div className="flex items-center gap-1 flex-wrap mt-3" data-testid="learning-workflow">
                {d.workflow.map((s, i) => (
                    <span key={s} className="flex items-center gap-1">
                        <span className="font-mono text-[9px] tracking-widest px-1.5 py-0.5 border border-[#2A2A2A] text-[#A1A1AA]">
                            {STAGE_NAMES[s] || s.toUpperCase()}
                        </span>
                        {i < d.workflow.length - 1 && <ArrowRight className="w-3 h-3 text-[#3F3F46]" />}
                    </span>
                ))}
            </div>
            <div className="font-mono text-[9px] text-[#71717A] mt-2" data-testid="learning-freeze-reason">
                {d.freeze.reason}
            </div>
            {d.ml_state && (
                <div className="mt-2 pt-2 border-t border-[#141414] font-mono text-[10px] flex flex-wrap gap-x-4 gap-y-1"
                    data-testid="ml-model-state">
                    <span data-testid="ml-production-state">
                        <span className="text-[#52525B] tracking-widest text-[9px]">PRODUCTION · </span>
                        {d.ml_state.production ? (
                            <span className="text-[#00FF41]">
                                ACTIVE {String(d.ml_state.production.digest || "").slice(0, 12)}…
                                {d.ml_state.production.n_trades != null && ` · ${d.ml_state.production.n_trades} trades`}
                            </span>
                        ) : <span className="text-[#71717A]">NONE</span>}
                    </span>
                    <span data-testid="ml-candidate-state">
                        <span className="text-[#52525B] tracking-widest text-[9px]">CANDIDATE · </span>
                        {d.ml_state.candidate ? (
                            <span className="text-[#FFD700]">
                                {String(d.ml_state.candidate.status || "").toUpperCase().replace(/_/g, " ")}{" "}
                                {String(d.ml_state.candidate.digest || "").slice(0, 12)}…
                                {` · ${(d.ml_state.candidate.approvals || []).length}/2 approvals`}
                            </span>
                        ) : <span className="text-[#71717A]">NONE PENDING</span>}
                    </span>
                </div>
            )}
            {ml && (
                <div className="mt-2 pt-2 border-t border-[#141414] font-mono text-[10px]" data-testid="learning-last-run">
                    <span className="text-[#52525B] tracking-widest text-[9px]">LAST GATED RETRAIN · </span>
                    <span style={{ color: statusColor(ml.status) }}>{(ml.status || "").toUpperCase()}</span>
                    {ml.candidate_auc != null && (
                        <span className="text-[#A1A1AA]">
                            {" "}— candidate AUC {ml.candidate_auc}
                            {ml.production_auc != null && ` vs production ${ml.production_auc}`}
                            {ml.holdout_n != null && ` on ${ml.holdout_n} unseen trades`}
                        </span>
                    )}
                    {ml.detail && <span className="text-[#71717A]"> · {ml.detail}</span>}
                </div>
            )}
            {Object.keys(d.shadow_lab || {}).length > 0 && (
                <div className="font-mono text-[9px] text-[#71717A] mt-1" data-testid="learning-shadow-lab">
                    SHADOW LAB: {Object.entries(d.shadow_lab).map(([k, v]) => `${v} ${k}`).join(" · ")}
                </div>
            )}
            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mt-2">
                MODELS NEVER LEARN DIRECTLY FROM LIVE TRADES — CANDIDATES MUST BEAT PRODUCTION ON UNSEEN DATA; LOSING STREAKS FREEZE THE GATE.
            </div>
        </div>
    );
};
