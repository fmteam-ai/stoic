import { KeyRound } from "lucide-react";

const Pill = ({ ok, yes, no }) => (
    <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${ok === true ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10" : ok === false ? "border-[#FF3B30]/40 text-[#FF3B30] bg-[#FF3B30]/10" : "border-[#1F1F1F] text-[#71717A]"}`}>
        {ok === true ? yes : ok === false ? no : "N/A"}
    </span>
);

export const SignerHealthRow = ({ check }) => {
    const s = check?.signer || {};
    const external = s.mode === "external";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-3" data-testid="signer-health-row">
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 font-mono text-sm text-white">
                    <KeyRound className={`w-4 h-4 ${check?.status === "pass" ? "text-[#00FF41]" : check?.status === "fail" ? "text-[#FF3B30]" : "text-[#FFB000]"}`} />
                    EXTERNAL RELEASE SIGNER
                </div>
                <Pill ok={check?.status === "pass" ? true : check?.status === "fail" ? false : null} yes="REACHABLE · KEY MATCH" no="UNAVAILABLE" />
            </div>
            <div className="font-mono text-[11px] text-[#A1A1AA] grid sm:grid-cols-2 gap-x-6 gap-y-1" data-testid="signer-health-detail">
                <div>mode: <span className="text-white" data-testid="signer-health-mode">{s.mode || "—"}</span></div>
                <div>host: <span className="text-white" data-testid="signer-health-host">{s.host || "—"}</span></div>
                <div>remote key_id: <span className="text-white">{s.remote_key_id || "—"}</span></div>
                <div>pinned key_id: <span className="text-white">{s.pinned_key_id || "—"}</span></div>
                <div>pinned public key: <span className="text-white">{s.pinned_public_key_prefix ? `${s.pinned_public_key_prefix}…` : "—"}</span></div>
                <div className="flex items-center gap-2">pinned-key match: <Pill ok={s.identity_matches ?? null} yes="MATCH" no="MISMATCH" /></div>
                <div>latency: <span className="text-white">{s.latency_ms != null ? `${s.latency_ms} ms` : "—"}</span></div>
                {(s.error || !external) && <div className="sm:col-span-2 text-[#FFB000]" data-testid="signer-health-note">{s.error || check?.current}</div>}
            </div>
            {check?.status !== "pass" && <div className="text-xs text-[#71717A] leading-relaxed">{check?.fix}</div>}
        </div>
    );
};
