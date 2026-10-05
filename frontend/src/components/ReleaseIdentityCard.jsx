import { useEffect, useState } from "react";
import { Fingerprint } from "lucide-react";
import api from "@/lib/api";

const short = (h) => (h ? `${String(h).slice(0, 12)}…${String(h).slice(-6)}` : "—");

// P1-05 (A9e) — what is RUNNING: build commit, image digest, shipped EA and the EX5 hashes
// this build admits for live terminals (current + previous during a rollout).
export function ReleaseIdentityCard() {
    const [d, setD] = useState(null);
    useEffect(() => {
        let live = true;
        Promise.all([api.get("/health"), api.get("/health/release")])
            .then(([h, rel]) => live && setD({ ...h.data, ...rel.data }))
            .catch(() => live && setD(null));
        return () => { live = false; };
    }, []);
    const ri = d?.release_identity || {};
    const row = "flex justify-between gap-3 py-1.5 border-b border-[#141414] last:border-0 font-mono text-[11px]";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="release-identity-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Fingerprint className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Release Identity</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">RUNNING BUILD · EA PROOF</span>
            </div>
            <div className="px-4 py-2">
                <div className={row}><span className="text-[#71717A]">BUILD SHA</span><span className="text-[#E4E4E7]" data-testid="release-build-sha">{short(d?.build_sha)}</span></div>
                <div className={row}><span className="text-[#71717A]">IMAGE DIGEST</span><span className="text-[#E4E4E7]" data-testid="release-image-digest">{ri.image_digest ? short(ri.image_digest) : "not injected (local build)"}</span></div>
                <div className={row}><span className="text-[#71717A]">EA SHIPPED</span><span className="text-[#E4E4E7]" data-testid="release-ea-version">{ri.ea_shipped_version || d?.ea_version || "—"}</span></div>
                <div className={row}>
                    <span className="text-[#71717A]">EA EX5 ACCEPTED</span>
                    <span className={ri.ea_accepted_sha256s?.length ? "text-[#00FF41]" : "text-[#FF3B30]"} data-testid="release-ea-hashes">
                        {ri.ea_accepted_sha256s?.length ? ri.ea_accepted_sha256s.map(short).join(" · ") : "NONE — live accounts close-only (pin EA_RELEASE_SHA256)"}
                    </span>
                </div>
                <div className={row}><span className="text-[#71717A]">SOURCE</span><span className="text-[#A1A1AA]">{ri.ea_signed_record ? "signed release record" : ri.ea_accepted_sha256s?.length ? "env pin" : "—"}</span></div>
            </div>
        </div>
    );
}

export default ReleaseIdentityCard;
