import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Layers, TrendingUp, TrendingDown, Minus, Check, Clock } from "lucide-react";

const TREND_STYLE = {
    UP:   { fg: "text-[#00FF41]", Icon: TrendingUp },
    DOWN: { fg: "text-[#FF3B30]", Icon: TrendingDown },
    FLAT: { fg: "text-[#52525B]", Icon: Minus },
};

function Row({ label, value, detail, ok }) {
    return (
        <div className="flex items-center gap-3 px-5 py-2.5">
            <span className="font-mono text-[9px] tracking-widest text-[#52525B] w-24 shrink-0">{label}</span>
            <span className={`font-mono text-xs w-28 shrink-0 ${ok === true ? "text-[#00FF41]" : ok === false ? "text-[#52525B]" : "text-[#E4E4E7]"}`}>{value}</span>
            <span className="text-[11px] text-[#A1A1AA] flex-1 truncate">{detail}</span>
        </div>
    );
}

// Live MTF cascade: 4H trend → 1H structure → M15 setup → entry trigger.
export default function MtfCascadePanel() {
    const [data, setData] = useState(null);

    const load = useCallback(async () => {
        try {
            const r = await api.get("/bot/mtf-confluence?symbol=XAUUSD");
            setData(r.data);
        } catch { /* silent */ }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 60000);
        return () => clearInterval(t);
    }, [load]);

    if (!data || !data.available) return null;
    const h4 = TREND_STYLE[data.h4_trend] || TREND_STYLE.FLAT;
    const h1 = TREND_STYLE[data.h1_structure] || TREND_STYLE.FLAT;
    const setup = data.m15_setup;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="mtf-cascade-panel">
            <div className="flex items-center justify-between px-5 py-3 border-b border-[#1F1F1F]">
                <div className="flex items-center gap-2">
                    <Layers className="w-4 h-4 text-[#00FF41]" />
                    <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">MTF CONFLUENCE · XAUUSD · LIVE {data.live_price}</span>
                </div>
                {data.aligned ? (
                    <span className="flex items-center gap-1.5 px-2 py-0.5 border border-[#00FF41]/40 bg-[#00FF41]/10 font-mono text-[9px] tracking-widest text-[#00FF41]" data-testid="mtf-aligned-badge">
                        <Check className="w-3 h-3" /> ALIGNED · {data.direction}
                    </span>
                ) : (
                    <span className="flex items-center gap-1.5 font-mono text-[9px] tracking-widest text-[#52525B]">
                        <Clock className="w-3 h-3" /> WAITING
                    </span>
                )}
            </div>
            <div className="divide-y divide-[#1F1F1F]">
                <Row label="4H TREND" ok={data.h4_trend !== "FLAT"}
                     value={<span className={`flex items-center gap-1 ${h4.fg}`}><h4.Icon className="w-3 h-3" />{data.h4_trend}</span>}
                     detail="Overall trend (resampled from EA M15 stream)" />
                <Row label="1H STRUCTURE" ok={data.h1_structure !== "FLAT" && data.h1_structure === data.h4_trend}
                     value={<span className={`flex items-center gap-1 ${h1.fg}`}><h1.Icon className="w-3 h-3" />{data.h1_structure}</span>}
                     detail={data.h1_structure === data.h4_trend && data.h4_trend !== "FLAT" ? "Agrees with 4H" : "Must agree with 4H"} />
                <Row label="15M SETUP" ok={setup ? setup.ready : false}
                     value={setup ? (setup.ready ? "PULLBACK ✓" : "WAITING") : "—"}
                     detail={setup ? setup.note : "Needs 4H + 1H agreement first"} />
                <Row label="ENTRY" ok={data.entry_trigger}
                     value={data.entry_trigger ? "TRIGGERED" : data.swing_level ? `BREAK ${data.swing_level}` : "—"}
                     detail={data.note} />
            </div>
        </div>
    );
}
