import { useEffect, useState } from "react";
import { AlertOctagon, ChevronDown, LifeBuoy, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { UnblockTour } from "@/components/UnblockTour";

/**
 * TradingReadinessStrip (audit F-02) — the DOMINANT status layer.
 * Answers one question: is the system safe and authorized to trade?
 * Infrastructure uptime lives in the secondary StatusBar below.
 */
const LEVEL_STYLE = {
    READY: { text: "text-[#00FF41]", border: "border-[#00FF41]/30", bg: "bg-[#00FF41]/5" },
    DEGRADED: { text: "text-[#FFD700]", border: "border-[#FFD700]/40", bg: "bg-[#FFD700]/5" },
    CLOSE_ONLY: { text: "text-[#FFB020]", border: "border-[#FFB020]/50", bg: "bg-[#FFB020]/10" },
    BLOCKED: { text: "text-[#FF3B30]", border: "border-[#FF3B30]/50", bg: "bg-[#FF3B30]/10" },
    EMERGENCY: { text: "text-[#FF3B30]", border: "border-[#FF3B30]", bg: "bg-[#FF3B30]/15" },
};

const ago = (iso) => {
    if (!iso) return "";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
    return `${(s / 86400).toFixed(1)}d`;
};

export function TradingReadinessStrip() {
    const [data, setData] = useState(null);
    const [openList, setOpenList] = useState(false);
    const [trusting, setTrusting] = useState(null);
    const [tour, setTour] = useState(null);

    const trustTerminal = async (accountId) => {
        setTrusting(accountId);
        try {
            await api.post(`/accounts/${accountId}/trust-terminal`);
            toast.success("Terminal trusted — full trading authority restores within a minute");
            const r = await api.get("/state/readiness");
            setData(r.data);
        } catch (e) {
            toast.error("Could not trust terminal", { description: formatApiError(e) });
        } finally {
            setTrusting(null);
        }
    };

    useEffect(() => {
        let alive = true;
        const load = () =>
            api.get("/state/readiness")
                .then(r => { if (alive) setData(r.data); })
                .catch(() => {});
        load();
        const t = setInterval(load, 30000);
        return () => { alive = false; clearInterval(t); };
    }, []);

    if (!data) return null;
    const st = LEVEL_STYLE[data.level] || LEVEL_STYLE.DEGRADED;
    const top = (data.reasons || [])[0];
    const calm = data.level === "READY";

    return (
        <div className={`border-b ${st.border} ${st.bg}`} data-testid="trading-readiness-strip">
            <div className="px-4 md:px-6 py-1.5 flex items-center justify-between gap-3">
                <div className="flex items-center gap-3 min-w-0">
                    {calm
                        ? <ShieldCheck className={`w-3.5 h-3.5 shrink-0 ${st.text}`} />
                        : <AlertOctagon className={`w-3.5 h-3.5 shrink-0 ${st.text} ${data.level === "EMERGENCY" ? "animate-pulse" : ""}`} />}
                    <span className={`font-mono text-[11px] font-bold tracking-widest shrink-0 ${st.text}`}
                        data-testid="trading-readiness-level">
                        TRADING {data.level.replace("_", "-")}
                    </span>
                    {top ? (
                        <span className="font-mono text-[10px] text-[#A1A1AA] truncate"
                            data-testid="trading-readiness-reason">
                            {top.message}
                            {top.accounts?.length ? ` · ${top.accounts.length} account(s)` : ""}
                            {top.first_seen ? ` · since ${ago(top.first_seen)}` : ""}
                        </span>
                    ) : (
                        <span className="font-mono text-[10px] text-[#52525B]">
                            {data.accounts_enabled} enabled account(s) — all checks clear
                        </span>
                    )}
                </div>
                {(data.reasons || []).length > 0 && (
                    <button onClick={() => setOpenList(o => !o)}
                        data-testid="trading-readiness-blockers-btn"
                        className={`shrink-0 flex items-center gap-1 font-mono text-[10px] tracking-widest ${st.text} hover:opacity-80`}>
                        {data.reasons.length} BLOCKER{data.reasons.length === 1 ? "" : "S"}
                        <ChevronDown className={`w-3 h-3 transition-transform ${openList ? "rotate-180" : ""}`} />
                    </button>
                )}
            </div>
            {openList && (
                <div className="px-4 md:px-6 pb-2 space-y-1.5" data-testid="trading-readiness-blockers">
                    {(data.reasons || []).map(r => (
                        <div key={r.code} className="font-mono text-[10px] border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2"
                            data-testid={`readiness-reason-${r.code}`}>
                            <div className="flex items-center gap-2 flex-wrap">
                                <button onClick={() => setTour({ code: r.code, reason: r.message })}
                                    data-testid={`fix-this-btn-${r.code}`}
                                    className="flex items-center gap-1 font-mono text-[9px] font-bold tracking-widest px-2 py-0.5 border border-[#FFD700]/50 text-[#FFD700] hover:bg-[#FFD700]/10 transition-colors shrink-0">
                                    <LifeBuoy className="w-3 h-3" /> FIX THIS
                                </button>
                                <span className={(LEVEL_STYLE[r.level] || LEVEL_STYLE.DEGRADED).text}>{r.level.replace("_", "-")}</span>
                                <span className="text-[#52525B]">{r.code}</span>
                                <span className="text-[#A1A1AA]">{r.message}</span>
                                {r.first_seen && <span className="text-[#52525B]">first seen {ago(r.first_seen)} ago</span>}
                            </div>
                            <div className="text-[#52525B] mt-0.5">→ {r.recovery}</div>
                            {(r.accounts || []).length > 0 && (
                                <div className="text-[#52525B] mt-0.5" data-testid={`readiness-reason-accounts-${r.code}`}>
                                    {r.accounts.map(a => (
                                        <div key={a.account_id} className="flex items-center gap-2 flex-wrap">
                                            <span>
                                                affected: <span className="text-[#A1A1AA]">{a.label || a.account_id.slice(-6)}</span>
                                                {a.reason ? <span> — {a.reason}</span> : null}
                                            </span>
                                            {a.trust_eligible && (
                                                <button onClick={() => trustTerminal(a.account_id)}
                                                    disabled={trusting === a.account_id}
                                                    data-testid={`trust-terminal-btn-${a.account_id}`}
                                                    className="font-mono text-[9px] font-bold tracking-widest px-2 py-0.5 border border-[#00FF41]/50 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-50 transition-colors">
                                                    {trusting === a.account_id ? "TRUSTING…" : "TRUST THIS TERMINAL"}
                                                </button>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            )}
            {tour && (
                <UnblockTour code={tour.code} reason={tour.reason}
                    onClose={() => setTour(null)} />
            )}
        </div>
    );
}
