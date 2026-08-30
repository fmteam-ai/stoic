import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Bomb, RefreshCw } from "lucide-react";

export function ChaosDrillsCard() {
    const [d, setD] = useState(null);
    const [running, setRunning] = useState(false);
    const [forbidden, setForbidden] = useState(false);

    const load = useCallback(() => {
        api.get("/ops/chaos").then(({ data }) => setD(data))
            .catch((e) => { if (e?.response?.status === 403) setForbidden(true); });
    }, []);
    useEffect(() => { load(); }, [load]);

    const run = async () => {
        setRunning(true);
        try {
            const { data } = await api.post("/ops/chaos/run");
            setD(data);
            toast.success(`Chaos drills: ${data.passed}/${data.total} passed`);
        } catch (e) {
            if (e?.response?.status === 403) { setForbidden(true); toast.error("Admin access required"); }
            else toast.error("Drill run failed");
        } finally { setRunning(false); }
    };

    if (forbidden) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="chaos-card">
            <div className="flex items-center gap-2 mb-1">
                <Bomb size={13} className="text-[#FF3B30]" />
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">CHAOS DRILLS · FAILURE-SCENARIO PROOFS</span>
                <button onClick={run} disabled={running} data-testid="chaos-run"
                    className="ml-auto flex items-center gap-1.5 font-mono text-[10px] tracking-widest px-2.5 py-1 border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 disabled:opacity-40">
                    <RefreshCw size={11} className={running ? "animate-spin" : ""} />
                    {running ? "RUNNING…" : "RUN DRILLS"}
                </button>
            </div>
            <div className="font-mono text-[9px] text-[#3F3F46] mb-2">
                Broker disconnects, volatility shocks, worker crashes, duplicate orders and DB durability — provoked with synthetic data, defences asserted. Feeds the Release Safety Score.
            </div>
            {d?.results?.length ? (
                <>
                    <div className="font-mono text-xs mb-2" data-testid="chaos-score">
                        <span className={d.passed === d.total ? "text-[#00FF41]" : "text-[#FFD700]"}>
                            {d.passed}/{d.total} PASSED{d.partial ? ` · ${d.partial} PARTIAL` : ""}
                        </span>
                        {d.partial ? (
                            <span className="text-[#FFB000]" title="A drill with skipped assertions is partial evidence, never a full pass"> (skipped assertions are not passes)</span>
                        ) : null}
                        <span className="text-[#52525B]"> · {String(d.at).slice(0, 16).replace("T", " ")} UTC</span>
                    </div>
                    {d.results.map((r) => (
                        <div key={r.drill} className="flex items-start gap-2 py-1 border-t border-[#141414]"
                            data-testid={`chaos-drill-${r.drill}`}>
                            <span className={`font-mono text-[9px] px-1.5 py-0.5 border shrink-0 ${r.passed ? "text-[#00FF41] border-[#00FF41]/40" : "text-[#FF3B30] border-[#FF3B30]/40"}`}>
                                {r.passed ? "PASS" : "FAIL"}
                            </span>
                            <div className="min-w-0">
                                <div className="font-mono text-[10px] text-[#A1A1AA]">{r.drill}</div>
                                <div className="font-mono text-[9px] text-[#52525B]">{r.detail}</div>
                            </div>
                        </div>
                    ))}
                </>
            ) : (
                <div className="font-mono text-xs text-[#52525B]" data-testid="chaos-empty">
                    No drills recorded yet — run the first campaign.
                </div>
            )}
        </div>
    );
}
