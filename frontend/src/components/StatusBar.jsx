import { ShieldCheck, Lock, Activity, Server } from "lucide-react";

/**
 * StatusBar — slim trust signal strip pinned above the main app area.
 *
 * Surfaces: service status · encryption · funds-stay-with-broker · uptime.
 * Pure presentational; no API calls. Optional `online` prop wires to ws state.
 */
export function StatusBar({ online = true }) {
    return (
        <div
            className="hidden md:flex items-center justify-between gap-4 border-b border-[#1F1F1F] bg-[#0A0A0A] px-6 py-1.5"
            data-testid="status-bar">
            <div className="flex items-center gap-5 font-mono text-[10px] tracking-widest text-[#52525B]">
                <span className="flex items-center gap-1.5" data-testid="status-operational">
                    <span className={`w-1.5 h-1.5 rounded-full ${online ? "bg-[#00FF41] pulse-dot" : "bg-[#FFB000]"}`} />
                    <span className={online ? "text-[#00FF41]" : "text-[#FFB000]"}>
                        {online ? "SERVICE · OPERATIONAL" : "DEGRADED"}
                    </span>
                </span>
                <span className="hidden lg:flex items-center gap-1.5">
                    <Lock className="w-2.5 h-2.5" />
                    ENCRYPTED LOGIN
                </span>
                <span className="hidden lg:flex items-center gap-1.5">
                    <Server className="w-2.5 h-2.5" />
                    EU SERVERS
                </span>
                <span className="hidden xl:flex items-center gap-1.5">
                    <Activity className="w-2.5 h-2.5" />
                    24/7 UPTIME
                </span>
            </div>
            <div className="flex items-center gap-2 font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                <ShieldCheck className="w-3 h-3 text-[#00FF41]" />
                <span>YOUR FUNDS STAY WITH YOUR BROKER</span>
                <span className="hidden xl:inline text-[#52525B]">· STOIC IS SOFTWARE, NOT A BROKER</span>
            </div>
        </div>
    );
}
