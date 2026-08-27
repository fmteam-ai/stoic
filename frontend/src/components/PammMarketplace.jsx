import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { Briefcase, Loader2, TrendingDown, TrendingUp, Users } from "lucide-react";

const box = "border border-[#1F1F1F] bg-[#0A0A0A]";
const label = "font-mono text-[10px] tracking-widest text-[#52525B]";

const STATUS_CLS = {
    pending: "border-[#FFB000]/40 text-[#FFB000]",
    processing: "border-[#FFB000]/40 text-[#FFB000]",
    approved: "border-[#00FF41]/40 text-[#00FF41]",
    rejected: "border-[#FF3B30]/40 text-[#FF3B30]",
};

function Stat({ title, value, tone = "text-white" }) {
    return (
        <div>
            <div className={label}>{title}</div>
            <div className={`font-mono text-sm font-bold ${tone}`}>{value}</div>
        </div>
    );
}

function JoinDialog({ listing, myPending, onDone }) {
    const [open, setOpen] = useState(false);
    const [amount, setAmount] = useState("");
    const [note, setNote] = useState("");
    const [busy, setBusy] = useState(false);
    const submit = async () => {
        setBusy(true);
        try {
            await api.post(`/pamm/marketplace/${listing.program_id}/join`,
                { amount: parseFloat(amount), note });
            toast.success("Request sent — the manager will review it");
            setOpen(false); setAmount(""); setNote("");
            onDone();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    if (myPending) return (
        <div className="mt-3 px-2 py-1.5 border border-[#FFB000]/40 text-[#FFB000] font-mono text-[10px] tracking-widest text-center"
            data-testid={`join-pending-${listing.program_id}`}>REQUEST PENDING REVIEW</div>
    );
    return open ? (
        <div className="mt-3 space-y-2" data-testid={`join-form-${listing.program_id}`}>
            <input type="number" placeholder={`Amount (${listing.currency})`} value={amount}
                onChange={e => setAmount(e.target.value)} data-testid={`join-amount-${listing.program_id}`}
                className="w-full bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs text-white" />
            <input placeholder="Note to the manager (optional)" value={note}
                onChange={e => setNote(e.target.value)} data-testid={`join-note-${listing.program_id}`}
                className="w-full bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs text-white" />
            <div className="flex gap-2">
                <button onClick={submit} disabled={busy || !(parseFloat(amount) > 0)}
                    data-testid={`join-submit-${listing.program_id}`}
                    className="flex-1 px-2 py-1.5 text-[10px] font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-40">
                    {busy ? <Loader2 className="w-3 h-3 animate-spin inline" /> : "SEND REQUEST"}
                </button>
                <button onClick={() => setOpen(false)}
                    className="px-2 py-1.5 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#52525B] hover:text-white">CANCEL</button>
            </div>
        </div>
    ) : (
        <button onClick={() => setOpen(true)} data-testid={`join-open-${listing.program_id}`}
            className="mt-3 w-full px-2 py-1.5 text-[10px] font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10">
            REQUEST TO JOIN
        </button>
    );
}

function ListingCard({ l, myPending, onDone }) {
    const ret = l.performance?.total_return_pct;
    const fmt = v => (v === null || v === undefined) ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
    return (
        <div className={`${box} p-4 flex flex-col`} data-testid={`pamm-listing-${l.program_id}`}>
            <div className="flex items-start justify-between gap-2">
                <div>
                    <div className="font-display font-bold text-sm text-white flex items-center gap-1.5">
                        <Briefcase className="w-3.5 h-3.5 text-[#00FF41]" /> {l.name}
                    </div>
                    <div className="font-mono text-[9px] text-[#52525B] mt-0.5">
                        MANAGED · {l.currency} · fee {fmt(l.manager_fee_pct)}%
                    </div>
                </div>
                <span className={`font-mono text-[9px] px-1.5 py-0.5 border ${l.trading === "enabled" ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FFB000]/40 text-[#FFB000]"}`}>
                    {l.trading === "enabled" ? "LIVE" : "PAUSED"}
                </span>
            </div>
            {l.pitch && <div className="font-mono text-[10px] text-[#71717A] mt-2">{l.pitch}</div>}
            <div className="grid grid-cols-2 gap-x-3 gap-y-2 mt-3 border-t border-[#141414] pt-2.5">
                <Stat title="RETURN" value={`${fmt(ret)}%`} tone={(ret || 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
                <Stat title="MAX DD" value={`${fmt(l.performance?.max_drawdown_pct)}%`} tone="text-[#FFB000]" />
                <Stat title="AUM" value={fmt(l.aum)} />
                <Stat title="INVESTORS" value={l.investor_count ?? 0} />
            </div>
            <div className="flex-1" />
            <JoinDialog listing={l} myPending={myPending} onDone={onDone} />
        </div>
    );
}

export function PammMarketplace() {
    const [listings, setListings] = useState(null);
    const [requests, setRequests] = useState([]);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        try {
            const [l, r] = await Promise.all([
                api.get("/pamm/marketplace"),
                api.get("/pamm/marketplace/my-requests"),
            ]);
            setListings(l.data.listings || []);
            setRequests(r.data.requests || []);
        } catch (e) { setErr(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const pendingSet = new Set(requests.filter(r => r.status === "pending" || r.status === "processing").map(r => r.program_id));

    return (
        <div className="space-y-5" data-testid="pamm-marketplace">
            {err && <div className="font-mono text-xs text-[#FF3B30]" data-testid="pamm-market-error">{err}</div>}
            {listings === null && !err && <div className="font-mono text-xs text-[#52525B]" data-testid="pamm-market-loading">Loading managed strategies…</div>}
            {listings?.length === 0 && (
                <div className={`${box} p-10 text-center font-mono text-xs text-[#52525B]`} data-testid="pamm-market-empty">
                    No managed strategies are published yet — check back soon.
                </div>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                {(listings || []).map(l => (
                    <ListingCard key={l.program_id} l={l} myPending={pendingSet.has(l.program_id)} onDone={load} />
                ))}
            </div>
            {requests.length > 0 && (
                <div className={box} data-testid="pamm-my-requests">
                    <div className="px-4 py-3 border-b border-[#1F1F1F]"><span className={label}>MY REQUESTS</span></div>
                    {requests.map(r => (
                        <div key={r.request_id} className="px-4 py-2 border-b border-[#141414] last:border-b-0 flex items-center justify-between text-xs font-mono">
                            <span className="text-white">{r.program_name}</span>
                            <span className="text-[#A1A1AA]">{Number(r.amount).toLocaleString()}</span>
                            <span className={`px-1.5 py-0.5 border text-[9px] tracking-widest ${STATUS_CLS[r.status] || STATUS_CLS.pending}`}
                                data-testid={`my-request-status-${r.request_id}`}>
                                {r.status.toUpperCase()}
                            </span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
