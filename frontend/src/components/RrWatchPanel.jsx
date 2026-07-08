import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Crosshair } from "lucide-react";

// Realized R:R watch — verifies the iter-119 expectancy fix on live trades.
export default function RrWatchPanel() {
    const [data, setData] = useState(null);

    useEffect(() => {
        api.get("/analytics/rr-watch").then((r) => setData(r.data)).catch(() => {});
    }, []);

    if (!data) return null;
    const { before, after } = data;
    const afterOk = after.median_rr != null && after.median_rr >= 0.75;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="rr-watch-panel">
            <div className="flex items-center gap-2 px-5 py-3 border-b border-[#1F1F1F]">
                <Crosshair className="w-4 h-4 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                    REALIZED R:R WATCH · GEOMETRY FIX VERIFICATION
                </span>
            </div>
            <div className="grid grid-cols-2 divide-x divide-[#1F1F1F]">
                <div className="px-5 py-4">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1">BEFORE FIX</div>
                    <div className="font-mono text-2xl text-[#FF3B30]" data-testid="rr-watch-before">
                        {before.median_rr ?? "—"}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] mt-1">
                        {before.trades} trades · WR {before.win_rate ?? "—"}% · net ${before.net_pnl}
                    </div>
                </div>
                <div className="px-5 py-4">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1">AFTER FIX</div>
                    <div className={`font-mono text-2xl ${afterOk ? "text-[#00FF41]" : "text-[#A1A1AA]"}`} data-testid="rr-watch-after">
                        {after.median_rr ?? "awaiting trades"}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] mt-1">
                        {after.trades} trades · WR {after.win_rate ?? "—"}% · net ${after.net_pnl}
                    </div>
                </div>
            </div>
            <div className="px-5 py-2 border-t border-[#1F1F1F]">
                <span className="font-mono text-[9px] text-[#52525B]">{data.note}</span>
            </div>
        </div>
    );
}
