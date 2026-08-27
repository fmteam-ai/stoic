import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { BrainCircuit } from "lucide-react";

const DIM_LABEL = {
    opportunity_quality: "OPPORTUNITY QUALITY",
    strategy_reliability: "STRATEGY RELIABILITY",
    regime_compatibility: "REGIME COMPATIBILITY",
    execution_quality: "EXECUTION QUALITY",
    risk_environment: "RISK ENVIRONMENT",
};
const DEC_COLOR = { TRADE: "#00FF41", REDUCE: "#FFB000", SKIP: "#FF3B30" };

function Bar({ label, value }) {
    return (
        <div className="flex items-center gap-2">
            <div className="font-mono text-[9px] text-[#A1A1AA] w-40 shrink-0">{label}</div>
            <div className="flex-1 h-1.5 bg-[#141414]">
                <div className="h-1.5" style={{
                    width: `${Math.min(100, value)}%`,
                    backgroundColor: value >= 70 ? "#00FF41" : value >= 40 ? "#FFB000" : "#FF3B30",
                }} />
            </div>
            <div className="font-mono text-[10px] text-white w-14 text-right">{Math.round(value)}/100</div>
        </div>
    );
}

export function MetaBrainPanel() {
    const [regime, setRegime] = useState(null);
    const [router, setRouter] = useState(null);
    const [recent, setRecent] = useState([]);
    const load = useCallback(async () => {
        try {
            const [r1, r2, r3] = await Promise.all([
                api.get("/brain/regime?symbol=XAUUSD"),
                api.get("/brain/router?symbol=XAUUSD"),
                api.get("/brain/meta/recent?limit=1"),
            ]);
            setRegime(r1.data); setRouter(r2.data);
            setRecent(r3.data?.decisions || []);
        } catch { /* non-fatal */ }
    }, []);
    useEffect(() => { load(); }, [load]);
    if (!regime) return null;
    const vec = regime.vector || {};
    const md = recent[0];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="meta-brain-panel">
            <div className="px-4 py-3 border-b border-[#141414] flex items-center gap-2">
                <BrainCircuit className="w-4 h-4 text-[#A855F7]" />
                <div className="font-display font-bold text-sm">Meta-Decision Brain</div>
                <span className="font-mono text-[10px] text-[#52525B]">regime 2.0 · strategy router · uncertainty · the AI above the strategies</span>
            </div>
            <div className="px-4 py-3 grid grid-cols-1 lg:grid-cols-3 gap-4">
                <div data-testid="brain-regime">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">MARKET STATE · XAUUSD</div>
                    {regime.available === false ? (
                        <div className="font-mono text-[10px] text-[#52525B]">{regime.note}</div>
                    ) : (
                        <>
                            <div className="flex flex-wrap gap-1 mb-2" data-testid="brain-fingerprint">
                                {(regime.labels || []).map(l => (
                                    <span key={l} className="font-mono text-[9px] px-1.5 py-0.5 border border-[#A855F7]/40 text-[#A855F7]">{l}</span>
                                ))}
                            </div>
                            <div className="font-mono text-[9px] text-[#A1A1AA] grid grid-cols-2 gap-x-3 gap-y-0.5">
                                {Object.entries(vec).map(([k, v]) => (
                                    <div key={k} className="flex justify-between">
                                        <span className="text-[#52525B]">{k.replace(/_/g, " ")}</span>
                                        <span style={{ color: v >= 0 ? "#E4E4E7" : "#FF8C00" }}>{v > 0 && k === "trend" ? "+" : ""}{v}</span>
                                    </div>
                                ))}
                            </div>
                        </>
                    )}
                </div>
                <div data-testid="brain-router">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">STRATEGY ROUTER</div>
                    {Object.entries(router?.weights || {}).map(([f, w]) => (
                        <div key={f} className="flex items-center gap-2 mb-1">
                            <div className="font-mono text-[9px] text-[#A1A1AA] w-20">{f.toUpperCase()}</div>
                            <div className="flex-1 h-1.5 bg-[#141414]">
                                <div className="h-1.5 bg-[#0099FF]" style={{ width: `${w * 100}%` }} />
                            </div>
                            <div className="font-mono text-[10px] text-white w-10 text-right">{(w * 100).toFixed(0)}%</div>
                        </div>
                    ))}
                    <div className="font-mono text-[8px] text-[#52525B] mt-1">
                        {router?.fingerprint_matched ? "learned from this exact regime" : "global evidence (regime history accumulating)"} · allocates only, never overrides risk
                    </div>
                </div>
                <div data-testid="brain-meta-latest">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">LATEST META DECISION</div>
                    {!md ? (
                        <div className="font-mono text-[10px] text-[#52525B]" data-testid="brain-meta-empty">
                            No meta decisions yet — every bot signal is now scored TRADE / REDUCE / SKIP before execution.
                        </div>
                    ) : (
                        <div className="space-y-1">
                            {Object.entries(md.scorecard || {}).map(([k, v]) => (
                                <Bar key={k} label={DIM_LABEL[k] || k} value={v} />
                            ))}
                            <div className="flex items-center gap-3 pt-1.5">
                                <span className="font-mono text-[10px] px-2 py-0.5 border font-bold"
                                    data-testid="brain-meta-decision"
                                    style={{ color: DEC_COLOR[md.decision], borderColor: `${DEC_COLOR[md.decision]}66` }}>
                                    {md.decision}
                                </span>
                                <span className="font-mono text-[10px] text-[#A1A1AA]">risk × {md.risk_multiplier}</span>
                                <span className="font-mono text-[10px] text-[#52525B]">uncertainty {Math.round((md.uncertainty || 0) * 100)}%</span>
                                <span className="font-mono text-[10px] text-white ml-auto">{md.composite}/100</span>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
