import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Handshake, ExternalLink, Star } from "lucide-react";
import { toast } from "sonner";

// Recommended partner brokers with IB tracking links (admin-editable via
// POST /api/partners/brokers). Clicks are tracked before opening the link.
export default function PartnerBrokerCard() {
    const [brokers, setBrokers] = useState([]);

    const load = useCallback(async () => {
        try {
            const r = await api.get("/partners/brokers");
            setBrokers(r.data?.items || []);
        } catch { /* silent */ }
    }, []);

    useEffect(() => { load(); }, [load]);

    const open = async (b) => {
        try {
            const r = await api.post(`/partners/brokers/${b.id}/click`);
            window.open(r.data?.url || b.ib_link, "_blank", "noopener");
        } catch {
            toast.error("Could not open broker link");
        }
    };

    if (!brokers.length) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="partner-broker-card">
            <div className="flex items-center gap-2 px-5 py-3 border-b border-[#1F1F1F]">
                <Handshake className="w-4 h-4 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">RECOMMENDED BROKERS · TESTED WITH THE STOIC EA</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-[#1F1F1F]">
                {brokers.map((b) => (
                    <div key={b.id} className="p-5 flex flex-col gap-2" data-testid={`partner-broker-${b.id}`}>
                        <div className="flex items-center gap-2">
                            <span className="text-sm text-[#E4E4E7] font-semibold">{b.name}</span>
                            {b.highlight && <Star className="w-3.5 h-3.5 text-[#FFD700] fill-[#FFD700]" />}
                        </div>
                        <p className="text-xs text-[#A1A1AA] leading-relaxed flex-1">{b.tagline}</p>
                        <div className="font-mono text-[10px] text-[#52525B] space-y-0.5">
                            <div>{b.regulation}</div>
                            <div>MIN DEPOSIT {b.min_deposit}</div>
                        </div>
                        <button onClick={() => open(b)} data-testid={`partner-broker-open-${b.id}`}
                            className="mt-1 flex items-center justify-center gap-2 px-3 py-2 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 font-mono text-[10px] tracking-widest transition-colors">
                            OPEN ACCOUNT <ExternalLink className="w-3 h-3" />
                        </button>
                    </div>
                ))}
            </div>
            <div className="px-5 py-2 border-t border-[#1F1F1F]">
                <span className="font-mono text-[9px] text-[#52525B]">
                    Opening an account through these links supports STOIC development at no extra cost to you.
                </span>
            </div>
        </div>
    );
}
