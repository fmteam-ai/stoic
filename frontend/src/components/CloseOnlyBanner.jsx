import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Ban, ChevronDown } from "lucide-react";

/** main128 — one-line "why am I close-only" notice built from the caller's own `per_user_blocks`. Renders nothing when clean. */
export function CloseOnlyBanner({ refreshKey = 0 }) {
    const [notice, setNotice] = useState(null);
    const [open, setOpen] = useState(false);
    useEffect(() => {
        let alive = true;
        api.get("/accounts/close-only-notice").then((r) => { if (alive) setNotice(r.data); }).catch(() => { if (alive) setNotice(null); });
        return () => { alive = false; };
    }, [refreshKey]);
    if (!notice?.blocked) return null;
    return (
        <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-4 py-2 font-mono text-xs" data-testid="close-only-banner">
            <div className="flex items-center gap-2 flex-wrap">
                <Ban className="w-3.5 h-3.5 text-[#FF3B30] shrink-0" />
                <span className="text-[#FF3B30]" data-testid="close-only-banner-line">{notice.line}</span>
                <span className="text-[#A1A1AA]">→ {notice.fix?.label}</span>
                {notice.reasons.length > 1 && (
                    <button onClick={() => setOpen((o) => !o)} data-testid="close-only-banner-toggle"
                        className="ml-auto flex items-center gap-1 text-[#71717A] hover:text-white">
                        {open ? "hide" : `all ${notice.reasons.length}`} <ChevronDown className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} />
                    </button>
                )}
            </div>
            {open && (
                <ul className="mt-2 ml-5 space-y-0.5 text-[#A1A1AA]" data-testid="close-only-banner-reasons">
                    {notice.reasons.map((r, i) => <li key={i}>· {r}</li>)}
                </ul>
            )}
            <div className="mt-1 ml-5 text-[10px] text-[#52525B]">Open positions are still managed; only new entries are held until this is fixed{notice.policy_version ? ` · rules policy ${notice.policy_version}` : ""}.</div>
        </div>
    );
}
