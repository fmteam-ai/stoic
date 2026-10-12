import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { CheckCircle2, XCircle, Copy, Loader2, RefreshCw } from "lucide-react";

/** A5 — Go-Live checklist: green/red rows, each red row with ONE fix (button, command to copy or link). */
export function GoLiveChecklist() {
    const [data, setData] = useState(null);
    const [busy, setBusy] = useState("");
    const load = () => api.get("/ops/go-live-checklist").then((r) => setData(r.data)).catch((e) => toast.error(formatApiError(e)));
    useEffect(() => { load(); }, []);
    const act = async (row) => {
        const fix = row.fix;
        if (fix.kind === "command") { try { await navigator.clipboard.writeText(fix.command); toast.success("Command copied"); } catch { toast.error("Select & copy the command"); } return; }
        if (fix.kind === "link") { window.location.href = fix.href; return; }
        if (fix.action === "load_latest_policy") {
            setBusy(row.id);
            try { const { data: r } = await api.post("/authority/policy/load-latest"); toast[r.activated ? "success" : "info"](r.activated ? `Rules policy ${r.policy_version} active` : r.reason); await load(); }
            catch (e) { toast.error(formatApiError(e)); } finally { setBusy(""); }
        }
    };
    return (
        <section className="rounded-xl border border-white/10 bg-black/40 p-4 space-y-3" data-testid="go-live-checklist">
            <div className="flex items-center justify-between gap-2">
                <div className="font-mono text-sm text-white flex items-center gap-2">GO-LIVE CHECKLIST
                    {data && <span className={`text-[10px] px-1.5 py-0.5 border ${data.ok ? "border-[#00FF41]/50 text-[#00FF41]" : "border-[#FF3B30]/50 text-[#FF3B30]"}`} data-testid="go-live-overall">{data.ok ? "ALL GREEN" : `${data.rows.filter((r) => !r.ok).length} RED`}</span>}
                </div>
                <button onClick={load} className="text-white/50 hover:text-white" data-testid="go-live-refresh" title="Refresh"><RefreshCw className="h-3.5 w-3.5" /></button>
            </div>
            {!data ? <div className="text-xs text-white/50 font-mono flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" /> checking…</div> : (
                <ul className="divide-y divide-white/5">
                    {data.rows.map((r) => (
                        <li key={r.id} className="py-2 flex flex-wrap items-start gap-2" data-testid={`go-live-row-${r.id}`} data-ok={r.ok ? "1" : "0"}>
                            {r.ok ? <CheckCircle2 className="h-4 w-4 text-[#00FF41] mt-0.5 shrink-0" /> : <XCircle className="h-4 w-4 text-[#FF3B30] mt-0.5 shrink-0" />}
                            <div className="flex-1 min-w-[12rem]">
                                <div className="text-sm text-white">{r.label}</div>
                                <div className="text-[11px] font-mono text-white/50 break-all">{r.detail}</div>
                                {!r.ok && r.fix?.kind === "command" && <code className="block mt-1 text-[10px] font-mono text-[#FFD700] bg-black border border-white/10 p-1.5 break-all select-all" data-testid={`go-live-cmd-${r.id}`}>{r.fix.command}</code>}
                            </div>
                            {!r.ok && r.fix && (
                                <button onClick={() => act(r)} disabled={busy === r.id} data-testid={`go-live-fix-${r.id}`}
                                    className="px-2.5 py-1.5 text-[10px] font-mono tracking-widest bg-[#FFD700] text-black hover:bg-[#E6C200] disabled:opacity-50 flex items-center gap-1">
                                    {busy === r.id ? <Loader2 className="h-3 w-3 animate-spin" /> : r.fix.kind === "command" ? <Copy className="h-3 w-3" /> : null}
                                    {(r.fix.kind === "command" ? "COPY COMMAND" : r.fix.label).toUpperCase()}
                                </button>
                            )}
                        </li>
                    ))}
                </ul>
            )}
        </section>
    );
}
