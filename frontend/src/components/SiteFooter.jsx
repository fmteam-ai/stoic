import { useEffect, useState } from "react";
import api from "@/lib/api";

export function SiteFooter() {
    const [rel, setRel] = useState(null);
    useEffect(() => { api.get("/health").then((r) => setRel(r.data?.release || null)).catch(() => setRel(null)); }, []);
    return (
        <footer
            className="border-t border-[#1F1F1F] bg-[#050505] px-4 md:px-8 py-5 text-center space-y-1"
            data-testid="site-footer"
        >
            <p className="text-xs text-[#71717A]">
                © 2026 STOIC AI Technologies. All rights reserved.
            </p>
            {rel?.id ? (
                <p className="text-[10px] font-mono text-[#52525B] break-words" data-testid="footer-release-id" title={rel.attested ? "verified release attestation" : "running build (no release attestation on this host)"}>
                    release {rel.id}{rel.attested ? "" : " · unattested build"}
                </p>
            ) : null}
        </footer>
    );
}
