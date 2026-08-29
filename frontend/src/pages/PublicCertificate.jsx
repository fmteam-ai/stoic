import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { BACKEND_URL } from "@/lib/api";
import { Award, ShieldCheck, ShieldX, Fingerprint } from "lucide-react";

const TIER_LABEL = {
    CERTIFIED_A: "CERTIFIED · TIER A", CERTIFIED_B: "CERTIFIED · TIER B",
    PROVISIONAL: "PROVISIONAL", UNCERTIFIED: "UNCERTIFIED",
};
const PILLAR_LABEL = {
    risk_truth: "Risk Truth", position_truth: "Position Truth",
    execution_alpha: "Execution Alpha", digital_twin: "Digital Twin",
    strategy_decay: "Strategy Decay", broker_intel: "Broker Intelligence",
};

export default function PublicCertificate() {
    const { certId } = useParams();
    const [cert, setCert] = useState(null);
    const [error, setError] = useState(null);

    useEffect(() => {
        fetch(`${BACKEND_URL}/api/public/certificate/${certId}`)
            .then(async r => {
                if (!r.ok) throw new Error((await r.json())?.detail || "not found");
                setCert(await r.json());
            })
            .catch(e => setError(String(e.message || e)));
    }, [certId]);

    const ok = cert?.valid && cert?.hash_verified;

    return (
        <div className="min-h-screen bg-[#050505] text-[#FAFAFA] flex items-center justify-center p-6"
            data-testid="public-certificate-page">
            <div className="w-full max-w-2xl">
                {error && (
                    <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/5 p-6 text-center" data-testid="pc-error">
                        <ShieldX className="w-10 h-10 text-[#FF3B30] mx-auto mb-2" />
                        <div className="font-mono text-sm text-[#FF3B30]">Certificate not found</div>
                        <div className="text-xs text-[#71717A] mt-1">{error}</div>
                    </div>
                )}
                {cert && (
                    <div className={`border ${ok ? "border-[#00FF41]/40" : "border-[#FF3B30]/40"} bg-[#0A0A0A] p-8`}
                        data-testid="pc-card">
                        <div className="text-center mb-6">
                            <Award className={`w-14 h-14 mx-auto mb-3 ${ok ? "text-[#00FF41]" : "text-[#FF3B30]"}`} />
                            <div className="text-[10px] font-mono tracking-[0.35em] text-[#52525B] mb-1">
                                STOIC PRODUCTION CERTIFICATE
                            </div>
                            <div className={`text-3xl font-mono font-bold tracking-widest ${ok ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                                data-testid="pc-tier">
                                {TIER_LABEL[cert.tier] || cert.tier}
                            </div>
                            <div className="mt-2 flex items-center justify-center gap-2 text-xs font-mono"
                                data-testid="pc-validity">
                                {ok ? <ShieldCheck className="w-4 h-4 text-[#00FF41]" /> : <ShieldX className="w-4 h-4 text-[#FF3B30]" />}
                                <span className={ok ? "text-[#00FF41]" : "text-[#FF3B30]"}>
                                    {cert.revoked ? "REVOKED" : cert.expired ? "EXPIRED"
                                        : !cert.hash_verified ? "HASH MISMATCH — DO NOT TRUST"
                                        : "VALID · HASH VERIFIED"}
                                </span>
                            </div>
                        </div>

                        <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs font-mono mb-6" data-testid="pc-meta">
                            <div className="text-[#52525B]">Certificate</div><div data-testid="pc-cert-id">{cert.cert_id} · #{cert.seq}</div>
                            <div className="text-[#52525B]">Subject</div><div>{cert.subject}</div>
                            <div className="text-[#52525B]">Broker / Account</div><div>{cert.broker} · {cert.account_ref}</div>
                            <div className="text-[#52525B]">Overall score</div><div data-testid="pc-score">{cert.overall_score ?? "—"} / 100 ({cert.pillars_scored} pillars)</div>
                            <div className="text-[#52525B]">Issued</div><div>{new Date(cert.issued_at).toUTCString()}</div>
                            <div className="text-[#52525B]">Expires</div><div>{new Date(cert.expires_at).toUTCString()}</div>
                            <div className="text-[#52525B]">Guard policy</div><div>{cert.provenance?.guard_policy_version}</div>
                        </div>

                        <div className="space-y-2 mb-6" data-testid="pc-pillars">
                            {(cert.pillars || []).map(p => (
                                <div key={p.pillar} className="flex items-center gap-3 text-xs">
                                    <span className="w-40 shrink-0 text-[#A1A1AA] font-mono">{PILLAR_LABEL[p.pillar] || p.pillar}</span>
                                    <div className="flex-1 h-1.5 bg-[#1F1F1F]">
                                        {p.score != null && (
                                            <div className={`h-full ${p.status === "GREEN" ? "bg-[#00FF41]" : p.status === "YELLOW" ? "bg-[#FFD700]" : "bg-[#FF3B30]"}`}
                                                style={{ width: `${p.score}%` }} />
                                        )}
                                    </div>
                                    <span className="w-12 text-right font-mono">{p.score != null ? p.score : "N/A"}</span>
                                </div>
                            ))}
                        </div>

                        <div className="border-t border-[#1F1F1F] pt-4 text-[10px] font-mono text-[#52525B] break-all"
                            data-testid="pc-hash">
                            <div className="flex items-center gap-1.5 mb-1 text-[#71717A]">
                                <Fingerprint className="w-3 h-3" /> HASH CHAIN
                            </div>
                            <div>hash: {cert.hash}</div>
                            <div>prev: {cert.prev_hash}</div>
                            <div className="mt-2 text-[#3F3F46]">{cert.verify}</div>
                        </div>
                    </div>
                )}
                <div className="text-center mt-4 text-[10px] font-mono tracking-widest text-[#3F3F46]">
                    VERIFIED BY STOIC · stoicaibot.com
                </div>
            </div>
        </div>
    );
}
