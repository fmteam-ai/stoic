import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Ban, ListChecks, Scale } from "lucide-react";

const Head = ({ icon: Icon, title, testid }) => (
    <div className="px-4 py-2.5 border-b border-[#1F1F1F] flex items-center gap-2" data-testid={testid}>
        <Icon className="w-3.5 h-3.5 text-[#00FF41]" />
        <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">{title}</span>
    </div>
);
const STATUS_CLS = {
    would_have_done: "text-[#FFB000]", refused_protected: "text-[#FF3B30]", refused_cap: "text-[#FF3B30]",
    pending: "text-[#3B82F6]", done: "text-[#00FF41]", undone: "text-[#71717A]",
};

export function WouldHaveDoneList({ onChanged }) {
    const [d, setD] = useState({ actions: [], rules: {} });
    const load = useCallback(async () => { try { setD((await api.get("/admin/security/actions?kind=containment&limit=50")).data); } catch { /* ignore */ } }, []);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);
    const undo = async (a) => {
        try { await api.post(`/admin/security/actions/${a.id}/undo`, { note: "undone from Ops Console" }); toast.success(`${a.rule} ${a.action} on ${a.target} undone`); await load(); onChanged?.(); }
        catch (e) { toast.error(formatApiError(e)); }
    };
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="security-actions-panel">
            <Head icon={Scale} title={`Rules R1–R8 · actions (${d.actions.length})`} />
            <div className="max-h-[320px] overflow-y-auto divide-y divide-[#141414]">
                {d.actions.length === 0 && <div className="p-4 text-xs text-[#52525B]" data-testid="security-actions-empty">No rule has met its proof threshold yet. Observe mode logs every proposal here before enforce mode is switched on.</div>}
                {d.actions.map(a => (
                    <div key={a.id} className="px-4 py-2 text-xs flex items-start gap-3" data-testid={`security-action-${a.rule}`}>
                        <span className="font-mono text-[10px] text-white shrink-0">{a.rule}</span>
                        <div className="min-w-0 flex-1">
                            <div className="text-[#E4E4E7] truncate">{a.action} → {a.target_kind} <span className="font-mono">{a.target}</span>{a.expires_min ? ` · ${a.expires_min} min` : ""}</div>
                            <div className="text-[11px] text-[#71717A] truncate">{d.rules?.[a.rule]?.title} · from {a.check_id} {a.dedup_key}{a.note ? ` · ${a.note}` : ""}</div>
                        </div>
                        <div className={`font-mono text-[10px] text-right shrink-0 ${STATUS_CLS[a.status] || "text-[#71717A]"}`}>
                            <div>{String(a.status).replace(/_/g, " ").toUpperCase()}</div><div className="text-[#52525B]">{String(a.at).slice(5, 16)}Z</div>
                        </div>
                        {a.status === "done" && a.undo && (
                            <button onClick={() => undo(a)} data-testid={`security-action-undo-${a.rule}`}
                                className="shrink-0 px-2 py-1 text-[9px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#FFB000]/60 hover:text-[#FFB000]">UNDO</button>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}

export function ActiveBlocksList({ onChanged }) {
    const [blocks, setBlocks] = useState([]);
    const load = useCallback(async () => { try { setBlocks((await api.get("/admin/security/blocks")).data.blocks || []); } catch { /* ignore */ } }, []);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);
    const act = async (b, kind) => {
        try {
            if (kind === "undo") await api.post(`/admin/security/actions/${b.action_id}/undo`, { note: "unblocked from Ops Console" });
            else {
                const m = window.prompt("Block should expire in how many minutes from now? (0 = now)", "60");
                if (m == null) return;
                await api.post(`/admin/security/actions/${b.action_id}/extend`, { minutes: Number(m) });
            }
            toast.success(kind === "undo" ? `${b.value} unblocked` : `${b.value} block updated`);
            await load(); onChanged?.();
        } catch (e) { toast.error(formatApiError(e)); }
    };
    const left = (s) => s == null ? "—" : s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${Math.max(1, Math.round(s / 60))}m`;
    const b = "px-2 py-1 text-[9px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] disabled:opacity-40";
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="security-blocks-panel">
            <Head icon={Ban} title={`Active blocks · ${blocks.length}`} />
            <div className="max-h-[260px] overflow-y-auto divide-y divide-[#141414]">
                {blocks.length === 0 && <div className="p-4 text-xs text-[#52525B]" data-testid="security-blocks-empty">No active IP blocks or account locks.</div>}
                {blocks.map(bl => (
                    <div key={bl.id} className="px-4 py-2 text-xs flex items-center gap-3" data-testid={`security-block-${bl.kind}`}>
                        <span className="font-mono text-[10px] text-[#FF3B30] shrink-0">{String(bl.kind).replace(/_/g, " ").toUpperCase()}</span>
                        <span className="font-mono text-white flex-1 truncate">{bl.value} <span className="text-[#52525B]">· {bl.scope} · {bl.rule}</span></span>
                        <span className="font-mono text-[10px] text-[#FFB000] shrink-0">{left(bl.seconds_left)} left</span>
                        <button className={b} disabled={!bl.action_id} onClick={() => act(bl, "extend")} data-testid="security-block-extend-btn">EXTEND</button>
                        <button className={b} disabled={!bl.action_id} onClick={() => act(bl, "undo")} data-testid="security-block-unblock-btn">UNBLOCK</button>
                    </div>
                ))}
            </div>
        </div>
    );
}

export function CheckStatusTable() {
    const [checks, setChecks] = useState([]);
    const load = useCallback(async () => { try { setChecks((await api.get("/admin/security/check-runs")).data.checks || []); } catch { /* ignore */ } }, []);
    useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
    const age = (at) => at ? Math.max(0, Math.round((Date.now() - new Date(at).getTime()) / 1000)) : null;
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="security-checks-panel">
            <Head icon={ListChecks} title={`Check status · ${checks.length} checks`} />
            <div className="max-h-[320px] overflow-y-auto">
                <table className="w-full text-[11px]">
                    <thead className="font-mono text-[9px] tracking-widest text-[#52525B] uppercase sticky top-0 bg-[#0A0A0A]">
                        <tr><th className="text-left px-4 py-1.5">Check</th><th className="text-left">Every</th><th className="text-left">Last run</th><th className="text-right">ms</th><th className="text-right">Found</th><th className="text-right px-4">Result</th></tr>
                    </thead>
                    <tbody className="divide-y divide-[#141414]">
                        {checks.map(c => {
                            const a = age(c.at);
                            const stale = a != null && a > c.interval_s * 3 + 120;
                            const cls = c.status === "failed" ? "text-[#FF3B30]" : c.status === "skipped" ? "text-[#71717A]" : !c.at || stale ? "text-[#FFB000]" : "text-[#00FF41]";
                            return (
                                <tr key={c.check_id} data-testid={`security-check-${c.check_id}`}>
                                    <td className="px-4 py-1 font-mono text-white">{c.check_id}</td>
                                    <td className="text-[#71717A]">{c.interval_s >= 3600 ? `${c.interval_s / 3600}h` : `${c.interval_s / 60}m`}</td>
                                    <td className="text-[#71717A]">{a == null ? "never" : a < 90 ? `${a}s ago` : `${Math.round(a / 60)}m ago`}</td>
                                    <td className="text-right font-mono text-[#A1A1AA]">{c.duration_ms ?? "—"}</td>
                                    <td className="text-right font-mono text-[#A1A1AA]">{c.findings ?? "—"}</td>
                                    <td className={`text-right px-4 font-mono ${cls}`}>{c.status === "failed" ? "FAILED" : c.status === "skipped" ? "SKIPPED" : !c.at ? "SILENT" : stale ? "STALE" : "OK"}</td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
