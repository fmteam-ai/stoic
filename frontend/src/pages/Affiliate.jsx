import { useEffect, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { AFFILIATE_TERMS, AFFILIATE_TERMS_VERSION } from "@/lib/affiliateTerms";
import { toast } from "sonner";
import {
    Users, Copy, CheckCircle2, AlertTriangle, Loader2, DollarSign,
    MousePointerClick, TrendingUp, Lock,
} from "lucide-react";
import { Link } from "react-router-dom";

const PAYMENT_METHODS = ["Stripe (debit/credit)", "PayPal", "Bank transfer"];

export default function Affiliate() {
    const { user } = useAuth();
    const [loading, setLoading] = useState(true);
    const [status, setStatus] = useState(null); // {state, affiliate?, application?}
    const [stats, setStats] = useState(null);
    const [err, setErr] = useState("");
    const [copied, setCopied] = useState(false);

    // Form state
    const [form, setForm] = useState({
        full_name: user?.name || "",
        audience_url: "",
        audience_size: "",
        promotion_strategy: "",
        payment_method: PAYMENT_METHODS[0],
        payment_details: "",
        terms_agreed: false,
    });
    const [submitting, setSubmitting] = useState(false);

    const load = async () => {
        setLoading(true);
        try {
            const { data: s } = await api.get("/affiliate/status");
            setStatus(s);
            if (s.state === "approved") {
                const { data: st } = await api.get("/affiliate/stats");
                setStats(st);
            }
        } catch (e) {
            setErr(formatApiError(e));
        } finally { setLoading(false); }
    };

    useEffect(() => { load(); }, []);

    const submit = async () => {
        if (!form.terms_agreed) {
            setErr("You must agree to the terms to apply.");
            return;
        }
        setErr(""); setSubmitting(true);
        try {
            await api.post("/affiliate/apply", { ...form, terms_version: AFFILIATE_TERMS_VERSION });
            toast.success("Application submitted — we'll review within 3 business days.");
            await load();
        } catch (e) {
            // Surface 402 sub gate as friendly message + CTA
            if (e?.response?.status === 402) {
                setErr("Active paid subscription required to apply for the affiliate program.");
            } else {
                setErr(formatApiError(e));
            }
        } finally { setSubmitting(false); }
    };

    const copyLink = async () => {
        const link = `${window.location.origin}/api/r/${status?.affiliate?.code}`;
        try {
            await navigator.clipboard.writeText(link);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch (e) {
            console.warn("[affiliate] clipboard copy failed", e?.message);
            toast.error("Couldn't copy — please copy manually");
        }
    };

    const requestPayout = async () => {
        if (!window.confirm(`Request payout of $${status.affiliate.unpaid_balance_usd?.toFixed(2)} via ${status.affiliate.payment_method}?\n\nAdmin processes payouts within 5 business days.`)) {
            return;
        }
        try {
            const { data } = await api.post("/affiliate/request-payout");
            toast.success(`Payout requested · $${data.amount_usd}`, {
                description: "We'll email you when it's processed.",
            });
            await load();
        } catch (e) {
            toast.error("Payout request failed", { description: formatApiError(e) });
        }
    };

    if (loading) {
        return (
            <AppLayout>
                <PageHeader title="Affiliate Program" subtitle="Earn 20% recurring on every referral." testid="affiliate-header" />
                <div className="p-8 text-[#52525B]"><Loader2 className="w-5 h-5 animate-spin" /></div>
            </AppLayout>
        );
    }

    return (
        <AppLayout>
            <PageHeader
                title="Affiliate Program"
                subtitle="Earn 20% recurring on every paid referral. 60-day attribution. $50 min payout."
                testid="affiliate-header"
            />

            <div className="p-4 md:p-8 space-y-6 max-w-5xl">
                {err && (
                    <div data-testid="affiliate-error" className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>
                )}

                {status?.state === "approved" && (
                    <ApprovedDashboard affiliate={status.affiliate} stats={stats}
                        onCopy={copyLink} copied={copied} onRequestPayout={requestPayout} />
                )}

                {status?.state === "pending" && (
                    <div data-testid="affiliate-pending"
                        className="border border-[#FFB000]/40 bg-[#FFB000]/5 p-6 flex items-center gap-4">
                        <AlertTriangle className="w-6 h-6 text-[#FFB000] shrink-0" />
                        <div>
                            <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-1">APPLICATION UNDER REVIEW</div>
                            <div className="text-sm">Thanks — we&apos;ll reach out within 3 business days. Submitted {new Date(status.application.submitted_at).toLocaleDateString()}.</div>
                        </div>
                    </div>
                )}

                {status?.state === "rejected" && (
                    <div data-testid="affiliate-rejected"
                        className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 p-6">
                        <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest mb-1">APPLICATION DECLINED</div>
                        <div className="text-sm mb-2">
                            {status.application.rejection_reason || "Unfortunately we couldn't approve your application at this time."}
                        </div>
                        <div className="font-mono text-[10px] text-[#52525B]">
                            Decided {new Date(status.application.rejected_at).toLocaleDateString()}
                        </div>
                    </div>
                )}

                {status?.state === "subscription_required" && (
                    <SubscriptionGate />
                )}

                {(status?.state === "none" || !status) && (
                    <ApplicationForm
                        form={form} setForm={setForm} submit={submit}
                        submitting={submitting} userEmail={user?.email} />
                )}
            </div>
        </AppLayout>
    );
}

function StatCard({ icon: Icon, label, value, accent }) {
    return (
        <div className={`bg-[#0A0A0A] border ${accent || "border-[#1F1F1F]"} p-4`}>
            <div className="flex items-center gap-2 mb-2">
                <Icon className="w-4 h-4 text-[#52525B]" />
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{label}</span>
            </div>
            <div className="font-display font-bold text-2xl tracking-tight">{value}</div>
        </div>
    );
}

function ApprovedDashboard({ affiliate, stats, onCopy, copied, onRequestPayout }) {
    const link = `${window.location.origin}/api/r/${affiliate.code}`;
    const balance = affiliate.unpaid_balance_usd ?? 0;
    const minPayout = stats?.min_payout_usd ?? 50;
    const canRequestPayout = balance >= minPayout;
    return (
        <div className="space-y-6" data-testid="affiliate-approved">
            <div className="border border-[#00FF41]/40 bg-[#00FF41]/5 p-6 space-y-4">
                <div className="flex items-center gap-2 mb-1">
                    <CheckCircle2 className="w-5 h-5 text-[#00FF41]" />
                    <span className="font-mono text-[10px] text-[#00FF41] tracking-widest">APPROVED · ACTIVE AFFILIATE</span>
                </div>
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">YOUR REFERRAL LINK</div>
                    <div className="flex items-center gap-2">
                        <code data-testid="affiliate-link"
                            className="flex-1 bg-[#050505] border border-[#1F1F1F] px-3 py-2 text-sm font-mono break-all">
                            {link}
                        </code>
                        <button onClick={onCopy} data-testid="copy-link"
                            className="px-3 py-2 text-xs font-mono tracking-widest bg-[#00FF41] text-black hover:bg-[#00E53A] flex items-center gap-1">
                            {copied ? <CheckCircle2 className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                            {copied ? "COPIED" : "COPY"}
                        </button>
                    </div>
                </div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    CODE · {affiliate.code} · 60-DAY ATTRIBUTION · 20% RECURRING
                </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <StatCard icon={MousePointerClick} label="LIFETIME CLICKS" value={affiliate.lifetime_clicks ?? 0} />
                <StatCard icon={Users} label="CONVERSIONS" value={affiliate.lifetime_conversions ?? 0} />
                <StatCard icon={TrendingUp} label="LIFETIME EARNINGS" value={`$${(affiliate.lifetime_earnings_usd ?? 0).toFixed(2)}`} />
                <StatCard icon={DollarSign} label="UNPAID BALANCE" value={`$${balance.toFixed(2)}`}
                    accent={canRequestPayout ? "border-[#00FF41]/40" : ""} />
            </div>

            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 flex items-center justify-between gap-4 flex-wrap"
                data-testid="payout-cta-row">
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">PAYOUT STATUS</div>
                    <div className="text-sm">
                        {canRequestPayout ? (
                            <span className="text-[#00FF41]">
                                You&apos;re eligible to request a payout of <span className="font-bold">${balance.toFixed(2)}</span>.
                            </span>
                        ) : (
                            <span className="text-[#A1A1AA]">
                                ${(minPayout - balance).toFixed(2)} more in unpaid commissions before you can request a payout.
                            </span>
                        )}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-1">
                        MINIMUM ${minPayout} · PAID VIA {affiliate.payment_method?.toUpperCase()}
                    </div>
                </div>
                <button onClick={onRequestPayout}
                    disabled={!canRequestPayout}
                    data-testid="request-payout-button"
                    className="px-5 py-2.5 text-xs font-mono tracking-widest bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-30 disabled:cursor-not-allowed text-black flex items-center gap-2">
                    <DollarSign className="w-4 h-4" /> REQUEST PAYOUT
                </button>
            </div>

            {stats?.recent_commissions?.length > 0 && (
                <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="commissions-list">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                        RECENT COMMISSIONS · {stats.recent_commissions.length}
                    </div>
                    <div className="divide-y divide-[#1F1F1F]">
                        {stats.recent_commissions.map(c => (
                            <div key={c.id} className="px-5 py-2.5 flex items-center gap-3 text-xs">
                                <span className="font-mono text-[#52525B] w-24">{new Date(c.created_at).toLocaleDateString()}</span>
                                <span className="font-mono text-[#A1A1AA]">{c.plan_id}</span>
                                <span className="flex-1" />
                                <span className="font-mono text-[#A1A1AA]">${c.sale_amount_usd}</span>
                                <span className="font-mono text-[#00FF41] font-bold">+${c.commission_usd}</span>
                                <span className={`font-mono text-[9px] tracking-widest px-1.5 py-0.5 border ${
                                    c.status === "paid" ? "border-[#00FF41] text-[#00FF41]" : "border-[#FFB000] text-[#FFB000]"
                                }`}>{c.status.toUpperCase()}</span>
                            </div>
                        ))}
                    </div>
                </div>
            )}

            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                MIN PAYOUT $50 · MONTHLY ON THE 15TH · PAYOUT VIA {affiliate.payment_method?.toUpperCase()}
            </div>
        </div>
    );
}

function ApplicationForm({ form, setForm, submit, submitting, userEmail }) {
    const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
    return (
        <div className="space-y-6">
            <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                <StatCard icon={DollarSign} label="COMMISSION" value="20%" accent="border-[#00FF41]/40" />
                <StatCard icon={MousePointerClick} label="ATTRIBUTION" value="60d" />
                <StatCard icon={TrendingUp} label="MIN PAYOUT" value="$50" />
                <StatCard icon={Users} label="DURATION" value="Lifetime" />
            </div>

            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 space-y-4" data-testid="affiliate-form">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">APPLICATION FORM</div>
                <Field label="Full name" value={form.full_name} onChange={v => set("full_name", v)} testid="aff-name" />
                <Field label="Audience URL (website / channel / newsletter)" value={form.audience_url}
                    onChange={v => set("audience_url", v)} testid="aff-url" placeholder="https://…" />
                <Field label="Audience size (optional)" value={form.audience_size}
                    onChange={v => set("audience_size", v)} testid="aff-size" placeholder="e.g. 12k newsletter subs" />
                <div>
                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1 block">
                        How will you promote STOIC? (required)
                    </label>
                    <textarea
                        value={form.promotion_strategy}
                        onChange={e => set("promotion_strategy", e.target.value)}
                        rows={4}
                        data-testid="aff-strategy"
                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none resize-none"
                        placeholder="YouTube reviews, dedicated landing page, trading-focused newsletter, etc." />
                </div>
                <div>
                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1 block">
                        Payment method (for payouts)
                    </label>
                    <select value={form.payment_method} onChange={e => set("payment_method", e.target.value)}
                        data-testid="aff-payment-method"
                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none">
                        {PAYMENT_METHODS.map(m => <option key={m} value={m}>{m}</option>)}
                    </select>
                </div>
                <Field label="Payment details (paypal email / bank info — optional)"
                    value={form.payment_details} onChange={v => set("payment_details", v)}
                    testid="aff-payment-details" />
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    EMAIL ON FILE · {userEmail}
                </div>
            </div>

            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5" data-testid="affiliate-terms-block">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">TERMS & CONDITIONS</div>
                <div className="bg-[#050505] border border-[#1F1F1F] p-4 max-h-[260px] overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-[#A1A1AA] font-mono">
                    {AFFILIATE_TERMS}
                </div>
                <label className="flex items-start gap-2 mt-4 cursor-pointer" data-testid="affiliate-terms-agree-wrap">
                    <input type="checkbox" checked={form.terms_agreed}
                        onChange={e => setForm(f => ({ ...f, terms_agreed: e.target.checked }))}
                        data-testid="affiliate-terms-agree"
                        className="mt-1" />
                    <span className="text-xs text-[#A1A1AA] leading-relaxed">
                        I have read and agree to the STOIC Affiliate Program Agreement
                        (Last updated: June 22, 2026), including the financial-marketing
                        compliance, anti-fraud, and self-referral restrictions.
                    </span>
                </label>
            </div>

            <div className="flex items-center gap-3">
                <button onClick={submit} disabled={submitting || !form.terms_agreed}
                    data-testid="affiliate-submit"
                    className="px-5 py-2.5 text-xs font-mono tracking-widest bg-[#00FF41] text-black hover:bg-[#00E53A] disabled:opacity-40 flex items-center gap-2">
                    {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Users className="w-4 h-4" />}
                    SUBMIT APPLICATION
                </button>
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    REVIEWED MANUALLY · USUALLY 1-3 BUSINESS DAYS
                </span>
            </div>
        </div>
    );
}

function Field({ label, value, onChange, placeholder, testid }) {
    return (
        <div>
            <label className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1 block">{label}</label>
            <input value={value} onChange={e => onChange(e.target.value)}
                placeholder={placeholder} data-testid={testid}
                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none" />
        </div>
    );
}


function SubscriptionGate() {
    return (
        <div data-testid="affiliate-sub-gate"
            className="border border-[#0099FF]/40 bg-[#0099FF]/5 p-8 text-center space-y-5">
            <Lock className="w-10 h-10 mx-auto text-[#0099FF]" />
            <div>
                <div className="font-mono text-[10px] text-[#0099FF] tracking-widest mb-1">SUBSCRIPTION REQUIRED</div>
                <div className="font-display font-bold text-2xl tracking-tight mb-2">Unlock the Affiliate Program</div>
                <div className="text-sm text-[#A1A1AA] max-w-xl mx-auto leading-relaxed">
                    Earn 20% recurring commission on every paid referral, with 60-day attribution.
                    Affiliates must hold an active paid plan — this keeps the program credible
                    and aligned with users who actually use the product.
                </div>
            </div>
            <div className="flex items-center justify-center gap-3 pt-2">
                <Link to="/subscription"
                    data-testid="affiliate-sub-gate-cta"
                    className="px-5 py-2.5 text-xs font-mono tracking-widest bg-[#0099FF] text-black hover:bg-[#33ADFF] inline-flex items-center gap-2">
                    <DollarSign className="w-4 h-4" />
                    VIEW SUBSCRIPTION PLANS
                </Link>
            </div>
        </div>
    );
}
