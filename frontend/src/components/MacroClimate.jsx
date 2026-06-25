import { useEffect, useState } from "react";
import api from "@/lib/api";
import { TrendingUp, TrendingDown, Minus, AlertTriangle } from "lucide-react";

function Delta({ value, label }) {
    const v = Number(value) || 0;
    const isUp = v > 0.001;
    const isDown = v < -0.001;
    const color = isUp ? "#00FF41" : isDown ? "#FF3B30" : "#52525B";
    const Icon = isUp ? TrendingUp : isDown ? TrendingDown : Minus;
    return (
        <div className="flex items-center gap-1 text-[10px] font-mono">
            <span className="text-[#52525B] tracking-widest">{label}</span>
            <Icon className="w-3 h-3" style={{ color }} />
            <span style={{ color }}>{v >= 0 ? "+" : ""}{v.toFixed(2)}</span>
        </div>
    );
}

function SeriesCard({ s }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3" data-testid={`macro-card-${s.series_id}`}>
            <div className="flex items-center justify-between mb-2">
                <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{s.series_id}</div>
                {s.stale_cache && (
                    <AlertTriangle className="w-3 h-3 text-[#FFB000]" title="Cached — FRED unreachable" />
                )}
            </div>
            <div className="text-xs text-[#A1A1AA] mb-1.5 leading-tight">{s.name}</div>
            <div className="flex items-baseline gap-1 mb-2">
                <span className="text-xl font-mono font-bold text-[#E4E4E7]">
                    {Number(s.latest).toFixed(s.unit === "%" ? 2 : 2)}
                </span>
                <span className="text-[10px] text-[#52525B]">{s.unit}</span>
            </div>
            <div className="flex items-center gap-3">
                <Delta value={s.dod_delta} label="DoD" />
                <Delta value={s.wow_delta} label="WoW" />
            </div>
            <div className="text-[9px] text-[#52525B] mt-2 font-mono">as of {s.date}</div>
        </div>
    );
}

export function MacroClimate() {
    const [snap, setSnap] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let mounted = true;
        const load = async () => {
            try {
                const { data } = await api.get("/macro/snapshot");
                if (mounted) setSnap(data);
            } catch {
                /* silent — widget self-hides on error */
            } finally {
                if (mounted) setLoading(false);
            }
        };
        load();
        // Refresh every 10 min (server caches for 1h anyway — this just rotates UI)
        const t = setInterval(load, 600_000);
        return () => { mounted = false; clearInterval(t); };
    }, []);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] p-4 text-center font-mono text-[10px] text-[#52525B] tracking-widest"
                 data-testid="macro-climate-loading">
                LOADING MACRO CLIMATE…
            </div>
        );
    }
    if (!snap || !snap.series || snap.series.length === 0) {
        return null;  // graceful hide on no data / no API key
    }

    return (
        <div data-testid="macro-climate">
            <div className="flex items-center justify-between px-1 mb-2">
                <div className="font-mono text-[10px] text-[#FFB000] tracking-widest">MACRO CLIMATE</div>
                {snap.stale_cache && (
                    <div className="font-mono text-[10px] text-[#FFB000] tracking-widest inline-flex items-center gap-1">
                        <AlertTriangle className="w-3 h-3" /> STALE CACHE
                    </div>
                )}
            </div>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2">
                {snap.series.map((s) => <SeriesCard key={s.series_id} s={s} />)}
            </div>
        </div>
    );
}
