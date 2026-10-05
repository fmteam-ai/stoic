import { useCallback, useEffect, useState } from "react";
import { Cpu, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const VERDICT = {
    failing: { label: "FAILING", cls: "border-[#FF3B30]/60 text-[#FF3B30] bg-[#FF3B30]/10" },
    slow: { label: "SLOW", cls: "border-[#FFB000]/60 text-[#FFB000] bg-[#FFB000]/10" },
    healthy: { label: "HEALTHY", cls: "border-[#00FF41]/60 text-[#00FF41] bg-[#00FF41]/10" },
    insufficient_data: { label: "FEW CALLS", cls: "border-[#1F1F1F] text-[#71717A]" },
};

// AI latency card — every LLM call from the trading loop (narration, sentiment, bot
// doctor, optimizer…) is sampled; slow or failing providers show up here before they
// cost signals (timeouts fall back to neutral verdicts).
export function AiLatencyCard() {
    const [hours, setHours] = useState(24);
    const [d, setD] = useState(null);
    const [loading, setLoading] = useState(false);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try { setD((await api.get(`/diagnostic/ai-latency?hours=${hours}`)).data); }
        catch (e) { setD(null); if ([401, 403].includes(e?.response?.status)) setHidden(true); }   // admin-only
        finally { setLoading(false); }
    }, [hours]);

    useEffect(() => { load(); const t = setInterval(load, 60_000); return () => clearInterval(t); }, [load]);

    const overall = VERDICT[d?.overall] || null;
    if (hidden) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="ai-latency-card">
            <div className="flex flex-wrap items-center gap-2 mb-3">
                <Cpu className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">AI latency · providers</span>
                {overall && <span className={`px-1.5 py-0.5 border font-mono text-[10px] tracking-widest ${overall.cls}`} data-testid="ai-latency-overall">{overall.label}</span>}
                <div className="ml-auto flex items-center gap-2">
                    {[1, 24, 168].map((h) => (
                        <button key={h} onClick={() => setHours(h)} data-testid={`ai-latency-hours-${h}`}
                                className={`px-2 py-0.5 font-mono text-[10px] border ${hours === h ? "border-[#00FF41]/60 text-[#00FF41]" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>{h === 168 ? "7D" : `${h}H`}</button>
                    ))}
                    <button onClick={load} disabled={loading} className="text-[#52525B] hover:text-white" data-testid="ai-latency-refresh"><RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /></button>
                </div>
            </div>
            {!d ? (
                <div className="font-mono text-[10px] text-[#52525B]">{loading ? "loading…" : "latency data unavailable"}</div>
            ) : !d.providers.length ? (
                <div className="font-mono text-[10px] text-[#52525B]" data-testid="ai-latency-empty">no AI calls recorded in the last {hours} h — samples appear once the bot narrates a signal or scores news</div>
            ) : (
                <table className="w-full font-mono text-[10px]">
                    <thead>
                        <tr className="text-[#52525B] tracking-widest text-left">
                            <th className="py-1">PROVIDER · MODEL</th><th className="py-1 text-right">CALLS</th><th className="py-1 text-right">P50</th><th className="py-1 text-right">P95</th>
                            <th className="py-1 text-right">MAX</th><th className="py-1 text-right">TIMEOUTS</th><th className="py-1 text-right">ERRORS</th><th className="py-1">USED BY</th><th className="py-1">VERDICT</th>
                        </tr>
                    </thead>
                    <tbody>
                        {d.providers.map((p) => {
                            const v = VERDICT[p.verdict] || VERDICT.insufficient_data;
                            return (
                                <tr key={`${p.provider}/${p.model}`} className="border-t border-[#1F1F1F]" data-testid={`ai-latency-row-${p.provider}-${p.model}`}>
                                    <td className="py-1.5 text-white">{p.provider} <span className="text-[#71717A]">· {p.model}</span></td>
                                    <td className="py-1.5 text-right text-[#E4E4E7]">{p.calls}</td>
                                    <td className="py-1.5 text-right text-[#E4E4E7]">{(p.p50_ms / 1000).toFixed(1)}s</td>
                                    <td className={`py-1.5 text-right ${p.verdict === "slow" ? "text-[#FFB000]" : "text-[#E4E4E7]"}`}>{(p.p95_ms / 1000).toFixed(1)}s</td>
                                    <td className="py-1.5 text-right text-[#E4E4E7]">{(p.max_ms / 1000).toFixed(1)}s</td>
                                    <td className={`py-1.5 text-right ${p.timeout ? "text-[#FF3B30]" : "text-[#E4E4E7]"}`}>{p.timeout}</td>
                                    <td className={`py-1.5 text-right ${p.error ? "text-[#FF3B30]" : "text-[#E4E4E7]"}`}>{p.error}</td>
                                    <td className="py-1.5 text-[#71717A]">{Object.entries(p.labels).map(([k, n]) => `${k}×${n}`).join(" ")}</td>
                                    <td className="py-1.5"><span className={`px-1.5 py-0.5 border tracking-widest ${v.cls}`} title={p.why} data-testid={`ai-latency-verdict-${p.provider}-${p.model}`}>{v.label}</span></td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            )}
            {d && <div className="mt-2 font-mono text-[10px] text-[#52525B]">hard timeout {d.timeout_s}s per call · SLOW when p95 ≥ {Math.round(d.slow_p95_ratio * 100)}% of it · FAILING at ≥20% timeouts/errors · {d.samples} samples</div>}
        </div>
    );
}

export default AiLatencyCard;
