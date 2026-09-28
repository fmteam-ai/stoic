import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Loader2, Radar } from "lucide-react";

const STATUS = {
    watching: ["WATCHING", "border-[#FFB000]/40 text-[#FFB000] bg-[#FFB000]/10"],
    live: ["LIVE", "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10"],
    stalled: ["STALLED", "border-[#FF3B30]/40 text-[#FF3B30] bg-[#FF3B30]/10"],
    cancelled: ["CANCELLED", "border-[#1F1F1F] text-[#71717A] bg-transparent"],
};

const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

export const DeployWatchCard = ({ localSha }) => {
    const [data, setData] = useState(null);
    const [target, setTarget] = useState("https://www.stoicaibot.com");
    const [sha, setSha] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        try { setData((await api.get("/ops/deploy-watch")).data); setErr(""); }
        catch (e) { setErr(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);
    useEffect(() => {
        if (data?.watch?.status !== "watching") return undefined;
        const t = setInterval(load, 15000);
        return () => clearInterval(t);
    }, [data?.watch?.status, load]);
    useEffect(() => { if (localSha && !sha) setSha(localSha); }, [localSha, sha]);

    const act = async (path, body) => {
        setBusy(true); setErr("");
        try { setData((d) => ({ ...(d || {}), watch: null })); await api.post(path, body); await load(); }
        catch (e) { setErr(formatApiError(e)); await load(); }
        finally { setBusy(false); }
    };

    const w = data?.watch;
    const [label, cls] = STATUS[w?.status] || ["IDLE", "border-[#1F1F1F] text-[#71717A]"];
    const watching = w?.status === "watching";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-3" data-testid="deploy-watch-card">
            <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 font-mono text-sm text-white">
                    <Radar className={`w-4 h-4 ${watching ? "text-[#FFB000] animate-pulse" : "text-[#52525B]"}`} />
                    DEPLOY WATCHDOG
                </div>
                <span className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${cls}`} data-testid="deploy-watch-status">{label}</span>
            </div>
            <p className="text-[11px] text-[#71717A] font-mono leading-relaxed">
                Polls <span className="text-white">{"<target>"}/api/health</span> every {data?.interval_sec ?? 30}s and e-mails you the moment the expected build_sha goes live (or a stall notice at timeout).
                {data && !data.workers_in_process && <span className="text-[#FFB000]"> · BACKGROUND_WORKERS_IN_PROCESS is off on this instance — polling will not run here.</span>}
            </p>
            {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="deploy-watch-error">{err}</div>}
            {w && (
                <div className="font-mono text-[11px] text-[#A1A1AA] grid sm:grid-cols-2 gap-x-6 gap-y-1" data-testid="deploy-watch-detail">
                    <div>target: <span className="text-white">{w.target_url}</span></div>
                    <div>expected: <span className="text-white">{w.expected_sha || "any new build"}</span></div>
                    <div>baseline sha: <span className="text-white">{w.baseline_sha || "—"}</span> (HTTP {w.baseline_http})</div>
                    <div>last poll: <span className="text-white">{fmt(w.last_poll_at)}</span> · HTTP {w.last?.http} · sha {w.last?.build_sha || "—"}{w.last?.error ? ` · ${w.last.error}` : ""}</div>
                    <div>polls: <span className="text-white">{w.polls}</span> · expires {fmt(w.expires_at)}</div>
                    <div>notify: <span className="text-white">{w.notify_email}</span>{w.notified && <span className={w.notified.ok ? " text-[#00FF41]" : " text-[#FF3B30]"} data-testid="deploy-watch-notified"> · mail {w.notified.ok ? "sent" : `failed: ${w.notified.error}`}</span>}</div>
                    {w.status === "live" && <div className="sm:col-span-2 text-[#00FF41]" data-testid="deploy-watch-live">LIVE at {fmt(w.went_live_at)} · sha {w.live_sha}</div>}
                    {w.ended_reason && w.status !== "live" && <div className="sm:col-span-2">ended: {w.ended_reason}</div>}
                </div>
            )}
            {!watching ? (
                <div className="flex flex-wrap gap-2 items-end">
                    <label className="flex-1 min-w-[220px] text-[10px] font-mono text-[#71717A]">TARGET ORIGIN
                        <input value={target} onChange={(e) => setTarget(e.target.value)} data-testid="deploy-watch-target-input"
                            className="mt-1 w-full bg-black border border-[#1F1F1F] px-2 py-1.5 text-xs text-white font-mono focus:border-[#00FF41]/60 outline-none" />
                    </label>
                    <label className="flex-1 min-w-[220px] text-[10px] font-mono text-[#71717A]">EXPECTED BUILD_SHA (blank = any new build)
                        <input value={sha} onChange={(e) => setSha(e.target.value)} data-testid="deploy-watch-sha-input" placeholder={localSha || "commit sha"}
                            className="mt-1 w-full bg-black border border-[#1F1F1F] px-2 py-1.5 text-xs text-white font-mono focus:border-[#00FF41]/60 outline-none" />
                    </label>
                    <button disabled={busy} data-testid="deploy-watch-arm-button"
                        onClick={() => act("/ops/deploy-watch/arm", { target_url: target, expected_sha: sha, timeout_h: 24 })}
                        className="px-4 py-2 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 transition disabled:opacity-50 flex items-center gap-2">
                        {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />} ARM WATCH
                    </button>
                </div>
            ) : (
                <button disabled={busy} data-testid="deploy-watch-cancel-button" onClick={() => act("/ops/deploy-watch/cancel", {})}
                    className="px-4 py-2 text-xs font-mono tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 transition disabled:opacity-50">
                    CANCEL WATCH
                </button>
            )}
        </div>
    );
};
