import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { TrendingDown, Play, Loader2 } from "lucide-react";

export const StressTestCard = () => {
    const [data, setData] = useState(null);
    const [severity, setSeverity] = useState("moderate");
    const [running, setRunning] = useState(false);

    const load = useCallback(async () => {
        try { setData((await api.get("/stress-test/runs")).data); }
        catch { setData({ runs: [] }); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const run = async () => {
        setRunning(true);
        try {
            const { data: r } = await api.post(`/stress-test/run?severity=${severity}`);
            toast[r.verdict === "STAYED_CALM" ? "success" : "error"](
                r.verdict === "STAYED_CALM"
                    ? `Your bot stayed calm through a ${r.params.drop_pct}% crash`
                    : "A defense layer failed under pressure — see details");
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setRunning(false); }
    };

    const last = data?.runs?.[0];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid="user-stress-test-card">
            <div className="flex items-center gap-2 mb-1">
                <TrendingDown className="w-4 h-4 text-[#FF3B30]" />
                <h3 className="font-display text-sm tracking-widest text-white">STRESS TEST — FLASH CRASH</h3>
            </div>
            <p className="text-xs text-[#71717A] mb-3">
                Simulate a sudden market drop against <span className="text-[#A1A1AA]">your</span> risk
                profile and equity — see whether your bot stays calm under pressure.
            </p>
            <div className="flex items-center gap-2 mb-3">
                <select value={severity} onChange={e => setSeverity(e.target.value)}
                    data-testid="user-stress-severity"
                    className="bg-[#050505] border border-[#1F1F1F] text-xs text-[#A1A1AA] px-2 py-1.5 font-mono flex-1">
                    <option value="mild">MILD — 3% drop</option>
                    <option value="moderate">MODERATE — 8% drop</option>
                    <option value="severe">SEVERE — 15% drop</option>
                </select>
                <button onClick={run} disabled={running} data-testid="user-stress-run-btn"
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 flex items-center gap-1.5">
                    {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                    {running ? "CRASHING…" : "RUN"}
                </button>
            </div>
            {!last && <div className="text-xs text-[#52525B]" data-testid="user-stress-empty">No stress tests yet — run your first crash simulation.</div>}
            {last && (
                <div data-testid="user-stress-results">
                    <div className={`text-sm font-display mb-0.5 ${last.verdict === "STAYED_CALM" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                        data-testid="user-stress-verdict">
                        {last.verdict === "STAYED_CALM" ? "✓ STAYED CALM" : "✗ PANICKED"}
                        <span className="text-xs text-[#52525B] font-mono ml-2">
                            {last.severity} · {last.params?.drop_pct}% drop · {new Date(last.at).toLocaleString()}
                        </span>
                    </div>
                    {last.params?.profile_note && (
                        <div className="text-[10px] text-[#52525B] font-mono mb-1.5">{last.params.profile_note}</div>
                    )}
                    {last.checks.map(c => (
                        <div key={c.layer} className="flex items-start gap-2 py-0.5 text-xs" title={c.detail}>
                            <span className={`shrink-0 font-mono ${c.status === "pass" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                {c.status === "pass" ? "CALM" : "FAIL"}
                            </span>
                            <span className="text-[#A1A1AA]">{c.layer.replaceAll("_", " ")}</span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};
