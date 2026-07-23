import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import axios from "axios";
import { API } from "@/lib/api";
import { StoicOfficial } from "@/components/StoicLogo";
import { JournalCardBody } from "@/components/JournalCardModal";

export default function PublicJournal() {
    const { shareId } = useParams();
    const [d, setD] = useState(null);
    const [err, setErr] = useState("");

    useEffect(() => {
        axios.get(`${API}/public/journal/${shareId}`)
            .then(r => setD(r.data))
            .catch(() => setErr("This journal card is invalid or has been revoked."));
    }, [shareId]);

    return (
        <div className="min-h-screen bg-[#050505] text-white">
            <div className="max-w-lg mx-auto px-4 py-8 space-y-6" data-testid="public-journal">
                <div className="flex items-center gap-3">
                    <StoicOfficial size={34} />
                    <div>
                        <div className="font-display font-bold text-xl tracking-tight">STOIC · Trade Journal</div>
                        <div className="font-mono text-[10px] tracking-widest text-[#52525B]" data-testid="public-journal-ai-label">
                            {d?.edited ? "AI-ASSISTED · TRADER-EDITED · REAL TRADE" : "AI-WRITTEN POST-MORTEM · REAL TRADE"}
                        </div>
                    </div>
                </div>
                {err && (
                    <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-3 text-sm text-[#FF3B30]"
                        data-testid="public-journal-error">{err}</div>
                )}
                {d && (
                    <>
                        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6">
                            <JournalCardBody card={d.card} trade={d.trade} />
                        </div>
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B] text-center pb-8">
                            CLOSED {d.trade?.closed_at || "—"} · POWERED BY STOIC — PAST PERFORMANCE IS NOT INDICATIVE OF FUTURE RESULTS
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}
