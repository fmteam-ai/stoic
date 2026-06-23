import { useEffect, useState } from "react";
import api from "@/lib/api";

/**
 * TickerTape — infinite-scrolling marquee of live market data.
 * - XAUUSD, BTCUSD via /market/quotes
 * - DFF (Fed Funds), DGS10 (10Y), VIX, T10YIE via /agents/macro (FRED)
 * Hover to pause. Refreshes prices every 30s.
 */

const SYMBOLS = ["XAUUSD", "BTCUSD"];

const MACRO_LABELS = {
    DFF:    { label: "FED FUNDS",  unit: "%" },
    DGS10:  { label: "US 10Y",     unit: "%" },
    VIXCLS: { label: "VIX",        unit: ""  },
    T10YIE: { label: "BREAKEVEN",  unit: "%" },
    UNRATE: { label: "UNEMPLOY",   unit: "%" },
};

function formatPrice(p) {
    if (p == null) return "—";
    if (p >= 1000) return p.toLocaleString(undefined, { maximumFractionDigits: 2 });
    return p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function TickerItem({ label, value, change, suffix }) {
    const positive = (change ?? 0) >= 0;
    const color = change == null ? "#A1A1AA" : positive ? "#00FF41" : "#FF3B30";
    return (
        <span className="inline-flex items-center gap-2 px-5 py-2 border-r border-[#1F1F1F]"
            data-testid={`ticker-item-${label}`}>
            <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{label}</span>
            <span className="font-mono text-xs text-white">
                {formatPrice(value)}{suffix}
            </span>
            {change != null && (
                <span className="font-mono text-[10px]" style={{ color }}>
                    {positive ? "▲" : "▼"} {Math.abs(change).toFixed(2)}%
                </span>
            )}
        </span>
    );
}

export function TickerTape() {
    const [items, setItems] = useState([]);

    useEffect(() => {
        let cancelled = false;

        const load = async () => {
            const next = [];
            try {
                const { data } = await api.get(`/market/quotes?symbols=${SYMBOLS.join(",")}`);
                for (const q of data.quotes || []) {
                    if (q.error || !q.symbol) continue;
                    next.push({
                        label: q.symbol,
                        value: q.price,
                        change: q.change_pct,
                        suffix: "",
                    });
                }
            } catch (e) {
                /* network errors are silent — ticker just goes stale */
            }
            try {
                const { data } = await api.get("/agents/macro");
                const series = data?.series || {};
                for (const [code, meta] of Object.entries(MACRO_LABELS)) {
                    const row = series[code];
                    if (!row) continue;
                    next.push({
                        label: meta.label,
                        value: row.value,
                        change: null,           // FRED: 30d delta absolute, not % — hide
                        suffix: meta.unit,
                    });
                }
            } catch (e) { /* macro optional */ }
            if (!cancelled && next.length) setItems(next);
        };

        load();
        const id = setInterval(load, 30_000);
        return () => { cancelled = true; clearInterval(id); };
    }, []);

    if (items.length === 0) return null;

    // Duplicate the items so the keyframe `translateX(-50%)` produces a seamless loop.
    const reel = [...items, ...items];

    return (
        <div className="border-b border-[#1F1F1F] bg-[#0A0A0A] overflow-hidden"
            data-testid="ticker-tape">
            <div className="ticker-track">
                {reel.map((it, idx) => (
                    <TickerItem key={`${it.label}-${idx}`}
                        label={it.label}
                        value={it.value}
                        change={it.change}
                        suffix={it.suffix} />
                ))}
            </div>
        </div>
    );
}
