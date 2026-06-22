import { useEffect, useState } from "react";
import { useSearchParams, useNavigate, Link } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { CheckCircle2, Loader2, AlertTriangle } from "lucide-react";

const MAX_ATTEMPTS = 10;
const POLL_INTERVAL_MS = 2000;

export default function SubscriptionSuccess() {
    const [params] = useSearchParams();
    const navigate = useNavigate();
    const sessionId = params.get("session_id");
    const [status, setStatus] = useState("polling"); // polling | paid | expired | error
    const [subscription, setSubscription] = useState(null);
    const [err, setErr] = useState("");

    useEffect(() => {
        if (!sessionId) {
            setStatus("error");
            setErr("Missing session_id in URL.");
            return;
        }
        let cancelled = false;
        let attempts = 0;

        const tick = async () => {
            if (cancelled) return;
            attempts += 1;
            try {
                const { data } = await api.get(`/subscription/poll/${sessionId}`);
                if (data.payment_status === "paid") {
                    setStatus("paid");
                    setSubscription(data.subscription);
                    return;
                }
                if (data.payment_status === "expired" || data.status === "expired") {
                    setStatus("expired");
                    return;
                }
                if (attempts >= MAX_ATTEMPTS) {
                    setStatus("error");
                    setErr("Payment status check timed out. Refresh to retry.");
                    return;
                }
                setTimeout(tick, POLL_INTERVAL_MS);
            } catch (e) {
                setErr(formatApiError(e));
                setStatus("error");
            }
        };
        tick();
        return () => { cancelled = true; };
    }, [sessionId]);

    return (
        <AppLayout>
            <PageHeader
                title="Payment Confirmation"
                subtitle="Verifying your subscription with Stripe…"
                testid="subscription-success-header"
            />
            <div className="p-4 md:p-8 max-w-2xl">
                <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-8 text-center"
                    data-testid="subscription-success-card">
                    {status === "polling" && (
                        <>
                            <Loader2 className="w-12 h-12 text-[#00FF41] mx-auto mb-4 animate-spin" />
                            <div className="font-display font-bold text-2xl tracking-tight mb-2">
                                Confirming payment…
                            </div>
                            <p className="text-sm text-[#A1A1AA]">
                                Stripe usually settles in a few seconds. Don&apos;t close this page.
                            </p>
                        </>
                    )}
                    {status === "paid" && (
                        <>
                            <CheckCircle2 className="w-12 h-12 text-[#00FF41] mx-auto mb-4" />
                            <div className="font-display font-bold text-2xl tracking-tight mb-2">
                                You&apos;re in! 🎉
                            </div>
                            <p className="text-sm text-[#A1A1AA] mb-2">
                                Subscription active until{" "}
                                <span className="text-white font-mono">
                                    {subscription?.valid_until ? new Date(subscription.valid_until).toLocaleDateString() : "—"}
                                </span>.
                            </p>
                            <p className="text-xs text-[#52525B] mb-6 font-mono tracking-widest">
                                PLAN · {subscription?.current_plan_id?.toUpperCase()}
                            </p>
                            <div className="flex items-center justify-center gap-3">
                                <Link to="/" data-testid="success-go-dashboard"
                                    className="px-4 py-2 bg-[#00FF41] text-black text-xs font-mono tracking-widest hover:bg-[#00E53A]">
                                    GO TO DASHBOARD
                                </Link>
                                <Link to="/bot" data-testid="success-go-bot"
                                    className="px-4 py-2 border border-[#1F1F1F] text-xs font-mono tracking-widest hover:border-[#00FF41]">
                                    CONFIGURE BOT
                                </Link>
                            </div>
                        </>
                    )}
                    {status === "expired" && (
                        <>
                            <AlertTriangle className="w-12 h-12 text-[#FFB000] mx-auto mb-4" />
                            <div className="font-display font-bold text-2xl tracking-tight mb-2">
                                Session expired
                            </div>
                            <p className="text-sm text-[#A1A1AA] mb-6">
                                The checkout session expired without a payment. Try again.
                            </p>
                            <button onClick={() => navigate("/subscription")}
                                data-testid="success-retry"
                                className="px-4 py-2 bg-[#FFB000] text-black text-xs font-mono tracking-widest hover:bg-[#E59E00]">
                                BACK TO PLANS
                            </button>
                        </>
                    )}
                    {status === "error" && (
                        <>
                            <AlertTriangle className="w-12 h-12 text-[#FF3B30] mx-auto mb-4" />
                            <div className="font-display font-bold text-2xl tracking-tight mb-2">
                                Couldn&apos;t verify
                            </div>
                            <p className="text-sm text-[#A1A1AA] mb-2">{err}</p>
                            <p className="text-xs text-[#52525B] mb-6 font-mono">
                                If you completed payment, refresh the page or contact support.
                            </p>
                            <button onClick={() => navigate("/subscription")}
                                data-testid="success-back"
                                className="px-4 py-2 border border-[#1F1F1F] text-xs font-mono tracking-widest hover:border-[#FF3B30]">
                                BACK TO PLANS
                            </button>
                        </>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}
