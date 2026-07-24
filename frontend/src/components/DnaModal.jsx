import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Dna, X, BadgeCheck } from "lucide-react";

const Row = ({ label, children }) => (
    <div className="flex items-start gap-3 py-1.5 border-b border-[#141414]">
        <span className="font-mono text-[9px] tracking-widest text-[#52525B] w-40 shrink-0 pt-0.5">{label}</span>
        <span className="text-xs text-[#E4E4E7] flex-1 break-words">{children ?? "—"}</span>
    </div>
);

const fmt = (v) => v === null || v === undefined ? "—"
    : typeof v === "object" ? JSON.stringify(v) : String(v);

export const DnaModal = ({ trade, onClose }) => {
    const [d, setD] = useState(null);
    const [err, setErr] = useState("");

    useEffect(() => {
        api.get(`/trades/${trade.id || trade._id}/dna`)
            .then(({ data }) => setD(data))
            .catch(e => setErr(e?.response?.data?.detail || "Failed to load DNA"));
    }, [trade]);

    return (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#1F1F1F] w-full max-w-3xl max-h-[85vh] overflow-y-auto"
                onClick={e => e.stopPropagation()} data-testid="dna-modal">
                <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2 sticky top-0 bg-[#0A0A0A]">
                    <Dna className="w-4 h-4 text-[#00FF41]" />
                    <span className="font-display font-bold">Decision DNA · {trade.symbol} {trade.action}</span>
                    <button onClick={onClose} className="ml-auto text-[#52525B] hover:text-white" data-testid="dna-close">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                {err && <div className="p-5 text-xs text-[#FF3B30] font-mono">{fmt(err)}</div>}
                {!d && !err && <div className="p-5 text-xs text-[#52525B] font-mono tracking-widest">COMPOSING DNA…</div>}
                {d && (
                    <div className="p-5">
                        <div className="border border-[#00FF41]/30 bg-[#00FF41]/5 p-3 mb-4 space-y-1.5" data-testid="dna-four-questions">
                            <div className="font-mono text-[9px] tracking-widest text-[#00FF41]">THE FOUR QUESTIONS</div>
                            <div className="text-xs"><span className="text-[#52525B]">Why:</span> {d.four_questions.why_decided}</div>
                            <div className="text-xs"><span className="text-[#52525B]">Evidence:</span> {fmt(d.four_questions.evidence)}</div>
                            <div className="text-xs"><span className="text-[#52525B]">Risks considered:</span> {fmt(d.four_questions.risks_considered)}</div>
                            <div className="text-xs"><span className="text-[#52525B]">Outcome vs expectation:</span> expected {fmt(d.four_questions.outcome_vs_expectation.expected_r)}R → realized {fmt(d.four_questions.outcome_vs_expectation.realized_r)}R · <span className="text-[#FFD700]">{d.four_questions.outcome_vs_expectation.verdict?.toUpperCase()}</span></div>
                        </div>
                        <Row label="MARKET REGIME">{fmt(d.market_regime?.key || d.market_regime)}</Row>
                        <Row label="TREND STRENGTH">{d.trend_strength?.score != null ? `${d.trend_strength.score}/100 ${d.trend_strength.direction || ""}` : "—"}</Row>
                        <Row label="VOLATILITY SCORE">{fmt(d.volatility_score)}</Row>
                        <Row label="LIQUIDITY SCORE">{fmt(d.liquidity_score)}</Row>
                        <Row label="NEWS SCORE">{fmt(d.news_score?.net)}</Row>
                        <Row label="CONFIDENCE">{d.confidence?.raw != null ? `${d.confidence.raw}% raw · ${fmt(d.confidence.calibrated)}% calibrated · ${fmt(d.confidence.uncertainty_tier)}` : "—"}</Row>
                        <Row label="EXPECTED VALUE">{d.expected_value_r != null ? `${d.expected_value_r}R` : "—"}</Row>
                        <Row label="RISK BUDGET">{d.risk_budget?.risk_pct != null ? `${d.risk_budget.risk_pct}% · ${fmt(d.risk_budget.lot_size)} lots · ${fmt(d.risk_budget.sizing_method)}` : "—"}</Row>
                        <Row label="STRATEGY VERSION">{fmt(d.strategy_version)}</Row>
                        <Row label="AI OPINION">{fmt(d.ai_opinion)}</Row>
                        <Row label="DETERMINISTIC OPINION">{`consensus ${fmt(d.deterministic_opinion?.consensus_score)} · ${fmt(d.deterministic_opinion?.engine)}`}</Row>
                        <Row label="EXECUTION DELAY">{d.execution_delay_ms != null ? `${d.execution_delay_ms}ms` : "—"}</Row>
                        <Row label="BROKER QUALITY">{d.broker_quality?.score != null ? `${d.broker_quality.score}/100` : "—"}</Row>
                        <Row label="EXIT LOGIC">{`SL ${fmt(d.exit_logic?.stop_loss)} · TP ${fmt(d.exit_logic?.take_profit)} · ${fmt(d.exit_logic?.close_reason)}`}</Row>
                        <Row label="LEARNING RECORD">{`MFE ${fmt(d.learning_record?.mfe_r)}R · MAE ${fmt(d.learning_record?.mae_r)}R · ${fmt(d.learning_record?.failure?.category)}`}</Row>
                        {d.attestation && (
                            <div className="flex items-center gap-2 mt-3 font-mono text-[9px] tracking-widest text-[#0099FF]" data-testid="dna-attestation">
                                <BadgeCheck className="w-3.5 h-3.5" />
                                SIGNED {d.attestation.key_id} · {d.attestation.payload_hash?.slice(0, 24)}…
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
};
