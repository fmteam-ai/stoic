import { useState } from "react";
import { Copy, CheckCircle2, Loader2 } from "lucide-react";
import { toast } from "sonner";

/** U5 — MetaQuotes keeps the WebRequest allow-list encrypted: no installer, config file or startup ini can set
 *  it. So: one guided dialog, a Copy URL button, and a live check — the step turns green on the next heartbeat
 *  (the install-progress poll already runs every 4 s). Shown when the heartbeat step is blocked/warn for a
 *  WebRequest reason. */
export function WebRequestGuide({ accountId, url, step, force = false }) {
    const [copied, setCopied] = useState(false);
    const reason = `${step?.detail || ""} ${step?.hint || ""}`;
    const relevant = force || (step && step.status !== "done" && /WebRequest|4014|blocks the address|No request/i.test(reason) && !/REJECTED/i.test(step.detail || ""));
    if (!relevant || !url) return null;
    const copy = async () => {
        try { await navigator.clipboard.writeText(url); setCopied(true); toast.success("URL copied — paste it into MT5"); }
        catch { toast.error("Copy failed — select the URL and press Ctrl+C"); }
    };
    const live = step?.status === "done";
    return (
        <div className="mt-3 border border-[#FFB000]/40 bg-[#FFB000]/5 p-3" data-testid={`webrequest-guide-${accountId}`}>
            <div className="text-sm text-white font-medium mb-1">One MT5 setting only you can change</div>
            <div className="text-xs text-[#A1A1AA] mb-2">MetaTrader keeps its WebRequest list encrypted — the installer cannot write it. 40 seconds, once per terminal:</div>
            <ol className="text-xs text-[#D4D4D8] space-y-1 list-decimal pl-5 mb-3">
                <li>In MetaTrader 5: <span className="text-white">Tools → Options → Expert Advisors</span></li>
                <li>Tick <span className="text-white">Allow WebRequest for listed URL</span></li>
                <li>Click the empty line under it, paste the URL (button below), press <span className="text-white">Enter</span>, then <span className="text-white">OK</span></li>
                <li>Right-click the chart with <span className="text-white">EmergentTradingBridge</span> → Expert Advisors → Remove, then drag it on again (or restart MT5)</li>
            </ol>
            <div className="flex flex-wrap items-center gap-2">
                <code className="text-[#FFD700] font-mono text-xs select-all px-2 py-1 bg-black border border-[#1F1F1F]" data-testid={`webrequest-guide-url-${accountId}`}>{url}</code>
                <button onClick={copy} data-testid={`webrequest-guide-copy-${accountId}`}
                    className="px-3 py-1.5 text-xs font-mono tracking-widest bg-[#FFD700] text-black flex items-center gap-1.5">
                    <Copy className="w-3 h-3" /> {copied ? "COPIED" : "COPY URL"}
                </button>
                <span className="flex items-center gap-1 text-[11px] font-mono text-[#71717A]" data-testid={`webrequest-guide-live-${accountId}`}>
                    {live ? <><CheckCircle2 className="w-3 h-3 text-[#00FF41]" /> heartbeat received</> : <><Loader2 className="w-3 h-3 animate-spin" /> waiting for the first heartbeat (checks every 4 s)</>}
                </span>
            </div>
        </div>
    );
}
