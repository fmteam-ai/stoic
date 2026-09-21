import { useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Play, Snowflake, Globe, PowerOff, RotateCcw, Undo2, CheckCircle2, XCircle } from "lucide-react";

function Btn({ onClick, busy, danger, icon: I, children, testid, disabled }) {
    return (
        <button onClick={onClick} disabled={busy || disabled} data-testid={testid}
            className={`px-4 py-2.5 text-xs font-mono tracking-widest border flex items-center gap-2 disabled:opacity-40 ${
                danger ? "border-[#FF3B30]/50 text-[#FF3B30] hover:bg-[#FF3B30]/10" : "border-[#00FF41]/50 text-[#00FF41] hover:bg-[#00FF41]/10"}`}>
            {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <I className="w-3.5 h-3.5" />}{children}
        </button>
    );
}

const Check = ({ ok, label, testid }) => (
    <div className="flex items-center gap-2 text-xs" data-testid={testid}>
        {ok ? <CheckCircle2 className="w-3.5 h-3.5 text-[#00FF41]" /> : <XCircle className="w-3.5 h-3.5 text-[#FF3B30]" />}
        <span className={ok ? "text-[#A1A1AA]" : "text-[#E4E4E7]"}>{label}</span>
    </div>
);

export function GatePanel({ state, refresh }) {
    const [busy, setBusy] = useState(null);
    const [confirm, setConfirm] = useState("");
    const post = async (path, body, key, msg) => {
        setBusy(key);
        try { await api.post(`/admin/host-migration/${path}`, body); if (msg) toast.success(msg); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(null); setConfirm(""); refresh(); }
    };
    const src = state.source || {};
    const cut = state.facts?.cutover;
    const aw = state.awaiting;
    const running = state.status === "running";

    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-5 space-y-4" data-testid="hm-gate">
            <div className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">NEXT ACTION</div>

            {running && <div className="text-xs text-[#FFB020] font-mono flex items-center gap-2" data-testid="hm-running">
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> {state.current_step?.replaceAll("_", " ")} in progress — you can leave this page; the sidecar keeps working.</div>}

            {aw === "install" && !running && (
                <>
                    <p className="text-xs text-[#A1A1AA]">Preflight passed. Starting will install STOIC on <b className="text-[#E4E4E7]">{state.target.host}</b>
                        {" "}(same release {src.tag || src.sha?.slice(0, 10)}, same profile), copy secrets/env/backups/TLS certs and run a full <b className="text-[#E4E4E7]">warm</b> data sync.
                        The source keeps trading — nothing is stopped in this phase.</p>
                    <Btn onClick={() => post("start", {}, "start", "Migration started")} busy={busy === "start"} icon={Play} testid="hm-start-btn">START INSTALL + WARM SYNC</Btn>
                </>
            )}

            {aw === "freeze" && !running && (
                <>
                    <p className="text-xs text-[#A1A1AA]">New host installed and warm-synced. The next step <b className="text-[#FF3B30]">stops the source API and workers</b>,
                        copies the final delta and brings the new host's workers up. Downtime is typically under a few minutes; EAs reconnect automatically once DNS moves.
                        Best done in a quiet market window with no open positions.</p>
                    <input value={confirm} onChange={e => setConfirm(e.target.value)} placeholder='type FREEZE to confirm' data-testid="hm-freeze-confirm"
                        className="w-full bg-[#050505] border border-[#1F1F1F] px-3 py-2 text-xs font-mono text-[#E4E4E7] outline-none focus:border-[#FF3B30]/50" />
                    <Btn danger disabled={confirm !== "FREEZE"} onClick={() => post("advance", { step: "freeze" }, "freeze", "Source frozen — final sync running")}
                        busy={busy === "freeze"} icon={Snowflake} testid="hm-freeze-btn">FREEZE SOURCE + FINAL SYNC</Btn>
                </>
            )}

            {aw === "cutover_check" && !running && (
                <>
                    <p className="text-xs text-[#A1A1AA]">New host verified (release-readiness green). Now point <b className="text-[#E4E4E7]">{src.domain || "your domain"}</b> at
                        {" "}<b className="text-[#E4E4E7]">{state.target.public_ip || state.target.host}</b> (Cloudflare → DNS → A record; keep the proxy on). Then re-run the check until EA heartbeats arrive on the new host.</p>
                    {cut && (
                        <div className="space-y-1.5 border border-[#1F1F1F] p-3" data-testid="hm-cutover-result">
                            <Check ok={cut.target_serves_domain_tls} label={`New host answers https://${cut.domain} with a valid certificate`} testid="hm-cut-tls" />
                            <Check ok={cut.public_health} label="Public https://domain/api/health responds" testid="hm-cut-public" />
                            <Check ok={cut.dns_points_to_target || cut.dns_proxied} label={`DNS resolves (${(cut.dns || []).join(", ") || "unresolved"})${cut.dns_proxied ? " — proxied, IP hidden" : ""}`} testid="hm-cut-dns" />
                            <Check ok={cut.ea_heartbeats_on_target > 0} label={`EA heartbeats on new host since freeze: ${cut.ea_heartbeats_on_target} (source had ${cut.connected_accounts_at_source ?? "?"} connected)`} testid="hm-cut-ea" />
                        </div>
                    )}
                    <div className="flex flex-wrap gap-2">
                        <Btn onClick={() => post("advance", { step: "cutover_check" }, "cut")} busy={busy === "cut"} icon={Globe} testid="hm-cutover-check-btn">CHECK CUTOVER</Btn>
                    </div>
                </>
            )}

            {aw === "decommission" && !running && (
                <>
                    <p className="text-xs text-[#A1A1AA]">EAs are reporting to the new host. Decommissioning <b className="text-[#FF3B30]">stops every container on this (old) server</b>.
                        Data stays on disk for rollback; nothing is deleted.</p>
                    <input value={confirm} onChange={e => setConfirm(e.target.value)} placeholder='type DECOMMISSION to confirm' data-testid="hm-decom-confirm"
                        className="w-full bg-[#050505] border border-[#1F1F1F] px-3 py-2 text-xs font-mono text-[#E4E4E7] outline-none focus:border-[#FF3B30]/50" />
                    <Btn danger disabled={confirm !== "DECOMMISSION"} onClick={() => post("advance", { step: "decommission" }, "decom", "Source decommissioned")}
                        busy={busy === "decom"} icon={PowerOff} testid="hm-decommission-btn">DECOMMISSION SOURCE</Btn>
                </>
            )}

            {state.status === "done" && <div className="text-sm text-[#00FF41] font-mono" data-testid="hm-done">✓ MIGRATION COMPLETE — this server is stopped. Log in at https://{src.domain} (new host) and run <code>make migrator-off</code> there.</div>}
            {state.status === "aborted" && <div className="text-sm text-[#FFB020] font-mono" data-testid="hm-aborted">Migration aborted — source restored. Run a new preflight to try again.</div>}
            {state.status === "failed" && <div className="text-xs text-[#FF3B30] font-mono break-all" data-testid="hm-error">{state.error}</div>}

            {(state.status === "failed" || (aw && aw !== "install")) && !running && (
                <div className="flex flex-wrap gap-2 pt-2 border-t border-[#1F1F1F]">
                    {state.status === "failed" && <Btn onClick={() => post("retry", {}, "retry", "Retrying failed step")} busy={busy === "retry"} icon={RotateCcw} testid="hm-retry-btn">RETRY FAILED STEP</Btn>}
                    <Btn danger onClick={() => post("abort", {}, "abort", "Aborted — source restarted if it was frozen")} busy={busy === "abort"} icon={Undo2} testid="hm-abort-btn">ABORT · RESTORE SOURCE</Btn>
                </div>
            )}
        </div>
    );
}
