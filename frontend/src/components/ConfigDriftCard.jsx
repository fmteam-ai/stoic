import { useCallback, useEffect, useState } from "react";
import { GitCompareArrows, RefreshCw } from "lucide-react";
import api from "@/lib/api";

// v1.60.40 — Drift Telemetry: SAVEs whose server echo differed from what the client sent (POST /bot/config/drift).
export function ConfigDriftCard() {
    const [d, setD] = useState(null);
    const load = useCallback(() => api.get("/ops/config-drift?days=7").then((r) => setD(r.data)).catch(() => setD({ error: true })), []);
    useEffect(() => { load(); const t = setInterval(load, 120000); return () => clearInterval(t); }, [load]);
    const events = d?.events ?? 0;
    const tone = d?.error ? "text-[#FF3B30]" : events === 0 ? "text-[#00FF41]" : "text-[#FFB000]";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="config-drift-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <GitCompareArrows className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Config Drift</span>
                <span className={`ml-auto font-mono text-[11px] ${tone}`} data-testid="config-drift-count">
                    {d?.error ? "unavailable" : `${events} mismatch${events === 1 ? "" : "es"} · 7d`}
                </span>
                <button onClick={load} className="text-[#52525B] hover:text-white" data-testid="config-drift-refresh" aria-label="Refresh"><RefreshCw className="w-3 h-3" /></button>
            </div>
            <div className="px-4 py-2 font-mono text-[11px]">
                {d && !d.error && events === 0 && <div className="text-[#52525B]">every saved field came back unchanged — no silent reverts</div>}
                {d && !d.error && events > 0 && (
                    <>
                        <div className="text-[#52525B] text-[10px] mb-1">{d.users} user{d.users === 1 ? "" : "s"} · last {d.last_at ? new Date(d.last_at).toLocaleString() : "—"}</div>
                        <div className="flex flex-wrap gap-2" data-testid="config-drift-fields">
                            {(d.fields || []).map((f) => (
                                <span key={f.field} className="px-1.5 py-0.5 border border-[#FFB000]/30 text-[#FFB000] text-[10px]">{f.field} ×{f.count}</span>
                            ))}
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

export default ConfigDriftCard;
