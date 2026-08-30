import { useState } from "react";
import api from "@/lib/api";
import { BadgeCheck, ShieldCheck, ShieldX } from "lucide-react";

export const AttestationSeal = ({ attestation, blocked }) => {
    const [result, setResult] = useState(null);
    const [busy, setBusy] = useState(false);

    if (!attestation) {
        if (!blocked) return null;
        return (
            <div className="border border-[#FFB020]/40 bg-[#FFB020]/5 px-4 py-3"
                data-testid="attestation-withheld">
                <div className="font-mono text-[10px] tracking-widest text-[#FFB020] mb-1">
                    ATTESTATION WITHHELD
                </div>
                <div className="font-mono text-[10px] text-[#A1A1AA] leading-relaxed">
                    {blocked.note}
                </div>
                <div className="font-mono text-[10px] text-[#52525B] mt-1">
                    blockers: {(blocked.reasons || []).join(", ")}
                </div>
            </div>
        );
    }

    const verify = async () => {
        setBusy(true);
        try {
            const { data } = await api.post("/public/performance/verify", {
                payload_hash: attestation.payload_hash,
                signature: attestation.signature,
            });
            setResult(data.valid ? "valid" : "invalid");
        } catch {
            setResult("invalid");
        } finally { setBusy(false); }
    };

    return (
        <div className="border border-[#0099FF]/30 bg-[#0099FF]/5 p-4" data-testid="attestation-seal">
            <div className="flex items-center gap-2 flex-wrap">
                <BadgeCheck className="w-4 h-4 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Signed Attestation</span>
                <span className="font-mono text-[9px] tracking-widest text-[#52525B] border border-[#1F1F1F] px-1.5 py-0.5">
                    {attestation.algo} · {attestation.key_id}
                </span>
                <button onClick={verify} disabled={busy} data-testid="attestation-verify-btn"
                    className="ml-auto px-3 py-1.5 border border-[#0099FF]/40 text-[#0099FF] hover:bg-[#0099FF]/10 text-[10px] font-mono tracking-widest disabled:opacity-50">
                    {busy ? "VERIFYING…" : "VERIFY SIGNATURE"}
                </button>
            </div>
            <div className="mt-2 font-mono text-[10px] text-[#71717A] break-all" data-testid="attestation-hash">
                HASH {attestation.payload_hash?.slice(0, 32)}… · SIG {attestation.signature?.slice(0, 32)}…
            </div>
            {result === "valid" && (
                <div className="mt-2 flex items-center gap-1.5 text-[#00FF41] font-mono text-[10px] tracking-widest" data-testid="attestation-valid">
                    <ShieldCheck className="w-3.5 h-3.5" /> SIGNATURE VALID — TRACK RECORD IS TAMPER-FREE
                </div>
            )}
            {result === "invalid" && (
                <div className="mt-2 flex items-center gap-1.5 text-[#FF3B30] font-mono text-[10px] tracking-widest" data-testid="attestation-invalid">
                    <ShieldX className="w-3.5 h-3.5" /> SIGNATURE INVALID — DATA MAY HAVE BEEN ALTERED
                </div>
            )}
        </div>
    );
};
