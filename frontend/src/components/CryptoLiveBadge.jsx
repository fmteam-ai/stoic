import { useEffect, useState } from "react";
import api from "@/lib/api";

/* main97 — the crypto master switch state, visible wherever the demo is judged. */
export function CryptoLiveBadge({ className = "" }) {
    const [live, setLive] = useState(null);
    useEffect(() => {
        api.get("/crypto/status").then(({ data }) => setLive(!!data.live_enabled)).catch(() => setLive(null));
    }, []);
    if (live === null) return null;
    return (
        <span data-testid="crypto-live-badge"
              className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${live ? "border-[#FF3B30] text-[#FF3B30] bg-[#FF3B30]/10" : "border-[#00FF41]/50 text-[#00FF41] bg-[#00FF41]/5"} ${className}`}>
            LIVE CRYPTO: {live ? "ON" : "OFF"}
        </span>
    );
}
