import { useEffect, useState, useCallback } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import {
    Users as UsersIcon, ShieldCheck, ShieldX, Loader2, RefreshCw, History,
    AlertTriangle, Clock, DollarSign,
} from "lucide-react";

function AffStatusPill({ aff }) {
    if (aff.terminated) {
        return <span className="inline-flex items-center px-2 py-0.5 border border-red-500/40 bg-red-500/10 text-red-400 text-[10px] font-mono tracking-widest">TERMINATED</span>;
    }
    if (!aff.active) {
        return <span className="inline-flex items-center px-2 py-0.5 border border-amber-500/40 bg-amber-500/10 text-amber-400 text-[10px] font-mono tracking-widest">SUSPENDED</span>;
    }
    return <span className="inline-flex items-center px-2 py-0.5 border border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41] text-[10px] font-mono tracking-widest">ACTIVE</span>;
}

function AffModerationModal({ open, onClose, action, target, onConfirm }) {
    const [reason, setReason] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => { if (open) setReason(""); }, [open]);
    if (!open) return null;
    const needsReason = action === "suspend" || action === "terminate";

    return (
        <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={onClose}>
            <div className="w-full max-w-lg bg-[#0A0A0A] border border-[#1F1F1F]" onClick={e => e.stopPropagation()}>
                <div className="p-5 border-b border-[#1F1F1F] flex items-start gap-3">
                    {action === "suspend" && <AlertTriangle className="w-5 h-5 text-amber-400" />}
                    {action === "terminate" && <ShieldX className="w-5 h-5 text-red-400" />}
                    {action === "unsuspend" && <ShieldCheck className="w-5 h-5 text-[#00FF41]" />}
                    <div>
                        <div className="font-display font-bold tracking-wide text-white capitalize">
                            {action} Affiliate
                        </div>
                        <div className="text-sm text-[#A1A1AA] mt-0.5">
                            {target?.user_email} · code {target?.code}
                        </div>
                    </div>
                </div>
                <div className="p-5 space-y-3">
                    {action === "terminate" && (
                        <div className="border border-red-900/40 bg-red-950/30 text-red-300 text-sm p-3">
                            <strong className="block text-red-400 mb-1">Permanent action — forfeits balance.</strong>
                            Unpaid commission balance of <strong>${(target?.unpaid_balance_usd ?? 0).toFixed(2)}</strong> will be forfeited per Terms §7. Pending payout requests will be cancelled.
                        </div>
                    )}
                    {needsReason && (
                        <div>
                            <label className="block text-[10px] font-mono tracking-widest text-[#52525B] mb-1">VIOLATION REASON</label>
                            <textarea
                                data-testid="aff-moderation-reason-input"
                                value={reason}
                                onChange={e => setReason(e.target.value)}
                                rows={4}
                                placeholder="e.g. §7 incentivized signups · fake testimonials · cookie stuffing"
                                className="w-full bg-[#121212] border border-[#1F1F1F] focus:border-[#FFD700] text-white text-sm px-3 py-2 outline-none"
                            />
                        </div>
                    )}
                    {action === "unsuspend" && (
                        <p className="text-sm text-[#A1A1AA]">Reactivates the affiliate. Their referral code starts attributing clicks again immediately.</p>
                    )}
                </div>
                <div className="p-4 border-t border-[#1F1F1F] flex justify-end gap-2">
                    <button onClick={onClose} disabled={busy}
                        className="px-3 py-1.5 text-xs font-mono tracking-widest text-[#A1A1AA] hover:text-white border border-[#1F1F1F]"
                        data-testid="aff-moderation-cancel-btn">
                        CANCEL
                    </button>
                    <button
                        data-testid="aff-moderation-confirm-btn"
                        disabled={busy || (needsReason && reason.trim().length < 3)}
                        onClick={async () => {
                            setBusy(true);
                            try { await onConfirm(reason.trim()); onClose(); }
                            finally { setBusy(false); }
                        }}
                        className={`px-3 py-1.5 text-xs font-mono tracking-widest border ${
                            action === "terminate" ? "border-red-500/60 bg-red-500/10 text-red-300 hover:bg-red-500/20"
                            : action === "suspend" ? "border-amber-500/60 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
                            : "border-[#00FF41]/60 bg-[#00FF41]/10 text-[#00FF41] hover:bg-[#00FF41]/20"
                        } ${busy ? "opacity-50" : ""}`}>
                        {busy ? <Loader2 className="w-3 h-3 animate-spin inline" /> : "CONFIRM"}
                    </button>
                </div>
            </div>
        </div>
    );
}

export default function AdminAffiliates() {
    const [affs, setAffs] = useState([]);
    const [loading, setLoading] = useState(true);
    const [statusFilter, setStatusFilter] = useState("");
    const [modal, setModal] = useState({ open: false, action: "", target: null });
    const [audit, setAudit] = useState([]);
    const [auditOpen, setAuditOpen] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const params = new URLSearchParams();
            if (statusFilter) params.set("status", statusFilter);
            const { data } = await api.get(`/admin/affiliates?${params}`);
            setAffs(data.affiliates || []);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setLoading(false); }
    }, [statusFilter]);

    useEffect(() => { load(); }, [load]);

    const loadAudit = async () => {
        try {
            const { data } = await api.get("/admin/audit-log?kind=affiliate&limit=50");
            setAudit(data.audit || []);
            setAuditOpen(true);
        } catch (e) { toast.error(formatApiError(e)); }
    };

    const doModerate = async (action, reason) => {
        const id = modal.target?.id;
        if (!id) return;
        try {
            const body = (action === "suspend" || action === "terminate") ? { reason } : {};
            await api.post(`/admin/affiliates/${id}/${action}`, body);
            toast.success(`Affiliate ${action}ed.`);
            await load();
        } catch (e) {
            toast.error(formatApiError(e));
            throw e;
        }
    };

    const openModal = (action, target) => setModal({ open: true, action, target });

    return (
        <AppLayout>
            <PageHeader
                title="Admin · Affiliate Management"
                subtitle="Suspend or terminate affiliates who violate Terms §7."
                testid="admin-affiliates-header"
                action={
                    <button onClick={loadAudit}
                        data-testid="admin-affiliates-audit-btn"
                        className="px-3 py-1.5 text-xs font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:text-white hover:border-[#52525B] flex items-center gap-1.5">
                        <History className="w-3 h-3" /> AUDIT LOG
                    </button>
                }
            />

            <div className="bg-[#0A0A0A] border border-[#1F1F1F] mb-4 p-3 flex flex-wrap items-center gap-2">
                {["", "active", "suspended", "terminated"].map(s => (
                    <button key={s || "all"}
                        data-testid={`admin-affiliates-filter-${s || "all"}`}
                        onClick={() => setStatusFilter(s)}
                        className={`px-3 py-1.5 text-[10px] font-mono tracking-widest border ${
                            statusFilter === s
                                ? "border-[#FFD700] bg-[#FFD700]/10 text-[#FFD700]"
                                : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B]"
                        }`}>
                        {s ? s.toUpperCase() : "ALL"}
                    </button>
                ))}
                <button onClick={load}
                    data-testid="admin-affiliates-refresh-btn"
                    className="px-2 py-1.5 text-xs text-[#A1A1AA] hover:text-white border border-[#1F1F1F] ml-auto">
                    <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                </button>
            </div>

            <div className="bg-[#0A0A0A] border border-[#1F1F1F] overflow-x-auto">
                <table className="w-full text-sm" data-testid="admin-affiliates-table">
                    <thead className="bg-[#121212] text-[10px] font-mono tracking-widest text-[#52525B]">
                        <tr>
                            <th className="text-left px-3 py-2">EMAIL · CODE</th>
                            <th className="text-left px-3 py-2">STATUS</th>
                            <th className="text-right px-3 py-2">CLICKS</th>
                            <th className="text-right px-3 py-2">CONVS</th>
                            <th className="text-right px-3 py-2">EARNED</th>
                            <th className="text-right px-3 py-2">UNPAID</th>
                            <th className="text-left px-3 py-2">REASON</th>
                            <th className="text-right px-3 py-2">ACTIONS</th>
                        </tr>
                    </thead>
                    <tbody>
                        {loading && (
                            <tr><td colSpan={8} className="py-10 text-center text-[#A1A1AA]">
                                <Loader2 className="w-4 h-4 animate-spin inline mr-2" />Loading…
                            </td></tr>
                        )}
                        {!loading && affs.length === 0 && (
                            <tr><td colSpan={8} className="py-10 text-center text-[#52525B]">No affiliates match.</td></tr>
                        )}
                        {!loading && affs.map(a => {
                            const reason = a.terminated_reason || a.suspended_reason || "";
                            return (
                                <tr key={a.id} className="border-t border-[#1F1F1F]" data-testid={`admin-affiliate-row-${a.id}`}>
                                    <td className="px-3 py-2">
                                        <div className="font-medium text-white">{a.user_email}</div>
                                        <div className="text-[10px] text-[#52525B] font-mono">code {a.code}</div>
                                    </td>
                                    <td className="px-3 py-2"><AffStatusPill aff={a} /></td>
                                    <td className="px-3 py-2 text-right text-[#A1A1AA]">{a.lifetime_clicks}</td>
                                    <td className="px-3 py-2 text-right text-[#A1A1AA]">{a.lifetime_conversions}</td>
                                    <td className="px-3 py-2 text-right text-[#00FF41]">${(a.lifetime_earnings_usd ?? 0).toFixed(2)}</td>
                                    <td className="px-3 py-2 text-right text-[#FFD700]">${(a.unpaid_balance_usd ?? 0).toFixed(2)}</td>
                                    <td className="px-3 py-2 text-[#A1A1AA] text-xs max-w-[180px] truncate" title={reason}>
                                        {reason || <span className="text-[#52525B]">—</span>}
                                    </td>
                                    <td className="px-3 py-2 text-right space-x-1">
                                        {a.terminated ? (
                                            <span className="text-[10px] text-[#52525B] font-mono">FINAL</span>
                                        ) : a.active ? (
                                            <>
                                                <button onClick={() => openModal("suspend", a)}
                                                    data-testid={`btn-aff-suspend-${a.id}`}
                                                    className="px-2 py-1 text-[10px] font-mono tracking-widest border border-amber-500/40 text-amber-400 hover:bg-amber-500/10">
                                                    SUSPEND
                                                </button>
                                                <button onClick={() => openModal("terminate", a)}
                                                    data-testid={`btn-aff-terminate-${a.id}`}
                                                    className="px-2 py-1 text-[10px] font-mono tracking-widest border border-red-500/40 text-red-400 hover:bg-red-500/10">
                                                    TERMINATE
                                                </button>
                                            </>
                                        ) : (
                                            <>
                                                <button onClick={() => openModal("unsuspend", a)}
                                                    data-testid={`btn-aff-unsuspend-${a.id}`}
                                                    className="px-2 py-1 text-[10px] font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10">
                                                    UNSUSPEND
                                                </button>
                                                <button onClick={() => openModal("terminate", a)}
                                                    data-testid={`btn-aff-terminate-${a.id}`}
                                                    className="px-2 py-1 text-[10px] font-mono tracking-widest border border-red-500/40 text-red-400 hover:bg-red-500/10">
                                                    TERMINATE
                                                </button>
                                            </>
                                        )}
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>

            <AffModerationModal
                open={modal.open}
                action={modal.action}
                target={modal.target}
                onClose={() => setModal({ ...modal, open: false })}
                onConfirm={(reason) => doModerate(modal.action, reason)}
            />

            {auditOpen && (
                <div className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4" onClick={() => setAuditOpen(false)}>
                    <div className="w-full max-w-3xl max-h-[80vh] overflow-y-auto bg-[#0A0A0A] border border-[#1F1F1F]" onClick={e => e.stopPropagation()}>
                        <div className="p-4 border-b border-[#1F1F1F] flex items-center justify-between sticky top-0 bg-[#0A0A0A]">
                            <div className="font-display font-bold tracking-wide text-white flex items-center gap-2">
                                <History className="w-4 h-4 text-[#FFD700]" /> Affiliate Audit Log
                            </div>
                            <button onClick={() => setAuditOpen(false)} className="text-[#A1A1AA] hover:text-white text-xl">×</button>
                        </div>
                        <div className="divide-y divide-[#1F1F1F]" data-testid="admin-affiliate-audit-list">
                            {audit.length === 0 && <div className="p-6 text-center text-[#52525B]">No actions recorded.</div>}
                            {audit.map(a => (
                                <div key={a.id} className="p-3 text-sm">
                                    <div className="flex items-center gap-2 text-xs font-mono tracking-widest mb-1">
                                        <Clock className="w-3 h-3 text-[#52525B]" />
                                        <span className="text-[#52525B]">{a.at}</span>
                                        <span className={`px-1.5 py-0.5 border ${
                                            a.action === "terminate" ? "border-red-500/40 text-red-400"
                                            : a.action === "suspend" ? "border-amber-500/40 text-amber-400"
                                            : "border-[#00FF41]/40 text-[#00FF41]"
                                        }`}>{a.action.toUpperCase()}</span>
                                        {a.meta?.forfeited_usd ? (
                                            <span className="text-red-400 text-[10px]">forfeited ${a.meta.forfeited_usd.toFixed(2)}</span>
                                        ) : null}
                                    </div>
                                    <div className="text-white">{a.target_label || a.target_id}</div>
                                    {a.reason && <div className="text-[#A1A1AA] text-xs mt-1">Reason: {a.reason}</div>}
                                    <div className="text-[10px] text-[#52525B] font-mono mt-1">by {a.actor_email}</div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            )}
        </AppLayout>
    );
}
