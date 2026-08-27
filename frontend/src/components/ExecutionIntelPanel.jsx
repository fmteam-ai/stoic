import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { Crosshair, Network, FlaskConical } from "lucide-react";

const MODE_COLOR = {
    EXECUTE_NOW: "#00FF41", WAIT: "#FFB000", REDUCE: "#FF8C00",
    SKIP: "#FF3B30", TRADE: "#00FF41",
};

function scoreColor(s) {
    return s >= 85 ? "#00FF41" : s >= 70 ? "#FFB000" : s >= 55 ? "#FF8C00" : "#FF3B30";
}

function Section({ icon: Icon, title, children, testId }) {
    return (
        <div data-testid={testId}>
            <div className="flex items-center gap-1.5 mb-1.5">
                <Icon className="w-3 h-3 text-[#00E5FF]" />
                <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{title}</div>
            </div>
            {children}
        </div>
    );
}

export function ExecutionIntelPanel() {
    const [matrix, setMatrix] = useState(null);
    const [alpha, setAlpha] = useState([]);
    const [twin, setTwin] = useState([]);
    const load = useCallback(async () => {
        try {
            const [r1, r2, r3] = await Promise.all([
                api.get("/brain/broker-matrix?days=30"),
                api.get("/brain/execution-alpha/recent?limit=6"),
                api.get("/brain/twin/recent?limit=6"),
            ]);
            setMatrix(r1.data); setAlpha(r2.data?.decisions || []); setTwin(r3.data?.verdicts || []);
        } catch { /* non-fatal */ }
    }, []);
    useEffect(() => { load(); }, [load]);
    if (!matrix) return null;
    const cells = (matrix.cells || []).slice(0, 6);
    const ranking = matrix.broker_ranking || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="execution-intel-panel">
            <div className="px-4 py-3 border-b border-[#141414] flex items-center gap-2">
                <Crosshair className="w-4 h-4 text-[#00E5FF]" />
                <div className="font-display font-bold text-sm">Execution Intelligence</div>
                <span className="font-mono text-[10px] text-[#52525B]">broker matrix · execution alpha · pre-trade twin</span>
            </div>
            <div className="px-4 py-3 grid grid-cols-1 lg:grid-cols-3 gap-4">
                <Section icon={Network} title="BROKER EXECUTION MATRIX · 30D" testId="broker-matrix-panel">
                    {ranking.length === 0 ? (
                        <div className="font-mono text-[10px] text-[#52525B]">no measured fills yet — scores build as trades execute</div>
                    ) : (
                        <>
                            {ranking.slice(0, 3).map(b => (
                                <div key={b.broker} className="flex items-center gap-2 py-0.5">
                                    <span className="font-mono text-[9px] text-[#A1A1AA] w-28 truncate">{b.broker}</span>
                                    <span className="font-mono text-[10px] font-bold" style={{ color: scoreColor(b.execution_score) }}>{b.execution_score}/100</span>
                                    <span className="font-mono text-[8px] text-[#52525B]">{b.submissions} orders</span>
                                </div>
                            ))}
                            <div className="mt-1 space-y-0.5">
                                {cells.map((c, i) => (
                                    <div key={i} className="flex gap-2 font-mono text-[8px] text-[#52525B]">
                                        <span className="w-16 truncate">{c.symbol}</span>
                                        <span className="w-14">{c.session}</span>
                                        <span style={{ color: scoreColor(c.score) }}>{c.score}</span>
                                        {c.avg_slippage_pips != null && <span>slip {c.avg_slippage_pips}p</span>}
                                        {c.latency_p50_ms != null && <span>{c.latency_p50_ms}ms</span>}
                                    </div>
                                ))}
                            </div>
                        </>
                    )}
                </Section>
                <Section icon={Crosshair} title="EXECUTION ALPHA · RECENT PLANS" testId="execution-alpha-panel">
                    {alpha.length === 0 ? (
                        <div className="font-mono text-[10px] text-[#52525B]">no execution plans yet — decided per order at send time</div>
                    ) : alpha.map((d, i) => (
                        <div key={i} className="py-0.5 border-b border-[#141414] last:border-0">
                            <div className="flex items-center gap-2">
                                <span className="font-mono text-[9px] px-1 border" style={{ color: MODE_COLOR[d.mode] || "#A1A1AA", borderColor: `${MODE_COLOR[d.mode] || "#A1A1AA"}40` }}>{d.mode}</span>
                                <span className="font-mono text-[9px] text-[#A1A1AA]">{d.symbol}</span>
                                {(d.advisory || []).map((a, j) => (
                                    <span key={j} className="font-mono text-[8px] text-[#00E5FF]">+{a.recommendation}</span>
                                ))}
                            </div>
                            <div className="font-mono text-[8px] text-[#52525B] truncate">{(d.reasons || [])[0]}</div>
                        </div>
                    ))}
                </Section>
                <Section icon={FlaskConical} title="PRE-TRADE DIGITAL TWIN" testId="pretrade-twin-panel">
                    {twin.length === 0 ? (
                        <div className="font-mono text-[10px] text-[#52525B]">no simulations yet — every candidate trade is twinned before capital commits</div>
                    ) : twin.map((t, i) => (
                        <div key={i} className="flex items-center gap-2 py-0.5">
                            <span className="font-mono text-[9px] px-1 border" style={{ color: MODE_COLOR[t.verdict] || "#FF8C00", borderColor: `${MODE_COLOR[t.verdict] || "#FF8C00"}40` }}>{t.verdict}</span>
                            <span className="font-mono text-[9px] text-[#A1A1AA]">{t.symbol}</span>
                            <span className="font-mono text-[8px] text-[#52525B]">{t.mode} · ${t.risk_usd} risk · {t.elapsed_ms}ms</span>
                            {t.approved_fraction < 1 && <span className="font-mono text-[8px] text-[#FF8C00]">×{t.approved_fraction}</span>}
                        </div>
                    ))}
                </Section>
            </div>
        </div>
    );
}
