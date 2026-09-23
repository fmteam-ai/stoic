import { useEffect, useState } from "react";
import { RefreshCw, ServerCrash } from "lucide-react";

const RETRY_MS = (attempt) => Math.min(30000, 5000 * 2 ** Math.min(3, Math.max(0, attempt - 1)));

export function BackendOutage({ outage, onRetry }) {
    const [left, setLeft] = useState(RETRY_MS(outage.attempt) / 1000);
    useEffect(() => {
        setLeft(RETRY_MS(outage.attempt) / 1000);
        const id = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000);
        return () => clearInterval(id);
    }, [outage.attempt, outage.at]);
    return (
        <div className="min-h-screen flex items-center justify-center bg-[#050505] p-4" data-testid="backend-outage-screen">
            <div className="max-w-lg w-full bg-[#0A0A0A] border border-[#FF3B3B]/40 p-8">
                <div className="flex items-center gap-3 mb-4">
                    <ServerCrash className="w-8 h-8 text-[#FF3B3B]" />
                    <div className="font-display text-xl text-white">Backend unavailable</div>
                </div>
                <p className="text-sm text-[#A1A1AA] leading-6 mb-5">
                    This is a protected page. The session could not be verified because the STOIC API did not
                    answer — you are <span className="text-white">not</span> logged out and this is{" "}
                    <span className="text-white">not</span> the public site. Trading authority stays BLOCKED until
                    the backend is reachable again.
                </p>
                <dl className="font-mono text-xs text-[#A1A1AA] grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 mb-6">
                    <dt>STATUS</dt><dd className="text-white" data-testid="backend-outage-status">{outage.status ? `HTTP ${outage.status}` : "NETWORK / TIMEOUT"}</dd>
                    <dt>DETAIL</dt><dd className="text-white">{outage.message}</dd>
                    <dt>CORRELATION</dt><dd className="text-[#FFB000] break-all" data-testid="backend-outage-correlation-id">{outage.correlationId}</dd>
                    <dt>ATTEMPTS</dt><dd className="text-white" data-testid="backend-outage-attempts">{outage.attempt}</dd>
                    <dt>NEXT RETRY</dt><dd className="text-white" data-testid="backend-outage-retry-countdown">{left > 0 ? `in ${left}s` : "now…"}</dd>
                </dl>
                <div className="flex gap-3 flex-wrap">
                    <button type="button" onClick={onRetry} data-testid="backend-outage-retry-button"
                        className="inline-flex items-center gap-2 px-5 py-2.5 bg-[#00FF41] text-black text-xs font-mono tracking-widest hover:bg-[#33FF66] transition-colors">
                        <RefreshCw className="w-3.5 h-3.5" /> RETRY NOW
                    </button>
                    <a href="/status" data-testid="backend-outage-status-link"
                        className="inline-flex items-center px-5 py-2.5 border border-[#2A2A2A] text-[#A1A1AA] text-xs font-mono tracking-widest hover:border-[#A1A1AA] transition-colors">
                        PUBLIC STATUS PAGE →
                    </a>
                </div>
            </div>
        </div>
    );
}
