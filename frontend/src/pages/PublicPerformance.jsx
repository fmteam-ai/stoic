import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import axios from "axios";
import { API } from "@/lib/api";
import { StoicOfficial } from "@/components/StoicLogo";
import { EquityCurve, StatTiles, IntegrityStamp, AccountsTable } from "@/components/VerifiedPerf";
import { AttestationSeal } from "@/components/AttestationSeal";

export default function PublicPerformance() {
    const { shareId } = useParams();
    const [d, setD] = useState(null);
    const [err, setErr] = useState("");

    useEffect(() => {
        axios.get(`${API}/public/performance/${shareId}`)
            .then(r => setD(r.data))
            .catch(() => setErr("This share link is invalid or has been revoked."));
    }, [shareId]);

    return (
        <div className="min-h-screen bg-[#050505] text-white">
            <div className="max-w-5xl mx-auto px-4 py-8 space-y-6" data-testid="public-performance">
                <div className="flex items-center gap-3">
                    <StoicOfficial size={34} />
                    <div>
                        <div className="font-display font-bold text-xl tracking-tight">STOIC · Verified Performance</div>
                        <div className="font-mono text-[10px] tracking-widest text-[#52525B]">
                            READ-ONLY BROKER-TRUTH TRACK RECORD
                        </div>
                    </div>
                </div>
                {err && (
                    <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-3 text-sm text-[#FF3B30]"
                        data-testid="public-perf-error">{err}</div>
                )}
                {d && d.verification_status?.startsWith("UNVERIFIED") && (
                    <div className="border border-[#FF3B30]/50 bg-[#FF3B30]/10 px-4 py-3" data-testid="public-perf-unverified">
                        <div className="font-mono text-xs tracking-widest text-[#FF3B30] font-bold">UNVERIFIED — NOT LIVE PERFORMANCE</div>
                        <div className="text-xs text-[#A1A1AA] mt-1">
                            Headline financial metrics are withheld: {(d.attestation_blocked?.reasons || []).join(", ")}.
                        </div>
                    </div>
                )}
                {d && (
                    <>
                        <IntegrityStamp integrity={d.integrity} />
                        {d.overall && <StatTiles overall={d.overall} maxDrawdown={d.max_drawdown} />}
                        {d.equity_curve?.length > 0 && <EquityCurve curve={d.equity_curve} />}
                        <AccountsTable accounts={d.accounts} />
                        <div className="font-mono text-[9px] tracking-widest text-[#52525B] text-center pb-8">
                            GENERATED {d.generated_at} · POWERED BY STOIC — PAST PERFORMANCE IS NOT INDICATIVE OF FUTURE RESULTS
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}
