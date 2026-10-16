import { useCallback, useEffect, useState } from "react";
import { KeyRound, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const PROVIDER = {
    anthropic_direct: { label: "DIRECT ANTHROPIC", cls: "border-[#00FF41]/60 text-[#00FF41] bg-[#00FF41]/10" },
    emergent: { label: "EMERGENT UNIVERSAL KEY", cls: "border-[#FFB000]/60 text-[#FFB000] bg-[#FFB000]/10" },
    none: { label: "NO LLM KEY", cls: "border-[#FF3B30]/60 text-[#FF3B30] bg-[#FF3B30]/10" },
};

// v1.60.59 — which LLM provider is live, the last (redacted) error, today's per-feature call counters and whether
// the news veto can fire. Trading engines never depend on this card being green.
export function LlmProviderCard() {
    const [d, setD] = useState(null);
    const [loading, setLoading] = useState(false);
    const [hidden, setHidden] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try { setD((await api.get("/diagnostic/llm-status")).data); }
        catch (e) { setD(null); if ([401, 403].includes(e?.response?.status)) setHidden(true); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => {
        if (hidden) return undefined;
        load(); const t = setInterval(load, 60_000); return () => clearInterval(t);
    }, [load, hidden]);

    if (hidden) return null;
    const prov = PROVIDER[d?.provider] || PROVIDER.none;
    const blocked = d?.last_error_kind === "universal_key_blocked_offhost";
    const feats = Object.entries(d?.features_today || {}).sort((a, b) => b[1].calls - a[1].calls);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="llm-provider-card">
            <div className="flex flex-wrap items-center gap-2 mb-3">
                <KeyRound className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">LLM provider · cost</span>
                {d && <span className={`px-1.5 py-0.5 border font-mono text-[10px] tracking-widest ${prov.cls}`} data-testid="llm-provider-active">{prov.label}</span>}
                {d && (
                    <span className={`px-1.5 py-0.5 border font-mono text-[10px] tracking-widest ${d.news_veto?.active ? "border-[#00FF41]/60 text-[#00FF41]" : "border-[#FF3B30]/60 text-[#FF3B30]"}`}
                          data-testid="llm-news-veto-state">{d.news_veto?.active ? "NEWS VETO ACTIVE" : "NEWS VETO INACTIVE — LLM UNAVAILABLE"}</span>
                )}
                <button onClick={load} disabled={loading} className="ml-auto text-[#52525B] hover:text-white" data-testid="llm-provider-refresh"><RefreshCw className={`w-3 h-3 ${loading ? "animate-spin" : ""}`} /></button>
            </div>
            {!d ? (
                <div className="font-mono text-[10px] text-[#52525B]">{loading ? "loading…" : "LLM status unavailable"}</div>
            ) : (
                <div className="space-y-2 font-mono text-[10px]">
                    <div className="text-[#71717A]">
                        models: fast={d.models?.fast} · analysis={d.models?.analysis} · deep={d.models?.deep}
                        {" · "}max_tokens fast/analysis/deep {d.max_tokens?.fast}/{d.max_tokens?.analysis}/{d.max_tokens?.deep}
                    </div>
                    <div className="text-[#E4E4E7]" data-testid="llm-daily-calls">
                        today {d.today_calls}/{d.daily_call_cap} calls
                        {d.last_ok_at && <span className="text-[#71717A]"> · last OK {d.last_ok_at.slice(11, 19)}Z via {d.last_ok_provider}</span>}
                    </div>
                    {d.last_error && (
                        <div className={blocked ? "text-[#FF3B30]" : "text-[#FFB000]"} data-testid="llm-last-error">
                            {blocked ? "Universal Key not allowed from this host (Emergent proxy 403) — set ANTHROPIC_API_KEY under Admin → Integrations → AI"
                                     : `last error (${d.last_error_kind}): ${d.last_error}`}
                            <span className="text-[#52525B]"> · {d.last_error_at?.slice(11, 19)}Z</span>
                        </div>
                    )}
                    {feats.length > 0 && (
                        <table className="w-full" data-testid="llm-feature-counters">
                            <thead><tr className="text-[#52525B] tracking-widest text-left"><th className="py-1">FEATURE</th><th className="py-1 text-right">CALLS</th><th className="py-1 text-right">OK</th><th className="py-1 text-right">ERRORS</th><th className="py-1">PROVIDER</th></tr></thead>
                            <tbody>
                                {feats.map(([f, c]) => (
                                    <tr key={f} className="border-t border-[#1F1F1F]" data-testid={`llm-feature-row-${f}`}>
                                        <td className="py-1 text-white">{f}</td>
                                        <td className="py-1 text-right text-[#E4E4E7]">{c.calls}</td>
                                        <td className="py-1 text-right text-[#E4E4E7]">{c.ok}</td>
                                        <td className={`py-1 text-right ${c.errors ? "text-[#FF3B30]" : "text-[#E4E4E7]"}`}>{c.errors}</td>
                                        <td className="py-1 text-[#71717A]">{c.provider}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            )}
        </div>
    );
}

export default LlmProviderCard;
