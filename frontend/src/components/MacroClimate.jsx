import { useEffect, useState } from "react";
import api from "@/lib/api";
import { TrendingUp, TrendingDown, Minus, AlertTriangle, ShieldOff, ShieldCheck } from "lucide-react";

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
    const [gate, setGate] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let mounted = true;
        const load = async () => {
            try {
                const [snapRes, gateRes] = await Promise.allSettled([
                    api.get("/macro/snapshot"),
                    api.get("/macro/gate"),
                ]);
                if (!mounted) return;
                if (snapRes.status === "fulfilled") setSnap(snapRes.value.data);
                if (gateRes.status === "fulfilled") setGate(gateRes.value.data);
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

    const gateBlocked = gate && (!gate.buy_ok || !gate.sell_ok);

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

            {gate && (
                <div
                    data-testid="macro-gate-status"
                    className={`mb-2 px-3 py-2 border flex items-start gap-2 ${
                        gateBlocked
                            ? "border-[#FF3B30]/40 bg-[#FF3B30]/5"
                            : "border-[#00FF41]/30 bg-[#00FF41]/5"
                    }`}
                >
                    {gateBlocked ? (
                        <ShieldOff className="w-3.5 h-3.5 text-[#FF3B30] mt-0.5 shrink-0" />
                    ) : (
                        <ShieldCheck className="w-3.5 h-3.5 text-[#00FF41] mt-0.5 shrink-0" />
                    )}
                    <div className="flex-1 min-w-0">
                        <div className="font-mono text-[10px] tracking-widest mb-0.5"
                             style={{ color: gateBlocked ? "#FF3B30" : "#00FF41" }}>
                            XAUUSD MACRO GATE · {gateBlocked ? "BLOCKING" : "OPEN"}
                        </div>
                        {gateBlocked ? (
                            <div className="space-y-0.5">
                                {(gate.blocked || []).map((b) => (
                                    <div key={b.action} className="text-[11px] text-[#E4E4E7] font-mono leading-snug">
                                        <span className="text-[#FF3B30]">{b.action}</span>
                                        <span className="text-[#52525B] mx-1.5">·</span>
                                        <span className="text-[#A1A1AA]">{b.reason}</span>
                                    </div>
                                ))}
                            </div>
                        ) : (
                            <div className="text-[11px] text-[#A1A1AA] font-mono leading-snug">
                                10Y yields + USD index within neutral range — gold trades unblocked.
                            </div>
                        )}
                    </div>
                </div>
            )}

            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2">
                {snap.series.map((s) => <SeriesCard key={s.series_id} s={s} />)}
            </div>
        </div>
    );
}
