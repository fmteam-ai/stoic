import { useEffect, useState } from "react";
import api from "@/lib/api";
import { ChevronDown, Loader2 } from "lucide-react";

const TONE = {
    connecting: "border-[#FFB000]/50 text-[#FFB000]",
    verified: "border-[#00BFFF]/50 text-[#00BFFF]",
    ready: "border-[#00FF41]/50 text-[#00FF41]",
    trading: "border-[#00FF41] text-[#00FF41] bg-[#00FF41]/10",
    blocked: "border-[#FF3B30]/60 text-[#FF3B30]",
};
const STEPS = ["connecting", "verified", "ready", "trading"];

/** U15 — ONE status line per account with ONE fix button; everything else behind "Why?". Polls every 5 s. */
export function StatusLine({ account, onFix }) {
    const [sl, setSl] = useState(null);
    const [why, setWhy] = useState(false);
    useEffect(() => {
        let alive = true;
        const load = () => api.get(`/accounts/${account.id}/status-line`).then((r) => alive && setSl(r.data)).catch(() => {});
        load();
        const t = setInterval(load, 5000);
        return () => { alive = false; clearInterval(t); };
    }, [account.id]);
    if (!sl) return <div className="text-xs text-[#71717A] flex items-center gap-1" data-testid={`status-line-loading-${account.account_number}`}><Loader2 className="w-3 h-3 animate-spin" /> status…</div>;
    const idx = STEPS.indexOf(sl.state);
    return (
        <div className={`border px-3 py-2 mb-3 ${TONE[sl.state] || TONE.connecting}`} data-testid={`status-line-${account.account_number}`} data-state={sl.state}>
            <div className="flex flex-wrap items-center gap-3">
                <div className="flex items-center gap-1 font-mono text-[10px] tracking-widest">
                    {STEPS.map((s, i) => (
                        <span key={s} className={`${i <= idx && sl.state !== "blocked" ? "opacity-100" : "opacity-35"}`}>{s.toUpperCase()}{i < STEPS.length - 1 ? " →" : ""}</span>
                    ))}
                </div>
                <div className="text-sm text-white flex-1 min-w-[12rem]" data-testid={`status-line-label-${account.account_number}`}>{sl.label}</div>
                {sl.fix && (
                    <button onClick={() => onFix && onFix(sl.fix)} data-testid={`status-line-fix-${account.account_number}`}
                        className="px-3 py-1.5 text-xs font-mono tracking-widest bg-white text-black hover:bg-[#FFD700] transition-colors">
                        {sl.fix.label.toUpperCase()}
                    </button>
                )}
                <button onClick={() => setWhy((v) => !v)} data-testid={`status-line-why-${account.account_number}`} className="text-[11px] font-mono text-[#A1A1AA] hover:text-white flex items-center gap-0.5">
                    Why? <ChevronDown className={`w-3 h-3 transition-transform ${why ? "rotate-180" : ""}`} />
                </button>
            </div>
            {why && (
                <div className="mt-2 grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-0.5 font-mono text-[10px] text-[#A1A1AA]" data-testid={`status-line-internals-${account.account_number}`}>
                    {Object.entries(sl.internals || {}).map(([k, v]) => <div key={k}><span className="text-[#52525B]">{k}</span> {v === null || v === undefined ? "—" : String(v)}</div>)}
                </div>
            )}
        </div>
    );
}
