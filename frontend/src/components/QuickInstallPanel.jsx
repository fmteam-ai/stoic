import { useEffect, useState } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { Copy, Loader2, Terminal as TerminalIcon, CheckCircle2, RefreshCw, Server } from "lucide-react";
import { toast } from "sonner";
import { InstallProgressPanel } from "@/components/InstallProgressPanel";
import { ServerMismatchFix } from "@/components/ServerMismatchFix";
import { VpsAgentInstall } from "@/components/VpsAgentInstall";
import { VpsOfferButton } from "@/components/VpsOffer";

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
 * Token TTL is 60 min; on expiry, the user clicks Generate again (GENERATE = "New code").
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
    const [pin, setPin] = useState(null);           // { install_command, installer_sha256 } from the server (hash-pinned one-liner)
    const oneLiner = token
        ? (pin?.install_command || `irm ${backendBase}/api/setup/installer.ps1 | iex; Install-Stoic -Token "${token}" -ServerUrl "${backendBase}"`)
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

    const [repairLine, setRepairLine] = useState(null);   // U17 — one line that fixes any broken connection
    const generate = async (repair = false) => {
        setGenerating(true);
        try {
            const { data } = await api.post("/setup/pairing-token", { account_id: accountId, repair });
            if (repair) {
                setRepairLine({ command: data.install_command, expires_at: data.expires_at, cmdPath: data.cmd_download_path });
                toast.success("Repair line ready — run it on the MT5 host");
                return;
            }
            setToken(data.token);
            setExpiresAt(data.expires_at);
            setPin({ install_command: data.install_command, installer_sha256: data.installer_sha256, cmdPath: data.cmd_download_path });
            setPairedAt(null);
            setPairedHost(null);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setGenerating(false); }
    };
    // SEC-019-001 — the download URL carries an opaque owner-bound handle, never the pairing token
    const setupCmdUrl = (cmdPath) => `${backendBase}${cmdPath}`;
    const repairSection = (
        <div className="mt-3 border border-[#1F1F1F] p-3" data-testid={`repair-section-${accountId}`}>
            <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-xs text-[#A1A1AA]"><span className="text-white font-medium">Repair</span> — wrong token file, EA not attached, old install: one line replaces the files, re-attaches the EA and restarts MT5. No questions asked.</div>
                <button onClick={() => generate(true)} disabled={generating} data-testid={`repair-btn-${accountId}`}
                    className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#FFD700]/60 text-[#FFD700] hover:bg-[#FFD700]/10 disabled:opacity-50 flex items-center gap-1.5">
                    {generating ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} REPAIR
                </button>
            </div>
            {repairLine && (
                <div className="mt-2">
                    <div className="flex items-start gap-2">
                        <code className="flex-1 text-[10px] font-mono text-[#FFD700] bg-black border border-[#1F1F1F] p-2 break-all select-all" data-testid={`repair-line-${accountId}`}>{repairLine.command}</code>
                        <button onClick={() => copy(repairLine.command, "Repair line")} data-testid={`repair-copy-${accountId}`} className="p-1.5 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]" title="Copy"><Copy className="w-3.5 h-3.5" /></button>
                    </div>
                    <div className="mt-1 text-[11px] font-mono text-[#71717A]">Paste in PowerShell on the MT5 host (valid 15 min){repairLine.cmdPath && <> · or <a className="text-[#FFD700] underline" href={setupCmdUrl(repairLine.cmdPath)} data-testid={`repair-cmd-${accountId}`}>download STOIC-Setup.cmd</a> and double-click it there (link valid 10 min)</>}.</div>
                </div>
            )}
        </div>
    );

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
                <ServerMismatchFix account={account} onFixed={onTrusted} />
                {trustSection}
                {repairSection}
                <div className="flex items-center gap-2 mb-2">
                    <CheckCircle2 className="w-5 h-5 text-[#00FF41]" />
                    <div className="font-display font-bold tracking-wide text-white">Paired — EA deployed</div>
                </div>
                <div className="text-sm text-[#A1A1AA] space-y-1">
                    <div>Paired host: <code className="text-[#FFD700] font-mono">{pairedHost || "—"}</code></div>
                    <div className="text-xs text-[#52525B] mt-2">
                        If the Install Progress above is not green yet: in MT5 allow WebRequest for the URL above (Tools → Options → Expert Advisors) and make sure <strong>EmergentTradingBridge</strong> is on a chart with AutoTrading ON (green ▶). Leave the EA inputs at default — server URL, token and installation id are auto-loaded.
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
            <VpsAgentInstall accountId={accountId} />
            <ServerMismatchFix account={account} onFixed={onTrusted} />
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

                    {pin?.installer_sha256 && (
                        <div className="flex items-center justify-between gap-2 text-[10px] font-mono tracking-widest text-[#52525B]" data-testid="quick-install-hash-pin">
                            <span>INSTALLER SHA-256 <span className="text-[#A1A1AA] tracking-normal" data-testid="quick-install-hash-value">{pin.installer_sha256.slice(0, 16)}…{pin.installer_sha256.slice(-8)}</span> — the command verifies it before running</span>
                            <button onClick={() => copy(pin.installer_sha256, "Installer hash")} data-testid="quick-install-copy-hash"
                                className="p-1 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]" title="Copy full hash">
                                <Copy className="w-3 h-3" />
                            </button>
                        </div>
                    )}
                    <div className="text-xs text-[#A1A1AA] space-y-1.5">
                        <div><strong className="text-white">Step 2 —</strong> On the VPS, with MT5 <strong>open and logged in</strong>, open <strong>PowerShell as Administrator</strong> and paste. <span className="text-[#52525B]">No VPS yet?</span> <VpsOfferButton compact testid="quick-install-vps-offer-link" className="align-middle ml-1" />
                            {pin?.cmdPath && (
                                <span className="block mt-1" data-testid="quick-install-cmd-download">
                                    Prefer no typing? <a className="text-[#FFD700] underline" href={setupCmdUrl(pin.cmdPath)} data-testid={`quick-install-cmd-link-${accountId}`}>Download STOIC-Setup.cmd</a> and double-click it on the MT5 host (same hash-pinned command; link valid 10 min; SmartScreen may ask once — choose Run).
                                </span>
                            )}
                            <span className="block text-[#52525B]" data-testid="quick-install-terminalid-hint">The installer picks the MT5 that is logged into <code className="text-[#FFD700]">#{account?.account_number}</code> by itself (it asks only when none or several match). It deploys the EA, the server URL, your bridge token and the .ex5 — <strong className="text-[#00FF41]">~60 seconds</strong>. Advanced: <code className="text-[#FFD700]">-TerminalId &lt;32-hex folder&gt;</code>, <code className="text-[#FFD700]">-ChartSymbol EURUSD</code>.</span>
                        </div>
                        <div><strong className="text-white">Step 3 —</strong> If MT5 is closed, the installer starts it with the EA attached and Algo Trading on — nothing to click. If MT5 is open it asks before restarting (positions stay open at the broker but unmanaged during the restart; default <strong>N</strong> — then drag EmergentTradingBridge onto any chart yourself).</div>
                        <div><strong className="text-white">Step 4 —</strong> One MT5 setting nobody can write for you (MetaTrader keeps it encrypted): allow the WebRequest URL once. The Install Progress above shows the guided fix with a Copy button and turns green by itself on the first heartbeat.</div>
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
                        <RefreshCw className="w-3 h-3" /> NEW CODE
                    </button>
                </div>
            )}
        </div>
    );
}

export default QuickInstallPanel;
