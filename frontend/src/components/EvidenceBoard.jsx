import { useEffect, useState } from "react";
import api from "@/lib/api";
import { FlaskConical } from "lucide-react";

const VERDICT = {
    proven:       { fg: "#00FF41", label: "PROVEN" },
    experimental: { fg: "#FFD700", label: "EXPERIMENTAL" },
    review:       { fg: "#FF3B30", label: "REVIEW" },
};

const NAME = {
    execution_timing: "Execution Timing",
    adaptive_exits: "Adaptive Exits",
    regime_gating: "Regime Gating",
    dynamic_allocation: "Dynamic Allocation",
    risk_layers: "Risk Layers",
    learning_pipeline: "Learning Pipeline",
};

export const EvidenceBoard = () => {
    const [d, setD] = useState(null);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        api.get("/performance/evidence")
            .then(({ data }) => { if (!dead) setD(data); })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !d) return null;

    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="evidence-board">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <FlaskConical className="w-4 h-4 text-[#FFD700]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">EVIDENCE-BASED DEVELOPMENT · LAST {d.window_days}D</div>
                        <div className="font-display font-bold text-lg tracking-tight">Feature Evidence Board</div>
                    </div>
                </div>
                <div className="flex items-center gap-2 font-mono text-[10px] tracking-widest" data-testid="evidence-summary">
                    <span className="text-[#00FF41]">{d.summary.proven} PROVEN</span>
                    <span className="text-[#FFD700]">{d.summary.experimental} EXPERIMENTAL</span>
                    {d.summary.review > 0 && <span className="text-[#FF3B30]">{d.summary.review} REVIEW</span>}
                </div>
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                {d.features.map(f => {
                    const v = VERDICT[f.verdict] || VERDICT.experimental;
                    return (
                        <div key={f.feature} className="px-5 py-3 flex items-start gap-3 flex-wrap"
                            data-testid={`evidence-${f.feature}`}>
                            <div className="min-w-[160px]">
                                <div className="font-display font-bold text-sm tracking-tight">{NAME[f.feature] || f.feature}</div>
                                <div className="font-mono text-[9px] text-[#52525B] tracking-widest mt-0.5">{f.question?.toUpperCase()}</div>
                            </div>
                            <div className="flex-1 min-w-[200px] text-xs text-[#A1A1AA] leading-relaxed">{f.metric}</div>
                            <span className="font-mono text-[9px] tracking-widest px-2 py-1 border"
                                style={{ color: v.fg, borderColor: `${v.fg}55` }}
                                data-testid={`evidence-verdict-${f.feature}`}>
                                {v.label} · N={f.n}
                            </span>
                        </div>
                    );
                })}
            </div>
            <div className="px-5 py-2.5 font-mono text-[9px] tracking-widest text-[#52525B] border-t border-[#1F1F1F]">
                {d.principle?.toUpperCase()}
            </div>
        </section>
    );
};
