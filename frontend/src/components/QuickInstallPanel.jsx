import { useEffect, useState } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { Copy, Loader2, Terminal as TerminalIcon, CheckCircle2, RefreshCw, Server } from "lucide-react";
import { toast } from "sonner";
import { InstallProgressPanel } from "@/components/InstallProgressPanel";

/**
 * QuickInstallPanel — one-stop pairing UX for the PowerShell auto-installer.
 *
 * Flow:
 *   1. User clicks "Generate token" → POST /api/setup/pairing-token
 *   2. Panel shows a copy-able PowerShell one-liner the user runs on their
 *      MT5 host (their PC or a broker VPS).
 *   3. We poll /api/setup/pairing-status/{accountId} every 4s until
 *      `installer_paired_at` flips — then show the success state.
 *
 * Token TTL is 15 min; on expiry, the user clicks Generate again.
 */
export function QuickInstallPanel({ accountId, accountLabel, account, onTrusted }) {
    const [token, setToken] = useState(null);
    const [expiresAt, setExpiresAt] = useState(null);
    const [generating, setGenerating] = useState(false);
    const [pairedAt, setPairedAt] = useState(null);
    const [pairedHost, setPairedHost] = useState(null);
    const [countdown, setCountdown] = useState(null);
    const [trusting, setTrusting] = useState(false);
    const [justTrusted, setJustTrusted] = useState(false);

    const backendBase = API.replace(/\/api$/, "");
    const oneLiner = token
        ? `irm ${backendBase}/api/setup/installer.ps1 | iex; Install-Stoic -Token "${token}" -ServerUrl "${backendBase}"`
        : "";

    // Poll for pairing completion
    useEffect(() => {
        if (!accountId) return;
        let mounted = true;
        const fetchStatus = async () => {
            try {
                const { data } = await api.get(`/setup/pairing-status/${accountId}`);
                if (!mounted) return;
                setPairedAt(data.installer_paired_at);
                setPairedHost(data.installer_paired_hostname);
            } catch {/* silent */}
        };
        fetchStatus();
        const t = setInterval(fetchStatus, 4000);
        return () => { mounted = false; clearInterval(t); };
    }, [accountId, token]);

    // Token countdown
    useEffect(() => {
        if (!expiresAt) { setCountdown(null); return; }
        const tick = () => {
            const diff = Math.max(0, Math.floor((new Date(expiresAt) - new Date()) / 1000));
            setCountdown(diff);
            if (diff === 0) { setToken(null); setExpiresAt(null); }
        };
        tick();
        const t = setInterval(tick, 1000);
        return () => clearInterval(t);
    }, [expiresAt]);

    const generate = async () => {
        setGenerating(true);
        try {
            const { data } = await api.post("/setup/pairing-token", { account_id: accountId });
            setToken(data.token);
            setExpiresAt(data.expires_at);
            setPairedAt(null);
            setPairedHost(null);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setGenerating(false); }
    };

    const copy = async (text, label) => {
        try {
            await navigator.clipboard.writeText(text);
            toast.success(`${label} copied to clipboard.`);
        } catch {
            toast.error("Clipboard blocked — select & copy manually.");
        }
    };

    // iter-172 — one-click trusted terminal: when the EA is ALREADY
    // heartbeating but unverified, no PowerShell is needed.
    const hbAge = account?.last_heartbeat
        ? (Date.now() - new Date(account.last_heartbeat).getTime()) / 1000
        : null;
    const eaConnected = hbAge !== null && hbAge < 180;
    const verified = justTrusted || !!account?.verified_identity;
    const trustEligible = !verified && eaConnected
        && account?.broker_account_id_reported != null
        && !account?.broker_account_mismatch;

    const trustTerminal = async () => {
        setTrusting(true);
        try {
            await api.post(`/accounts/${accountId}/trust-terminal`);
            setJustTrusted(true);
            toast.success("Terminal trusted — full trading authority restores within a minute");
            onTrusted && onTrusted();
        } catch (e) {
            toast.error("Could not trust terminal", { description: formatApiError(e) });
        } finally { setTrusting(false); }
    };

    const trustSection = verified ? (
        <div className="mb-3 flex items-center gap-2 font-mono text-xs text-[#00FF41]"
             data-testid={`terminal-verified-${accountId}`}>
            <CheckCircle2 className="w-4 h-4" />
            TERMINAL VERIFIED — FULL TRADING AUTHORITY
        </div>
    ) : trustEligible ? (
        <div className="mb-4 border border-[#00FF41]/40 bg-[#00FF41]/5 p-3"
             data-testid={`quick-trust-panel-${accountId}`}>
            <div className="text-sm text-white font-medium mb-1">
                EA already connected — no PowerShell needed
            </div>
            <div className="text-xs text-[#A1A1AA] mb-2.5">
                This terminal is heartbeating from MT5 login{" "}
                <code className="text-[#FFD700] font-mono">{String(account.broker_account_id_reported)}</code>.
                Trust it with one click to unlock full trading authority.
            </div>
            <button
                onClick={trustTerminal}
                disabled={trusting}
                data-testid={`quick-trust-btn-${accountId}`}
                className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-bold py-2.5 text-sm font-mono tracking-widest transition-colors"
            >
                {trusting ? "TRUSTING…" : "TRUST THIS TERMINAL — ONE CLICK"}
            </button>
        </div>
    ) : null;

    if (pairedAt) {
        return (
            <div className="border border-[#00FF41]/30 bg-[#00FF41]/5 p-4" data-testid="quick-install-paired">
                <InstallProgressPanel accountId={accountId} />
                {trustSection}
                <div className="flex items-center gap-2 mb-2">
                    <CheckCircle2 className="w-5 h-5 text-[#00FF41]" />
                    <div className="font-display font-bold tracking-wide text-white">Paired — EA deployed</div>
                </div>
                <div className="text-sm text-[#A1A1AA] space-y-1">
                    <div>Paired host: <code className="text-[#FFD700] font-mono">{pairedHost || "—"}</code></div>
                    <div className="text-xs text-[#52525B] mt-2">
                        Open MT5 on this host: Tools → Options → Expert Advisors → allow WebRequest for the URL above (once per terminal), then drag <strong>EmergentTradingBridge</strong> from Navigator → Experts onto any chart. AutoTrading must be ON (green ▶).
                    </div>
                </div>
                <button
                    onClick={generate}
                    disabled={generating}
                    data-testid="quick-install-repair-btn"
                    className="mt-3 px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#52525B] text-[#A1A1AA] flex items-center gap-1.5"
                >
                    <RefreshCw className="w-3 h-3" /> RE-PAIR (NEW VPS)
                </button>
            </div>
        );
    }

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="quick-install-panel">
            <InstallProgressPanel accountId={accountId} />
            {trustSection}
            <div className="flex items-center gap-2 mb-2">
                <TerminalIcon className="w-5 h-5 text-[#FFD700]" />
                <div className="font-display font-bold tracking-wide text-white">Quick Install (Windows VPS / PC)</div>
            </div>
            <p className="text-sm text-[#A1A1AA] mb-4">
                {trustEligible
                    ? "Prefer the strongest pairing instead? Generate a one-time token and paste a single PowerShell command on your MT5 host."
                    : "Skip MetaEditor and the F7 compile. Generate a one-time token, paste a single PowerShell command on your MT5 host, then allow the WebRequest URL once in MT5 (the installer prints it)."}
            </p>

            {!token ? (
                <button
                    onClick={generate}
                    disabled={generating}
                    data-testid="quick-install-generate-btn"
                    className="w-full bg-[#FFD700] hover:bg-[#E5C200] disabled:opacity-50 text-black font-medium py-2.5 text-sm transition-colors flex items-center justify-center gap-2"
                >
                    {generating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Server className="w-4 h-4" />}
                    {generating ? "GENERATING..." : "GENERATE PAIRING TOKEN"}
                </button>
            ) : (
                <div className="space-y-3">
                    <div className="flex items-center justify-between text-xs font-mono tracking-widest">
                        <span className="text-[#52525B]">TOKEN VALID FOR</span>
                        <span className={countdown < 60 ? "text-red-400" : "text-[#FFD700]"}>
                            {countdown ? `${Math.floor(countdown/60)}m ${countdown%60}s` : "EXPIRED"}
                        </span>
                    </div>

                    <div className="border border-[#1F1F1F] bg-[#050505] p-3">
                        <div className="text-[10px] font-mono tracking-widest text-[#52525B] mb-1.5">
                            STEP 1 — COPY THIS POWERSHELL ONE-LINER
                        </div>
                        <div className="flex items-start gap-2">
                            <pre className="flex-1 text-xs text-[#00FF41] font-mono whitespace-pre-wrap break-all leading-5"
                                 data-testid="quick-install-oneliner">{oneLiner}</pre>
                            <button
                                onClick={() => copy(oneLiner, "Install command")}
                                data-testid="quick-install-copy-cmd"
                                className="flex-shrink-0 p-1.5 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]"
                                title="Copy command"
                            >
                                <Copy className="w-3.5 h-3.5" />
                            </button>
                        </div>
                    </div>

                    <div className="text-xs text-[#A1A1AA] space-y-1.5">
                        <div><strong className="text-white">Step 2 —</strong> Open <strong>PowerShell as Administrator</strong> on your MT5 host (your PC or broker VPS) and paste.
                            <span className="block text-[#52525B]" data-testid="quick-install-terminalid-hint">Two MT5 terminals on that machine? Append <code className="text-[#FFD700]">-TerminalId &lt;32-hex folder name&gt;</code> (MT5: File → Open Data Folder) — or just answer the prompt.</span>
                        </div>
                        <div><strong className="text-white">Step 3 —</strong> The installer picks the MT5 terminal, deploys the EA, writes your bridge token, and installs the .ex5. <strong className="text-[#00FF41]">~60 seconds.</strong></div>
                        <div><strong className="text-white">Step 4 —</strong> In MT5: Tools → Options → Expert Advisors → allow WebRequest for the URL the installer prints; drag <strong>EmergentTradingBridge</strong> onto any chart; AutoTrading ON.</div>
                        <div className="text-[#52525B] mt-2">
                            This page will auto-detect when the installer completes.
                        </div>
                    </div>

                    <button
                        onClick={generate}
                        disabled={generating}
                        data-testid="quick-install-regenerate-btn"
                        className="w-full px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] hover:border-[#52525B] text-[#A1A1AA] flex items-center justify-center gap-1.5"
                    >
                        <RefreshCw className="w-3 h-3" /> REGENERATE TOKEN
                    </button>
                </div>
            )}
        </div>
    );
}

export default QuickInstallPanel;
