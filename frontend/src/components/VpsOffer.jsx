import { useEffect, useState } from "react";
import api from "@/lib/api";
import { ExternalLink } from "lucide-react";

const FALLBACK = { provider: "ForexVPS", url: "https://www.forexvps.net/partner/stoicaibot", label: "Get a ForexVPS (recommended)" };
let cached = null;

/** Recommended VPS + referral link (admin-editable: Integrations → VPS provider). Public endpoint, cached per page load. */
export function useVpsOffer() {
    const [offer, setOffer] = useState(cached || FALLBACK);
    useEffect(() => {
        if (cached) return;
        api.get("/public/vps-offer").then(r => { cached = r.data; setOffer(r.data); }).catch(() => {});
    }, []);
    return offer;
}

export function VpsOfferButton({ className = "", testid = "vps-offer-link", compact = false }) {
    const o = useVpsOffer();
    return (
        <a href={o.url} target="_blank" rel="noopener noreferrer sponsored" data-testid={testid}
            className={`inline-flex items-center gap-1.5 font-mono tracking-widest border border-[#FFD700]/50 bg-[#FFD700]/10 text-[#FFD700] hover:bg-[#FFD700]/20 ${compact ? "px-2 py-1 text-[10px]" : "px-3 py-2 text-xs"} ${className}`}>
            {o.label.toUpperCase()} <ExternalLink className="w-3 h-3" />
        </a>
    );
}
