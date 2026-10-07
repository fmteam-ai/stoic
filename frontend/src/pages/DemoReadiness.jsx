import { useCallback, useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { CheckCircle2, XCircle, AlertTriangle, Info, RefreshCw, Printer, Loader2 } from "lucide-react";
import { AcceptanceBundleCard } from "@/components/AcceptanceBundleCard";
import { CryptoLiveBadge } from "@/components/CryptoLiveBadge";

const ICON = {
    pass: <CheckCircle2 className="w-4 h-4 text-[#00FF41]" />,
    fail: <XCircle className="w-4 h-4 text-[#FF3B30]" />,
    warn: <AlertTriangle className="w-4 h-4 text-[#FFB020]" />,
    info: <Info className="w-4 h-4 text-[#71717A]" />,
};

function CheckRow({ c }) {
    return (
        <div className="flex items-start gap-3 py-2 border-b border-[#141414] last:border-0" data-testid={`demo-check-${c.id}`}>
            <span className="mt-0.5">{ICON[c.status] || ICON.info}</span>
            <div className="flex-1 min-w-0">
                <div className="text-sm text-[#E4E4E7]">{c.title}</div>
                <div className="font-mono text-[11px] text-[#A1A1AA] break-words" data-testid={`demo-check-detail-${c.id}`}>{c.detail}</div>
                {c.hint && c.status !== "pass" && <div className="font-mono text-[10px] text-[#71717A] mt-0.5">→ {c.hint}</div>}
            </div>
            <span className="font-mono text-[10px] tracking-widest uppercase text-[#71717A]">{c.status}</span>
        </div>
    );
}

function Flag({ ok }) {
    return ok ? <CheckCircle2 className="w-3.5 h-3.5 text-[#00FF41] inline" /> : <XCircle className="w-3.5 h-3.5 text-[#FF3B30] inline" />;
}

function FleetTable({ rows }) {
    if (!rows?.length) return <div className="font-mono text-[11px] text-[#71717A] py-3">No demo account is enabled or heartbeating in the last 24 h.</div>;
    return (
        <div className="overflow-x-auto">
            <table className="w-full font-mono text-[11px]" data-testid="demo-fleet-table">
                <thead><tr className="text-[#71717A] text-left">
                    <th className="py-1 pr-3">ACCOUNT</th><th className="pr-3">HEARTBEAT</th><th className="pr-3">EA</th><th className="pr-3">DEMO</th><th className="pr-3">EVIDENCE</th><th className="pr-3">BINARY</th><th className="pr-3">MODE</th><th className="pr-3">CAPS</th><th>BRAKE</th>
                </tr></thead>
                <tbody>
                    {rows.slice(0, 12).map((r) => (
                        <tr key={r.id} className="border-t border-[#141414]" data-testid={`demo-fleet-row-${r.id}`}>
                            <td className="py-1.5 pr-3 text-[#E4E4E7]">{r.label}</td>
                            <td className="pr-3"><Flag ok={r.heartbeat_fresh} /> {r.last_heartbeat ? String(r.last_heartbeat).slice(11, 19) : "never"}</td>
                            <td className="pr-3"><Flag ok={r.ea_current} /> {r.ea_version || "—"}</td>
                            <td className="pr-3"><Flag ok={r.attested_demo} /> {r.attested_demo ? "attested" : "LIVE"}</td>
                            <td className={`pr-3 ${r.demo_evidence?.kind === "real_money" ? "text-[#FF3B30]" : r.demo_evidence?.kind === "admin_override" ? "text-[#FFB020]" : ""}`} data-testid={`demo-evidence-${r.id}`}>{r.demo_evidence?.label || "—"}</td>
                            <td className={`pr-3 ${r.demo_evidence?.binary === "signed" ? "text-[#00FF41]" : "text-[#FFB020]"}`} data-testid={`demo-binary-${r.id}`}>{r.demo_evidence?.binary_label || "—"}</td>
                            <td className="pr-3"><Flag ok={r.position_mode_explicit} /> {r.position_mode} · {r.position_mode_source}</td>
                            <td className="pr-3"><Flag ok={r.caps_explicit} /> day {r.trade_of_day_cap ?? "—"} · open {r.max_concurrent_trades ?? "—"}</td>
                            <td>{r.braked ? <span className="text-[#FF3B30]">ACTIVE</span> : <span className="text-[#52525B]">none</span>}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
            {rows.length > 12 && <div className="font-mono text-[10px] text-[#71717A] py-2" data-testid="demo-fleet-more">+{rows.length - 12} more account(s) — the checks above cover all {rows.length}.</div>}
        </div>
    );
}

export default function DemoReadiness() {
    const [d, setD] = useState(null);
    const [err, setErr] = useState("");
    const [busy, setBusy] = useState(null);

    const load = useCallback(async () => {
        try { const { data } = await api.get("/admin/demo-readiness"); setD(data); setErr(""); }
        catch (e) { setErr(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const toggle = async (m) => {
        setBusy(m.id);
        try {
            await api.post("/admin/demo-readiness/manual", { id: m.id, checked: !m.checked });
            await load();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(null); }
    };

    const score = d?.score;
    return (
        <AppLayout>
            <PageHeader title="Demo Readiness" subtitle="One page · main94 deploy checklist · auto checks + your ticks" testid="demo-readiness-header"
                action={(
                    <div className="flex gap-2 print:hidden">
                        <button onClick={load} className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white font-mono text-[10px] tracking-widest" data-testid="demo-readiness-refresh"><RefreshCw className="w-3 h-3" /> REFRESH</button>
                        <button onClick={() => window.print()} className="inline-flex items-center gap-1.5 px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white font-mono text-[10px] tracking-widest" data-testid="demo-readiness-print"><Printer className="w-3 h-3" /> PRINT</button>
                    </div>
                )} />
            <div className="px-4 md:px-8 py-6 space-y-6 max-w-5xl">
                {err && <div className="text-[#FF3B30] font-mono text-xs" data-testid="demo-readiness-error">{err}</div>}
                {!d && !err && <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>}
                {d && (
                    <>
                        <div className={`border p-4 flex flex-wrap items-center gap-4 ${score.ready ? "border-[#00FF41]/50 bg-[#00FF41]/5" : "border-[#FF3B30]/40 bg-[#FF3B30]/5"}`} data-testid="demo-readiness-score">
                            <div className="font-display font-bold text-3xl" data-testid="demo-readiness-score-value">{score.passed}<span className="text-[#71717A] text-lg">/{score.total}</span></div>
                            <div className="flex-1 min-w-[220px]">
                                <div className="h-2 bg-[#141414]"><div className={`h-2 ${score.ready ? "bg-[#00FF41]" : "bg-[#FFB020]"}`} style={{ width: `${Math.round(100 * score.passed / Math.max(1, score.total))}%` }} /></div>
                                <div className="font-mono text-[11px] text-[#A1A1AA] mt-1" data-testid="demo-readiness-verdict">
                                    {score.ready ? "READY FOR CONTROLLED DEMO TEST — this page grants no trading authority." : `NOT READY · ${d.blockers.length} blocker(s) open · ${d.warnings.length} warning(s)`}
                                </div>
                                <div className="font-mono text-[10px] text-[#71717A] mt-1" data-testid="demo-readiness-authority">
                                    canonical trading authority: {d.authority?.summary?.length ? d.authority.summary.join(" · ") : "no demo accounts"} · build {String(d.context?.build_sha || "dev").slice(0, 12)} · {d.context?.environment} · EA {d.context?.ea_version}
                                </div>
                                <div className="font-mono text-[10px] text-[#A1A1AA] mt-1 select-all" data-testid="demo-readiness-installation-id" title="STOIC_INSTALLATION_ID — the policy-migration workflow must name this id">
                                    installation id: <span className="text-white">{d.context?.installation_id || "unset — run deploy/update.sh"}</span>
                                </div>
                            </div>
                            <div className="flex flex-col items-end gap-2">
                                <CryptoLiveBadge />
                                <div className="font-mono text-[10px] text-[#52525B]">generated {String(d.generated_at).slice(11, 19)} UTC</div>
                            </div>
                        </div>

                        <section className="border border-[#1F1F1F] bg-[#0A0A0A]">
                            <div className="px-4 py-3 border-b border-[#1F1F1F] font-mono text-[10px] tracking-widest text-[#71717A]">01 · ENVIRONMENT & PLATFORM (auto)</div>
                            <div className="px-4">{d.checks.filter((c) => !c.id.startsWith("fleet")).map((c) => <CheckRow key={c.id} c={c} />)}</div>
                        </section>

                        <section className="border border-[#1F1F1F] bg-[#0A0A0A]">
                            <div className="px-4 py-3 border-b border-[#1F1F1F] font-mono text-[10px] tracking-widest text-[#71717A]">02 · DEMO ACCOUNTS (auto)</div>
                            <div className="px-4">{d.checks.filter((c) => c.id.startsWith("fleet")).map((c) => <CheckRow key={c.id} c={c} />)}</div>
                            <div className="px-4 pb-3"><FleetTable rows={d.fleet} /></div>
                        </section>

                        <section className="border border-[#1F1F1F] bg-[#0A0A0A]">
                            <div className="px-4 py-3 border-b border-[#1F1F1F] font-mono text-[10px] tracking-widest text-[#71717A]">03 · YOUR STEPS (tick when done — stored with build, environment, actor; expire after a deploy / EA / credential / account change or 7 days)</div>
                            <div className="px-4">
                                {d.manual.map((m) => (
                                    <label key={m.id} className="flex items-start gap-3 py-2 border-b border-[#141414] last:border-0 cursor-pointer" data-testid={`demo-manual-${m.id}`}>
                                        <input type="checkbox" className="mt-1 accent-[#00FF41]" checked={m.checked} disabled={busy === m.id}
                                               onChange={() => toggle(m)} data-testid={`demo-manual-toggle-${m.id}`} />
                                        <div className="flex-1">
                                            <div className={`text-sm ${m.checked ? "text-[#71717A] line-through" : "text-[#E4E4E7]"}`}>{m.title}</div>
                                            <div className="font-mono text-[10px] text-[#71717A]">{m.hint}</div>
                                            {m.checked && <div className="font-mono text-[10px] text-[#00FF41]/80" data-testid={`demo-manual-by-${m.id}`}>✓ {m.checked_by} · {String(m.checked_at).slice(0, 16).replace("T", " ")} UTC · build {String(m.build_sha || "").slice(0, 7)} · valid until {String(m.expires_at || "").slice(0, 10)}</div>}
                                            {!m.checked && m.expired_reason && <div className="font-mono text-[10px] text-[#FFB020]" data-testid={`demo-manual-expired-${m.id}`}>{m.expired_reason} — tick again</div>}
                                        </div>
                                    </label>
                                ))}
                            </div>
                        </section>

                        <AcceptanceBundleCard />
                    </>
                )}
            </div>
        </AppLayout>
    );
}
