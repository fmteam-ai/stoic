import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Ban, AlertTriangle, Pause, ChevronDown, ChevronUp, Activity } from "lucide-react";

const LEVEL_RANK = { block: 3, warn: 2, info: 1 };
const LEVEL_STYLE = {
    block: { border: "border-[#FF3B30]/40", bg: "bg-[#FF3B30]/5", fg: "text-[#FF3B30]", Icon: Ban },
    warn:  { border: "border-[#FFB000]/40", bg: "bg-[#FFB000]/5", fg: "text-[#FFB000]", Icon: AlertTriangle },
    info:  { border: "border-[#1F1F1F]",    bg: "bg-[#0A0A0A]",   fg: "text-[#A1A1AA]", Icon: Pause },
};

function ago(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return `${seconds}s ago`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ${m % 60}m ago`;
    return `${Math.floor(h / 24)}d ago`;
}

// Banner that answers "why is my bot silent?" at a glance. Renders only when
// at least one bot is active AND none has executed in the last 30 minutes.
export default function SilentBotBanner() {
    const [items, setItems] = useState([]);
    const [open, setOpen] = useState(false);

    const load = useCallback(async () => {
        try {
            const r = await api.get("/bot/pulse");
            setItems((r.data?.items || []).filter((i) => i.active));
        } catch { /* silent */ }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);

    if (!items.length) return null;

    const executedRecently = items.some((i) =>
        ["BUY", "SELL", "EXEC"].includes(i.notable?.action) && (i.notable_stale_seconds ?? 1e9) < 1800);
    if (executedRecently) return null;

    const withNotable = items.filter((i) => i.notable);
    if (!withNotable.length) return null;

    // Dominant reason: most severe level wins, ties broken by recency.
    const dominant = [...withNotable].sort((a, b) => {
        const lv = (LEVEL_RANK[b.notable.level] || 1) - (LEVEL_RANK[a.notable.level] || 1);
        if (lv !== 0) return lv;
        return (a.notable_stale_seconds ?? 1e9) - (b.notable_stale_seconds ?? 1e9);
    })[0];

    const style = LEVEL_STYLE[dominant.notable.level] || LEVEL_STYLE.info;
    const Icon = style.Icon;

    return (
        <div className={`border ${style.border} ${style.bg}`} data-testid="silent-bot-banner">
            <div className="flex items-start gap-3 px-4 py-3">
                <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${style.fg}`} />
                <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className={`font-mono text-[10px] tracking-widest ${style.fg}`}>
                            WHY IS MY BOT SILENT?
                        </span>
                        <span className="font-mono text-[10px] text-[#52525B]">
                            {dominant.label} · {ago(dominant.notable_stale_seconds)}
                        </span>
                    </div>
                    <p className="text-xs text-[#E4E4E7] mt-1 leading-relaxed" data-testid="silent-bot-reason">
                        {dominant.notable.reason}
                    </p>
                </div>
                {withNotable.length > 1 && (
                    <button onClick={() => setOpen(!open)} data-testid="silent-bot-expand"
                        className="shrink-0 flex items-center gap-1 font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-[#00FF41] transition-colors">
                        {withNotable.length} BOTS {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    </button>
                )}
            </div>
            {open && (
                <div className="border-t border-[#1F1F1F] divide-y divide-[#1F1F1F]">
                    {withNotable.map((i) => {
                        const s = LEVEL_STYLE[i.notable.level] || LEVEL_STYLE.info;
                        return (
                            <div key={i.config_id} className="flex items-start gap-3 px-4 py-2"
                                 data-testid={`silent-bot-row-${i.config_id}`}>
                                <Activity className={`w-3 h-3 mt-0.5 shrink-0 ${s.fg}`} />
                                <span className="font-mono text-[10px] text-[#A1A1AA] w-40 shrink-0 truncate">{i.label}</span>
                                <span className="text-[11px] text-[#D4D4D8] flex-1">{i.notable.reason}</span>
                                <span className="font-mono text-[10px] text-[#52525B] shrink-0">{ago(i.notable_stale_seconds)}</span>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
