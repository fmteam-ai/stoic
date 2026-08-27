import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { Database, HeartPulse, ShieldAlert, Receipt, GitBranch } from "lucide-react";

const STATE_COLOR = {
    HEALTHY: "#00FF41", WATCH: "#FFB000", DEGRADED: "#FF8C00",
    DECAYING: "#FF3B30", DISABLED: "#FF3B30",
};

function Section({ icon: Icon, title, children, testId }) {
    return (
        <div data-testid={testId}>
            <div className="flex items-center gap-1.5 mb-1.5">
                <Icon className="w-3 h-3 text-[#A855F7]" />
                <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{title}</div>
            </div>
            {children}
        </div>
    );
}

function DecisionRow({ d }) {
    const [open, setOpen] = useState(false);
    const [full, setFull] = useState(null);
    const toggle = async () => {
        if (!open && !full) {
            try { const r = await api.get(`/brain/decisions/${d.decision_id}`); setFull(r.data); } catch { /* ignore */ }
        }
        setOpen(!open);
    };
    return (
        <div className="border-b border-[#141414] last:border-0">
            <button onClick={toggle} data-testid={`decision-row-${d.decision_id}`}
                className="w-full text-left py-1 flex items-center gap-2 hover:bg-[#141414] px-1">
                <span className="font-mono text-[9px] text-[#A855F7]">{d.decision_id}</span>
                <span className="font-mono text-[9px] text-[#A1A1AA]">{d.symbol}</span>
                <span className="font-mono text-[9px] text-[#52525B]">{d.scope}</span>
                <span className="font-mono text-[8px] text-[#52525B] ml-auto">{String(d.at || "").slice(5, 16).replace("T", " ")}</span>
            </button>
            {open && full && (
                <div className="px-2 pb-2 font-mono text-[9px] text-[#A1A1AA] space-y-0.5" data-testid="decision-detail">
                    {(full.events || full.stages || []).map((s, i) => (
                        <div key={i} className="flex gap-2">
                            <span className="text-[#00FF41] w-28 shrink-0">{s.stage}</span>
                            <span className="text-[#52525B] truncate">{JSON.stringify(s.detail).slice(0, 90)}</span>
                        </div>
                    ))}
                    {full.trade && <div className="text-[#FFB000]">→ trade {full.trade.status} · pnl {full.trade.pnl ?? "—"}</div>}
                    {!(full.events || full.stages || []).length && <div className="text-[#52525B]">no events recorded yet</div>}
                </div>
            )}
        </div>
    );
}

export function BrainPhaseBPanel() {
    const [health, setHealth] = useState(null);
    const [memory, setMemory] = useState(null);
    const [costs, setCosts] = useState(null);
    const [degraded, setDegraded] = useState(null);
    const [decisions, setDecisions] = useState([]);
    const load = useCallback(async () => {
        try {
            const [r1, r2, r3, r4, r5] = await Promise.all([
                api.get("/brain/strategy-health"),
                api.get("/brain/memory?symbol=XAUUSD"),
                api.get("/brain/costs?symbol=XAUUSD"),
                api.get("/brain/degraded"),
                api.get("/brain/decisions?limit=5"),
            ]);
            setHealth(r1.data?.strategies || []);
            setMemory(r2.data); setCosts(r3.data);
            setDegraded(r4.data); setDecisions(r5.data?.decisions || []);
        } catch { /* non-fatal */ }
    }, []);
    useEffect(() => { load(); }, [load]);
    if (!degraded) return null;
    const mem = memory?.memory || {};
    const dist = mem.distribution || {};
    const failing = degraded.failing || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="brain-phase-b-panel">
            <div className="px-4 py-3 border-b border-[#141414] flex items-center gap-2 flex-wrap">
                <Database className="w-4 h-4 text-[#A855F7]" />
                <div className="font-display font-bold text-sm">Brain Phase B</div>
                <span className="font-mono text-[10px] text-[#52525B]">decision context · market memory · decay · dynamic costs</span>
                <span data-testid="degraded-mode-chip" className="ml-auto font-mono text-[9px] px-1.5 py-0.5 border"
                    style={{ color: degraded.mode === "NORMAL" ? "#00FF41" : "#FF3B30", borderColor: degraded.mode === "NORMAL" ? "#00FF4140" : "#FF3B3040" }}>
                    {degraded.mode}{failing.length > 0 && ` · ${failing.join(", ")}`}
                </span>
            </div>
            <div className="px-4 py-3 grid grid-cols-1 lg:grid-cols-4 gap-4">
                <Section icon={HeartPulse} title="STRATEGY HEALTH" testId="strategy-health-board">
                    {(health || []).length === 0 ? (
                        <div className="font-mono text-[10px] text-[#52525B]">no closed-trade history yet</div>
                    ) : (health || []).slice(0, 5).map(h => (
                        <div key={h.scope} className="flex items-center gap-2 py-0.5">
                            <span className="font-mono text-[9px] text-[#A1A1AA] w-20 truncate">{h.scope}</span>
                            <span className="font-mono text-[9px] px-1 border" style={{ color: STATE_COLOR[h.state], borderColor: `${STATE_COLOR[h.state]}40` }}>{h.state}</span>
                            <span className="font-mono text-[9px] text-[#52525B]">{h.unproven ? `${h.n_base} trades` : `${h.metrics?.expectancy_recent}R`}</span>
                        </div>
                    ))}
                </Section>
                <Section icon={GitBranch} title="MARKET MEMORY · XAUUSD" testId="market-memory-panel">
                    {!mem.available ? (
                        <div className="font-mono text-[10px] text-[#52525B]">{mem.note || "building memory…"}</div>
                    ) : (
                        <div className="font-mono text-[9px] text-[#A1A1AA] space-y-0.5">
                            <div>similar situations <span className="text-white">{mem.n}</span> · confidence <span className="text-white">{Math.round((mem.confidence || 0) * 100)}%</span></div>
                            <div>continuation <span className="text-[#00FF41]">{Math.round((dist.continuation || 0) * 100)}%</span> · reversal <span className="text-[#FF3B30]">{Math.round((dist.reversal || 0) * 100)}%</span> · neutral {Math.round((dist.neutral || 0) * 100)}%</div>
                            <div>median <span style={{ color: mem.median_r >= 0 ? "#00FF41" : "#FF3B30" }}>{mem.median_r}R</span> · verdict <span className="text-[#A855F7]">{memory?.verdict?.action}</span></div>
                        </div>
                    )}
                </Section>
                <Section icon={Receipt} title="DYNAMIC COST MODEL · XAUUSD" testId="cost-model-panel">
                    {costs && (
                        <div className="font-mono text-[9px] text-[#A1A1AA] space-y-0.5">
                            <div>expected cost <span className="text-white">{costs.cost_r}R</span> · required edge <span className="text-[#FFB000]">{costs.required_edge_r}R</span></div>
                            {Object.entries(costs.components || {}).map(([k, v]) => (
                                <div key={k} className="flex justify-between"><span className="text-[#52525B]">{k.replace(/_/g, " ")}</span><span>{v}</span></div>
                            ))}
                        </div>
                    )}
                </Section>
                <Section icon={ShieldAlert} title="RECENT DECISIONS" testId="decision-inspector">
                    {decisions.length === 0 ? (
                        <div className="font-mono text-[10px] text-[#52525B]">no decision contexts yet — minted on the next BUY/SELL opportunity</div>
                    ) : decisions.map(d => <DecisionRow key={d.decision_id} d={d} />)}
                </Section>
            </div>
        </div>
    );
}
