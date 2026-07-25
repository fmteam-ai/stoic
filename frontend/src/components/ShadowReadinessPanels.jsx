import { useEffect, useState } from "react";
import api from "@/lib/api";

const pnlCls = (v) => v > 0 ? "text-[#00FF41]" : v < 0 ? "text-[#FF3B30]" : "text-[#A1A1AA]";
const scoreCls = (v) => v == null ? "text-[#52525B]" : v >= 75 ? "text-[#00FF41]" : v >= 60 ? "text-[#FFD700]" : "text-[#FF3B30]";

export function ShadowHealthCard() {
    const [h, setH] = useState(null);
    useEffect(() => { api.get("/shadow/health").then(({ data }) => setH(data)).catch(() => {}); }, []);
    if (!h) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="shadow-health-card">
            <div className="flex items-center gap-3 mb-2">
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">SHADOW HEALTH · PROMOTION GATE</span>
                <span className={`font-mono text-2xl ${scoreCls(h.overall)}`} data-testid="shadow-health-overall">{h.overall ?? "—"}</span>
                <span className={`ml-auto font-mono text-[10px] px-2 py-0.5 border ${h.promotions_paused ? "text-[#FF3B30] border-[#FF3B30]/40" : "text-[#00FF41] border-[#00FF41]/40"}`}
                    data-testid="shadow-health-verdict">
                    {h.promotions_paused ? `PROMOTIONS PAUSED (< ${h.threshold})` : "PROMOTIONS OPEN"}
                </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                {Object.entries(h.components || {}).map(([k, v]) => (
                    <div key={k} className="border border-[#141414] p-2" data-testid={`shadow-health-${k}`}>
                        <div className="font-mono text-[8px] text-[#52525B] tracking-widest uppercase">{k.replace(/_/g, " ")}</div>
                        <div className={`font-mono text-sm ${scoreCls(v)}`}>{v ?? "n/a"}</div>
                    </div>
                ))}
            </div>
        </div>
    );
}

export function ShadowBenchmarkTable() {
    const [b, setB] = useState(null);
    useEffect(() => { api.get("/shadow/benchmark?days=30").then(({ data }) => setB(data)).catch(() => {}); }, []);
    if (!b) return null;
    const rows = Object.entries(b.variants || {});
    const q = b.decision_quality || {};
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="shadow-benchmark">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">CONTINUOUS BENCHMARK · SAME WINDOW, FOUR VARIANTS (30D)</div>
            <div className="overflow-x-auto">
                <table className="w-full font-mono text-[10px]">
                    <thead><tr className="text-[#52525B] text-left">
                        <th className="py-1 pr-2">VARIANT</th><th className="pr-2">N</th><th className="pr-2">EV(R)</th>
                        <th className="pr-2">WIN%</th><th className="pr-2">PF</th><th className="pr-2">MAX DD(R)</th><th>NET R</th>
                    </tr></thead>
                    <tbody>
                        {rows.map(([name, m]) => (
                            <tr key={name} className="border-t border-[#141414]" data-testid={`benchmark-${name}`}>
                                <td className="py-1.5 pr-2 text-[#A1A1AA]">{name.replace(/_/g, " ")}</td>
                                {m ? (<>
                                    <td className="pr-2 text-[#52525B]">{m.n}</td>
                                    <td className={`pr-2 ${pnlCls(m.ev_r)}`}>{m.ev_r >= 0 ? "+" : ""}{m.ev_r}</td>
                                    <td className="pr-2 text-white">{m.win_rate}%</td>
                                    <td className="pr-2 text-[#0099FF]">{m.profit_factor ?? "∞"}</td>
                                    <td className="pr-2 text-[#FF3B30]">{m.max_drawdown_r}</td>
                                    <td className={pnlCls(m.net_r)}>{m.net_r >= 0 ? "+" : ""}{m.net_r}</td>
                                </>) : <td colSpan={6} className="text-[#3F3F46]">no data in window</td>}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
            <div className="font-mono text-[9px] text-[#52525B] mt-2" data-testid="benchmark-quality">
                Decision policy: {q.false_positive_rate ?? "—"}% false positives (taken &amp; lost) · {q.false_negative_rate ?? "—"}% false negatives (skipped would-be winners)
            </div>
        </div>
    );
}

export function TwinStressPanel() {
    const [s, setS] = useState(null);
    useEffect(() => { api.get("/twin/stress?days=30").then(({ data }) => setS(data)).catch(() => {}); }, []);
    if (!s) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="twin-stress-panel">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                TWIN STRESS LAB · {s.decisions} DECISIONS · CLEAN <span className={pnlCls(s.clean_net_r)}>{s.clean_net_r >= 0 ? "+" : ""}{s.clean_net_r}R</span>
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-2">{s.note}</div>
            {(s.scenarios || []).map((sc) => (
                <div key={sc.scenario} className="flex items-center gap-2 py-1 border-t border-[#141414]" data-testid={`stress-${sc.scenario}`}>
                    <span className={`font-mono text-[9px] px-1.5 py-0.5 border shrink-0 ${sc.verdict === "RESILIENT" ? "text-[#00FF41] border-[#00FF41]/40" : "text-[#FF3B30] border-[#FF3B30]/40"}`}>
                        {sc.verdict}
                    </span>
                    <span className="font-mono text-[10px] text-[#A1A1AA] w-36 shrink-0">{sc.scenario.replace(/_/g, " ")}</span>
                    <span className="font-mono text-[9px] text-[#52525B] truncate">{sc.description}</span>
                    <span className={`ml-auto font-mono text-[10px] shrink-0 ${pnlCls(sc.net_r)}`}>{sc.net_r >= 0 ? "+" : ""}{sc.net_r}R <span className="text-[#52525B]">(Δ{sc.delta_r})</span></span>
                </div>
            ))}
        </div>
    );
}

export function ValidationQuorumCard() {
    const [v, setV] = useState(null);
    useEffect(() => { api.get("/shadow/validation").then(({ data }) => setV(data)).catch(() => {}); }, []);
    if (!v) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="validation-quorum-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                DECISION VALIDATION · FOUR-VERDICT QUORUM
                <span className={`ml-2 ${scoreCls(v.agreement_rate)}`} data-testid="quorum-agreement">{v.agreement_rate != null ? `${v.agreement_rate}% AGREEMENT` : "AWAITING EXECUTIONS"}</span>
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-2">
                AI decision → deterministic → risk → execution. Every executed trade records all four verdicts; a healthy system agrees ~100%.
            </div>
            {(v.recent || []).slice(0, 5).map((r, i) => (
                <div key={i} className="flex items-center gap-2 py-1 border-t border-[#141414]" data-testid={`quorum-row-${i}`}>
                    <span className="font-mono text-[10px] text-[#A1A1AA] w-20">{r.symbol}</span>
                    {Object.entries(r.verdicts || {}).map(([k, verdict]) => (
                        <span key={k} className={`font-mono text-[8px] px-1 py-0.5 border ${verdict === "approve" ? "text-[#00FF41] border-[#00FF41]/30" : "text-[#FF3B30] border-[#FF3B30]/40"}`}>
                            {k.toUpperCase().slice(0, 4)}
                        </span>
                    ))}
                    <span className="ml-auto font-mono text-[9px] text-[#3F3F46]">{String(r.ts).slice(5, 16).replace("T", " ")}</span>
                </div>
            ))}
            {v.total === 0 && <div className="font-mono text-[9px] text-[#52525B]" data-testid="quorum-empty">Quorums are stamped on every new executed trade from now on.</div>}
        </div>
    );
}
