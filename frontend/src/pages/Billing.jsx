import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    CreditCard, Calendar, Receipt, CheckCircle2, AlertTriangle, Clock,
    Sparkles, ExternalLink, RefreshCw as ArrowsClockwise,
} from "lucide-react";

const PLAN_LABELS = {
    monthly: "Monthly",
    quarterly: "Quarterly",
    annual: "Annual",
    lifetime: "Lifetime",
    admin_grandfather: "Admin Grandfather",
    trial: "Trial",
};

const STATUS_PALETTE = {
    paid:      { fg: "text-[#00FF41]", bg: "bg-[#00FF41]/10", bd: "border-[#00FF41]/40", label: "PAID" },
    completed: { fg: "text-[#00FF41]", bg: "bg-[#00FF41]/10", bd: "border-[#00FF41]/40", label: "COMPLETED" },
    initiated: { fg: "text-[#FFB000]", bg: "bg-[#FFB000]/10", bd: "border-[#FFB000]/40", label: "PENDING" },
    pending:   { fg: "text-[#FFB000]", bg: "bg-[#FFB000]/10", bd: "border-[#FFB000]/40", label: "PENDING" },
    failed:    { fg: "text-[#FF3B30]", bg: "bg-[#FF3B30]/10", bd: "border-[#FF3B30]/40", label: "FAILED" },
    refunded:  { fg: "text-[#A1A1AA]", bg: "bg-[#0A0A0A]",    bd: "border-[#1F1F1F]",   label: "REFUNDED" },
    unknown:   { fg: "text-[#52525B]", bg: "bg-[#0A0A0A]",    bd: "border-[#1F1F1F]",   label: "UNKNOWN" },
};

function fmtDate(iso, opts = {}) {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleString("en-US", {
            year: "numeric", month: "short", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
            ...opts,
        });
    } catch { return iso.slice(0, 19); }
}

function daysUntil(iso) {
    if (!iso) return null;
    try {
        const ms = new Date(iso).getTime() - Date.now();
        return Math.ceil(ms / (1000 * 60 * 60 * 24));
    } catch { return null; }
}

export default function Billing() {
    const [status, setStatus] = useState(null);
    const [txs, setTxs] = useState([]);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        setErr("");
        try {
            const [s, t] = await Promise.all([
                api.get("/subscription/status"),
                api.get("/subscription/transactions"),
            ]);
            setStatus(s.data);
            setTxs(t.data || []);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    return (
        <AppLayout>
            <PageHeader
                title="Billing"
                subtitle="Your current plan, expiration and transaction history."
                testid="billing-header"
                action={
                    <button onClick={load} data-testid="billing-refresh-button"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                        <ArrowsClockwise className="w-3.5 h-3.5" /> REFRESH
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-6 max-w-5xl">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="billing-error">{err}</div>}
                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING BILLING DATA…</div>
                ) : (
                    <>
                        <CurrentPlanCard status={status} />
                        <TransactionsCard txs={txs} />
                    </>
                )}
            </div>
        </AppLayout>
    );
}

function CurrentPlanCard({ status }) {
    const sub = status?.subscription;
    const ent = status?.entitlement;
    const planId = sub?.current_plan_id || "trial";
    const planLabel = PLAN_LABELS[planId] || planId;
    const validUntil = sub?.valid_until;
    const isActive = ent?.active === true;
    const inGrace = ent?.in_grace === true;
    const days = daysUntil(validUntil);

    let badge;
    if (planId === "admin_grandfather") badge = { fg: "text-[#FFD700]", bg: "bg-[#FFD700]/10", bd: "border-[#FFD700]/40", label: "● PERMANENT" };
    else if (inGrace) badge = { fg: "text-[#FFB000]", bg: "bg-[#FFB000]/10", bd: "border-[#FFB000]/40", label: "● GRACE PERIOD" };
    else if (isActive) badge = { fg: "text-[#00FF41]", bg: "bg-[#00FF41]/10", bd: "border-[#00FF41]/40", label: "● ACTIVE" };
    else badge = { fg: "text-[#FF3B30]", bg: "bg-[#FF3B30]/10", bd: "border-[#FF3B30]/40", label: "○ INACTIVE" };

    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="current-plan-card">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <CreditCard className="w-4 h-4 text-[#00FF41]" />
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">CURRENT PLAN</div>
                        <div className="font-display font-bold text-lg tracking-tight" data-testid="current-plan-label">{planLabel}</div>
                    </div>
                </div>
                <div className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${badge.bd} ${badge.bg} ${badge.fg}`}
                    data-testid="current-plan-status">
                    {badge.label}
                </div>
            </div>

            <div className="p-5 grid grid-cols-1 md:grid-cols-3 gap-4">
                <PlanMetric icon={Calendar} label="EXPIRES ON" testid="plan-expires">
                    <div className="font-display font-bold text-base">{fmtDate(validUntil)}</div>
                    {planId === "admin_grandfather" ? (
                        <div className="font-mono text-[10px] text-[#FFD700] tracking-widest mt-1">— GRANDFATHERED · NEVER EXPIRES —</div>
                    ) : days !== null ? (
                        <div className={`font-mono text-[10px] tracking-widest mt-1 ${
                            days < 0 ? "text-[#FF3B30]" : days < 7 ? "text-[#FFB000]" : "text-[#A1A1AA]"
                        }`}>
                            {days < 0 ? `EXPIRED ${-days}D AGO`
                                : days === 0 ? "EXPIRES TODAY"
                                : `${days} DAY${days === 1 ? "" : "S"} REMAINING`}
                        </div>
                    ) : null}
                </PlanMetric>

                <PlanMetric icon={Clock} label="STATUS DETAIL" testid="plan-status-detail">
                    <div className="font-display font-bold text-base">
                        {ent?.reason === "subscription_active" ? "Active"
                            : ent?.reason === "in_grace" ? "Grace Period"
                            : ent?.reason === "expired" ? "Expired"
                            : ent?.reason === "no_subscription" ? "No Subscription"
                            : ent?.reason || "—"}
                    </div>
                    {inGrace && ent?.valid_until && (
                        <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mt-1">
                            GRACE ENDS {fmtDate(ent.valid_until, { hour: undefined, minute: undefined })}
                        </div>
                    )}
                </PlanMetric>

                <PlanMetric icon={Sparkles} label="AUTO-RENEW" testid="plan-auto-renew">
                    <div className="font-display font-bold text-base">{sub?.auto_renew ? "Enabled" : "Disabled"}</div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-1">
                        {sub?.auto_renew ? "WILL RENEW AUTOMATICALLY" : "MANUAL RENEWAL REQUIRED"}
                    </div>
                </PlanMetric>
            </div>

            <div className="px-5 py-3 border-t border-[#1F1F1F] flex items-center gap-3 flex-wrap">
                {planId === "admin_grandfather" ? (
                    <div className="font-mono text-[10px] text-[#FFD700] tracking-widest flex items-center gap-1">
                        <Sparkles className="w-3 h-3" /> ADMIN GRANDFATHER GRANT — NO ACTION REQUIRED
                    </div>
                ) : (!isActive || (days !== null && days < 14)) ? (
                    <Link to="/subscription"
                        data-testid="upgrade-link"
                        className="bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium px-4 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                        <Sparkles className="w-3.5 h-3.5" /> {isActive ? "RENEW PLAN" : "ACTIVATE A PLAN"}
                    </Link>
                ) : (
                    <Link to="/subscription"
                        data-testid="manage-plan-link"
                        className="border border-[#1F1F1F] hover:border-[#333333] text-[#A1A1AA] px-4 py-2 text-xs font-mono tracking-widest flex items-center gap-2 transition-colors">
                        MANAGE PLAN <ExternalLink className="w-3 h-3" />
                    </Link>
                )}
            </div>
        </section>
    );
}

function PlanMetric({ icon: Icon, label, children, testid }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#050505] p-4" data-testid={testid}>
            <div className="flex items-center gap-2 mb-2">
                <Icon className="w-3.5 h-3.5 text-[#52525B]" />
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{label}</span>
            </div>
            {children}
        </div>
    );
}

function TransactionsCard({ txs }) {
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="transactions-card">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Receipt className="w-4 h-4 text-[#A1A1AA]" />
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">TRANSACTION HISTORY</div>
                    <div className="font-display font-bold text-lg tracking-tight">{txs.length} record{txs.length === 1 ? "" : "s"}</div>
                </div>
            </div>

            {txs.length === 0 ? (
                <div className="p-12 text-center" data-testid="transactions-empty">
                    <Receipt className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                    <div className="font-display font-bold text-base mb-1">No transactions yet</div>
                    <div className="text-sm text-[#A1A1AA]">Your payment history will appear here after your first purchase.</div>
                </div>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full text-left">
                        <thead className="border-b border-[#1F1F1F] bg-[#050505]">
                            <tr>
                                <Th>DATE</Th>
                                <Th>PLAN</Th>
                                <Th className="text-right">AMOUNT</Th>
                                <Th>STATUS</Th>
                                <Th>SESSION</Th>
                            </tr>
                        </thead>
                        <tbody>
                            {txs.map(t => {
                                const palette = STATUS_PALETTE[t.payment_status] || STATUS_PALETTE.unknown;
                                return (
                                    <tr key={t.id} className="border-b border-[#1F1F1F] hover:bg-[#050505] transition-colors"
                                        data-testid={`transaction-${t.id}`}>
                                        <Td>
                                            <div className="font-mono text-xs">{fmtDate(t.created_at)}</div>
                                            {t.completed_at && (
                                                <div className="font-mono text-[10px] text-[#52525B] tracking-wide mt-0.5">
                                                    Settled {fmtDate(t.completed_at, { year: undefined, hour: undefined, minute: undefined })}
                                                </div>
                                            )}
                                        </Td>
                                        <Td>
                                            <span className="font-display font-bold text-sm">{PLAN_LABELS[t.plan_id] || t.plan_id}</span>
                                        </Td>
                                        <Td className="text-right">
                                            <span className="font-display font-bold text-sm tabular-nums">
                                                ${t.amount_usd.toFixed(2)}
                                            </span>
                                            <span className="font-mono text-[10px] text-[#52525B] tracking-widest ml-1">{t.currency}</span>
                                        </Td>
                                        <Td>
                                            <span className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${palette.bd} ${palette.bg} ${palette.fg}`}>
                                                {palette.label}
                                            </span>
                                        </Td>
                                        <Td>
                                            <code className="font-mono text-[10px] text-[#52525B] tracking-wide">
                                                {t.session_id ? `${t.session_id.slice(0, 12)}…` : "—"}
                                            </code>
                                        </Td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            )}

            {txs.length > 0 && (
                <div className="px-5 py-3 border-t border-[#1F1F1F] flex items-start gap-2">
                    <AlertTriangle className="w-3.5 h-3.5 text-[#52525B] shrink-0 mt-0.5" />
                    <div className="font-mono text-[10px] text-[#52525B] tracking-wide leading-relaxed">
                        TRANSACTIONS IN <span className="text-[#FFB000]">PENDING</span> STATE ARE CHECKOUT SESSIONS THAT WERE NEVER COMPLETED.
                        ONLY <span className="text-[#00FF41]">COMPLETED</span> / <span className="text-[#00FF41]">PAID</span> TRANSACTIONS COUNT TOWARDS YOUR SUBSCRIPTION.
                    </div>
                </div>
            )}
        </section>
    );
}

const Th = ({ children, className = "" }) => (
    <th className={`px-4 py-2 font-mono text-[10px] text-[#52525B] tracking-widest font-normal ${className}`}>{children}</th>
);
const Td = ({ children, className = "" }) => (
    <td className={`px-4 py-3 ${className}`}>{children}</td>
);
