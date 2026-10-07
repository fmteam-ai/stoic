import { useEffect, useState } from "react";
import api from "@/lib/api";

// A15-3 — one site-wide notice while sign-ups are closed: closed beta, demo only, no live capital.
export function ClosedBetaBanner({ compact = false }) {
    const [s, setS] = useState(null);
    useEffect(() => {
        api.get("/auth/signups-status").then((r) => setS(r.data)).catch(() => setS(null));
    }, []);
    if (!s?.closed) return null;
    return (
        <div data-testid="closed-beta-banner" role="status"
             className={`w-full bg-[#FFB020]/10 border-b border-[#FFB020]/40 text-[#FFB020] font-mono tracking-wider ${compact ? "text-[10px] px-3 py-1.5" : "text-[11px] px-4 py-2"}`}>
            <span className="font-bold">CLOSED BETA</span> · demo accounts only · no live capital ·
            readiness: <a href="/status" className="underline hover:text-white">/status</a> ·
            support: <a href="/support" className="underline hover:text-white">/support</a>
            {s.message ? <span className="text-[#A1A1AA]"> · {s.message}</span> : null}
        </div>
    );
}
