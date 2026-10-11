import { useEffect, useState } from "react";
import api, { formatApiError, API } from "@/lib/api";
import { toast } from "sonner";
import { Copy, Loader2, RefreshCw, Server, ShieldCheck, Download } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { WebRequestGuide } from "@/components/WebRequestGuide";

const TITLES = {
    install: ["Connect this account", "One line on the MT5 host — the installer picks the terminal logged into your login, deploys the EA, token and server URL (~60 s)."],
    repair: ["Repair the connection", "Old token file, EA not attached, stale install — one line replaces the files, re-attaches the EA and restarts MT5."],
    trust: ["Trust this terminal", "The EA is already heartbeating from this MT5 login. One click unlocks full trading authority — no PowerShell needed."],
    adopt_server: ["Use the server the EA reports", "Same MT5 login, different server NAME — a registration typo, not a wrong terminal. Step-up MFA; audited."],
    webrequest: ["Allow the URL in MT5", "MetaTrader keeps its WebRequest allow-list encrypted — only you can add it."],
    operator: ["Operator approval needed", "Real-money accounts are admitted by the STOIC operator, never automatically."],
    wait: ["Nothing to do yet", "The next heartbeat (≤ 60 s) carries what is missing. This line updates by itself."],
};

const copy = async (text, label) => {
    try { await navigator.clipboard.writeText(text); toast.success(`${label} copied`); }
    catch { toast.error("Clipboard blocked — select & copy manually."); }
};

function CommandBlock({ account, repair }) {
    const [line, setLine] = useState(null);
    const [err, setErr] = useState(null);
    const backendBase = API.replace(/\/api$/, "");
    const generate = () => {
        setLine(null); setErr(null);
        api.post("/setup/pairing-token", { account_id: account.id, repair })
            .then((r) => setLine(r.data))
            .catch((e) => setErr(formatApiError(e)));
    };
    useEffect(generate, [account.id, repair]);
    if (err) return <div className="text-xs text-[#FF3B30] font-mono" data-testid={`fix-dialog-error-${account.id}`}>{err} <button onClick={generate} className="underline ml-1">retry</button></div>;
    if (!line) return <div className="text-xs text-[#71717A] font-mono flex items-center gap-1" data-testid={`fix-dialog-generating-${account.id}`}><Loader2 className="w-3 h-3 animate-spin" /> generating a hash-pinned one-liner…</div>;
    const cmdUrl = `${backendBase}/api/setup/installer.cmd?token=${encodeURIComponent(line.token)}&login=${encodeURIComponent(line.mt5_login || "")}&repair=${repair ? 1 : 0}`;
    return (
        <div className="space-y-2">
            <div className="flex items-start gap-2">
                <code className="flex-1 text-[10px] font-mono text-[#FFD700] bg-black border border-[#1F1F1F] p-2 break-all select-all" data-testid={`fix-dialog-command-${account.id}`}>{line.install_command}</code>
                <button onClick={() => copy(line.install_command, repair ? "Repair line" : "Install line")} data-testid={`fix-dialog-copy-${account.id}`}
                    className="p-1.5 border border-[#1F1F1F] hover:border-[#FFD700] text-[#A1A1AA] hover:text-[#FFD700]" title="Copy"><Copy className="w-3.5 h-3.5" /></button>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono text-[#71717A]">
                <span>Paste in PowerShell on the MT5 host · valid {repair ? "15" : "60"} min</span>
                <a href={cmdUrl} data-testid={`fix-dialog-cmd-${account.id}`} className="text-[#FFD700] underline flex items-center gap-1"><Download className="w-3 h-3" /> or download STOIC-Setup.cmd</a>
                <button onClick={generate} data-testid={`fix-dialog-regenerate-${account.id}`} className="flex items-center gap-1 hover:text-white"><RefreshCw className="w-3 h-3" /> new line</button>
            </div>
            {line.installer_sha256 && <div className="text-[10px] font-mono text-[#52525B]" data-testid={`fix-dialog-hash-${account.id}`}>INSTALLER SHA-256 {line.installer_sha256.slice(0, 16)}…{line.installer_sha256.slice(-8)} — verified before it runs</div>}
            <div className="text-[11px] text-[#A1A1AA]">The status line above turns green by itself once the first heartbeat lands — keep this page open.</div>
        </div>
    );
}

function ActionButton({ account, testid, label, icon: Icon, request, okMsg, onDone }) {
    const [busy, setBusy] = useState(false);
    const run = async () => {
        setBusy(true);
        try { const r = await request(); toast.success(typeof okMsg === "function" ? okMsg(r) : okMsg); onDone(); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    return (
        <button onClick={run} disabled={busy} data-testid={`${testid}-${account.id}`}
            className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-bold py-2.5 text-sm font-mono tracking-widest flex items-center justify-center gap-2">
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Icon className="w-4 h-4" />} {label}
        </button>
    );
}

function WebRequestBody({ account }) {
    const [url, setUrl] = useState(null);
    useEffect(() => { api.get(`/setup/install-progress/${account.id}`).then((r) => setUrl(r.data.webrequest_url)).catch(() => setUrl(API.replace(/\/api$/, ""))); }, [account.id]);
    if (!url) return <div className="text-xs text-[#71717A] font-mono flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> loading…</div>;
    return <WebRequestGuide accountId={account.id} url={url} force />;
}

/** U15 — every StatusLine fix opens its dialog right on the card. `fix` = {action,label,why} from /status-line. */
export function StatusFixDialog({ account, fix, onClose, onDone }) {
    if (!fix) return null;
    const [title, desc] = TITLES[fix.action] || [fix.label, ""];
    const done = () => { onDone && onDone(); onClose(); };
    const reported = account.ea_identity?.broker_server;
    let body = null;
    if (fix.action === "install" || fix.action === "repair") body = <CommandBlock account={account} repair={fix.action === "repair"} />;
    else if (fix.action === "trust") body = (
        <div className="space-y-3">
            <div className="text-xs text-[#A1A1AA]">Heartbeating from MT5 login <code className="text-[#FFD700] font-mono">{String(account.broker_account_id_reported ?? account.account_number)}</code>{account.ea_identity?.ea_version ? <> · EA v{account.ea_identity.ea_version}</> : null}.</div>
            <ActionButton account={account} testid="fix-dialog-trust" label="TRUST THIS TERMINAL" icon={ShieldCheck} onDone={done}
                request={() => api.post(`/accounts/${account.id}/trust-terminal`)} okMsg="Terminal trusted — full authority restores within a minute" />
        </div>
    );
    else if (fix.action === "adopt_server") body = (
        <div className="space-y-3">
            <div className="text-xs text-[#A1A1AA] leading-relaxed">Registered on <code className="text-[#FFD700] font-mono">{account.server || "—"}</code>{reported && <> · the EA reports <code className="text-[#00FF41] font-mono">{reported}</code></>}. The verified chain re-verifies on the next heartbeat.</div>
            <ActionButton account={account} testid="fix-dialog-adopt" label={`USE ${reported || "THE REPORTED SERVER"}`} icon={Server} onDone={done}
                request={() => api.post(`/accounts/${account.id}/adopt-reported-server`)} okMsg={(r) => `Server set to ${r.data.server}`} />
        </div>
    );
    else if (fix.action === "webrequest") body = <WebRequestBody account={account} />;
    else body = <div className="text-xs text-[#A1A1AA]" data-testid={`fix-dialog-info-${account.id}`}>{fix.why || desc}</div>;
    return (
        <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
            <DialogContent className="bg-[#0A0A0A] border-[#1F1F1F] text-white max-w-lg" data-testid={`fix-dialog-${account.account_number}`} data-action={fix.action}>
                <DialogHeader>
                    <DialogTitle className="font-display">{title}</DialogTitle>
                    <DialogDescription className="font-mono text-xs text-[#71717A]">{desc}{fix.why ? ` · ${fix.why}` : ""}</DialogDescription>
                </DialogHeader>
                {body}
            </DialogContent>
        </Dialog>
    );
}
