import { useCallback, useEffect, useState } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, RefreshCw, Wrench, ChevronDown, ChevronRight, X, Copy } from "lucide-react";

const KIND_LABEL = {
    account_revived: "Account revived",
    account_dormant: "Account marked dormant",
    pending_modification_expired: "Pending modification expired",
    ghost_ack_old: "Ghost close acknowledged (>24h)",
    ghost_ack_orphan: "Ghost close acknowledged (account deleted)",
};
const label = k => KIND_LABEL[k] || (k || "").replaceAll("_", " ");
const fmt = iso => (iso ? new Date(iso).toLocaleString() : "—");
const PAGE = 50;

function KindChips({ kinds, active, onPick }) {
    return (
        <div className="flex flex-wrap gap-2 mb-4" data-testid="repair-kind-chips">
            <button onClick={() => onPick("")} data-testid="repair-kind-chip-all"
                className={`px-3 py-1.5 text-[10px] font-mono tracking-widest uppercase border ${
                    !active ? "border-[#00FF41]/50 bg-[#00FF41]/10 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                ALL
            </button>
            {kinds.map(k => (
                <button key={k.kind} onClick={() => onPick(k.kind)} data-testid={`repair-kind-chip-${k.kind}`}
                    title={`last: ${fmt(k.last_at)}`}
                    className={`px-3 py-1.5 text-[10px] font-mono tracking-widest uppercase border flex items-center gap-2 ${
                        active === k.kind ? "border-[#00FF41]/50 bg-[#00FF41]/10 text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"}`}>
                    {label(k.kind)}
                    <span className="text-[#52525B]">{k.sweeps}·{k.records}</span>
                </button>
            ))}
        </div>
    );
}

function AffectedIds({ ids }) {
    const copy = () => { navigator.clipboard?.writeText(ids.join("\n")); toast.success(`${ids.length} ids copied`); };
    return (
        <div className="mt-2 border border-[#1F1F1F] bg-[#050505] p-3" data-testid="repair-affected-ids">
            <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] font-mono tracking-widest text-[#52525B]">AFFECTED RECORDS · {ids.length}</span>
                <button onClick={copy} data-testid="repair-copy-ids-btn"
                    className="text-[10px] font-mono text-[#A1A1AA] hover:text-[#00FF41] flex items-center gap-1"><Copy className="w-3 h-3" /> COPY</button>
            </div>
            <div className="flex flex-wrap gap-1.5 max-h-48 overflow-auto">
                {ids.map(id => <code key={id} className="text-[11px] font-mono text-[#A1A1AA] bg-[#0A0A0A] border border-[#1F1F1F] px-1.5 py-0.5">{id}</code>)}
            </div>
        </div>
    );
}

function Row({ r, onSweep }) {
    const [open, setOpen] = useState(false);
    return (
        <div className="border-b border-[#1F1F1F] last:border-0" data-testid={`repair-row-${r.id}`}>
            <div className="grid grid-cols-[16px_1fr] sm:grid-cols-[16px_150px_1fr_1fr_80px] gap-x-3 items-center px-3 py-2.5 text-xs hover:bg-[#0F0F0F] cursor-pointer"
                onClick={() => setOpen(o => !o)} data-testid={`repair-row-toggle-${r.id}`}>
                <span className="text-[#52525B]">{open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}</span>
                <span className="font-mono text-[#71717A] hidden sm:block">{fmt(r.at)}</span>
                <span className="text-[#E4E4E7] truncate">{label(r.kind)}<span className="sm:hidden text-[#52525B] font-mono"> · {fmt(r.at)}</span></span>
                <span className="font-mono text-[#A1A1AA] truncate hidden sm:block" title={r.user_id}>{r.user_email || r.user_id || "—"}</span>
                <span className="font-mono text-[#00FF41] text-right hidden sm:block" data-testid={`repair-row-count-${r.id}`}>{r.count}</span>
            </div>
            {open && (
                <div className="px-3 pb-3 sm:pl-10" data-testid={`repair-row-detail-${r.id}`}>
                    <div className="flex flex-wrap gap-x-5 gap-y-1 text-[11px] font-mono text-[#71717A]">
                        <span>correlation <button onClick={() => onSweep(r.correlation_id)} data-testid={`repair-corr-link-${r.id}`}
                            className="text-[#00FF41] hover:underline">{r.correlation_id}</button></span>
                        <span>source <span className="text-[#A1A1AA]">{r.source || "—"}</span></span>
                        <span>user <span className="text-[#A1A1AA]">{r.user_email || "—"} ({r.user_id})</span></span>
                        <span className="sm:hidden">records <span className="text-[#00FF41]">{r.count}</span></span>
                        {r.detail && Object.keys(r.detail).length > 0 && <span>detail <span className="text-[#A1A1AA]">{JSON.stringify(r.detail)}</span></span>}
                    </div>
                    <AffectedIds ids={r.affected_ids || []} />
                </div>
            )}
        </div>
    );
}

function SweepDrawer({ corr, onClose }) {
    const [data, setData] = useState(null);
    useEffect(() => {
        setData(null);
        api.get(`/admin/repair-ledger/${encodeURIComponent(corr)}`).then(r => setData(r.data))
            .catch(e => { toast.error(formatApiError(e)); onClose(); });
    }, [corr, onClose]);
    return (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/60" onClick={onClose} data-testid="repair-sweep-drawer">
            <aside className="w-full max-w-xl h-full bg-[#0A0A0A] border-l border-[#1F1F1F] p-5 overflow-auto" onClick={e => e.stopPropagation()}>
                <div className="flex items-start justify-between mb-4">
                    <div>
                        <div className="text-[10px] font-mono tracking-widest text-[#52525B]">SWEEP</div>
                        <div className="font-mono text-sm text-[#00FF41]" data-testid="repair-sweep-corr">{corr}</div>
                    </div>
                    <button onClick={onClose} data-testid="repair-sweep-close" className="text-[#71717A] hover:text-[#E4E4E7]"><X className="w-4 h-4" /></button>
                </div>
                {!data ? <Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /> : (
                    <>
                        <div className="grid grid-cols-2 gap-3 text-xs mb-5">
                            <Stat k="WHEN" v={fmt(data.at)} />
                            <Stat k="SOURCE" v={data.source || "—"} />
                            <Stat k="USER" v={data.user_email || data.user_id || "—"} />
                            <Stat k="RECORDS TOUCHED" v={data.records} green testid="repair-sweep-records" />
                        </div>
                        {data.rows.map(r => (
                            <div key={r.id} className="mb-4" data-testid={`repair-sweep-row-${r.id}`}>
                                <div className="text-xs text-[#E4E4E7]">{label(r.kind)} <span className="font-mono text-[#00FF41]">· {r.count}</span></div>
                                <AffectedIds ids={r.affected_ids || []} />
                            </div>
                        ))}
                    </>
                )}
            </aside>
        </div>
    );
}

function Stat({ k, v, green, testid }) {
    return (
        <div className="border border-[#1F1F1F] p-3">
            <div className="text-[10px] font-mono tracking-widest text-[#52525B]">{k}</div>
            <div className={`font-mono text-sm mt-1 break-all ${green ? "text-[#00FF41]" : "text-[#E4E4E7]"}`} data-testid={testid}>{v}</div>
        </div>
    );
}

export default function AdminRepairLedger() {
    const [kinds, setKinds] = useState(null);
    const [data, setData] = useState(null);
    const [kind, setKind] = useState("");
    const [query, setQuery] = useState("");
    const [skip, setSkip] = useState(0);
    const [sweep, setSweep] = useState(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const q = query.trim();
            const params = { limit: PAGE, skip, kind };
            if (q.startsWith("repair_")) params.correlation_id = q; else if (q) params.user_id = q;
            const [k, l] = await Promise.all([api.get("/admin/repair-ledger/kinds"), api.get("/admin/repair-ledger", { params })]);
            setKinds(k.data); setData(l.data);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setLoading(false); }
    }, [kind, query, skip]);
    useEffect(() => { load(); }, [load]);

    const pick = k => { setKind(k); setSkip(0); };
    const closeSweep = useCallback(() => setSweep(null), []);
    const total = data?.total ?? 0;

    return (
        <AppLayout>
            <PageHeader title="Admin · Repair Ledger" testid="admin-repair-ledger-header"
                subtitle="Every automated state repair, with its correlation id and the exact records it touched. Read-only — GET endpoints never mutate."
                action={
                    <button onClick={load} data-testid="repair-ledger-refresh-btn"
                        className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5">
                        {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} REFRESH
                    </button>} />

            {kinds && (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5" data-testid="repair-ledger-summary">
                    <Stat k="LEDGER ROWS" v={kinds.total_rows} testid="repair-summary-rows" />
                    <Stat k="RECORDS REPAIRED" v={kinds.total_records} green testid="repair-summary-records" />
                    <Stat k="REPAIR KINDS" v={kinds.kinds.length} />
                    <Stat k="SOURCES" v={kinds.sources.join(", ") || "—"} />
                </div>
            )}
            {kinds && <KindChips kinds={kinds.kinds} active={kind} onPick={pick} />}

            <div className="flex flex-col sm:flex-row gap-2 mb-3">
                <input value={query} onChange={e => { setQuery(e.target.value); setSkip(0); }} data-testid="repair-ledger-search"
                    placeholder="filter by user id or correlation id (repair_…)"
                    className="flex-1 bg-[#0A0A0A] border border-[#1F1F1F] px-3 py-2 text-xs font-mono text-[#E4E4E7] placeholder:text-[#3F3F46] focus:border-[#00FF41]/50 outline-none" />
                <div className="text-[11px] font-mono text-[#52525B] self-center" data-testid="repair-ledger-total">{total} rows</div>
            </div>

            <div className="bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="repair-ledger-table">
                <div className="hidden sm:grid grid-cols-[16px_150px_1fr_1fr_80px] gap-x-3 px-3 py-2 text-[10px] font-mono tracking-widest text-[#52525B] border-b border-[#1F1F1F]">
                    <span /><span>WHEN</span><span>REPAIR</span><span>USER</span><span className="text-right">RECORDS</span>
                </div>
                {!data && <div className="flex justify-center py-12"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>}
                {data && data.rows.length === 0 && (
                    <div className="px-4 py-10 text-center text-xs text-[#52525B] flex flex-col items-center gap-2" data-testid="repair-ledger-empty">
                        <Wrench className="w-5 h-5" /> No repairs recorded{kind ? ` for ${label(kind)}` : ""} — the analytics worker writes here whenever it repairs state.
                    </div>
                )}
                {data?.rows.map(r => <Row key={r.id} r={r} onSweep={setSweep} />)}
            </div>

            {total > PAGE && (
                <div className="flex items-center justify-between mt-3 text-xs font-mono">
                    <button disabled={skip === 0} onClick={() => setSkip(s => Math.max(0, s - PAGE))} data-testid="repair-ledger-prev"
                        className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] disabled:opacity-30 hover:border-[#52525B]">← PREV</button>
                    <span className="text-[#52525B]">{skip + 1}–{Math.min(skip + PAGE, total)} of {total}</span>
                    <button disabled={skip + PAGE >= total} onClick={() => setSkip(s => s + PAGE)} data-testid="repair-ledger-next"
                        className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] disabled:opacity-30 hover:border-[#52525B]">NEXT →</button>
                </div>
            )}

            {sweep && <SweepDrawer corr={sweep} onClose={closeSweep} />}
        </AppLayout>
    );
}
