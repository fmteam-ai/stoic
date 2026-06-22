import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import api from "@/lib/api";
import { AlertTriangle, X } from "lucide-react";

const DISMISS_KEY = "subscription_banner_dismissed_until";
const SOON_THRESHOLD_DAYS = 7;

/**
 * Top-bar nag if the user is unsubscribed, in grace period, or close to expiry.
 * Admins (grandfathered) never see this. Dismissals last 24h.
 */
export function SubscriptionBanner() {
    const { user } = useAuth();
    const [state, setState] = useState(null);

    useEffect(() => {
        if (!user || !user.id) return;
        let cancel = false;
        api.get("/subscription/status").then(({ data }) => {
            if (!cancel) setState(data);
        }).catch((err) => console.warn("[sub-banner] status failed", err?.message));
        return () => { cancel = true; };
    }, [user]);

    if (!state) return null;
    const { entitlement, subscription } = state;
    if (subscription?.current_plan_id === "admin_grandfather") return null;

    const dismissedUntil = parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10);
    if (Date.now() < dismissedUntil) return null;

    let level = null; // 'expired' | 'grace' | 'soon' | null
    let daysLeft = null;
    let message = null;

    if (!entitlement.active) {
        level = "expired";
        message = "Your subscription expired — live trading is paused. Paper trading still works.";
    } else if (entitlement.in_grace) {
        const grace = new Date(entitlement.grace_until);
        daysLeft = Math.max(0, Math.ceil((grace - new Date()) / 86400000));
        level = "grace";
        message = `Free grace period ends in ${daysLeft} day${daysLeft === 1 ? "" : "s"}. Subscribe to keep live trading.`;
    } else if (entitlement.valid_until) {
        const vu = new Date(entitlement.valid_until);
        daysLeft = Math.ceil((vu - new Date()) / 86400000);
        if (daysLeft <= SOON_THRESHOLD_DAYS && daysLeft >= 0) {
            level = "soon";
            message = `Subscription expires in ${daysLeft} day${daysLeft === 1 ? "" : "s"} — renew to avoid interruption.`;
        }
    }
    if (!level) return null;

    const colorMap = {
        expired: "bg-[#FF3B30]/10 border-[#FF3B30]/40 text-[#FF3B30]",
        grace: "bg-[#FFB000]/10 border-[#FFB000]/40 text-[#FFB000]",
        soon: "bg-[#FFB000]/10 border-[#FFB000]/40 text-[#FFB000]",
    };

    const dismiss = () => {
        localStorage.setItem(DISMISS_KEY, String(Date.now() + 86400000));
        setState(null);
    };

    return (
        <div data-testid="subscription-banner-bar"
            data-no-capture
            className={`flex items-center justify-between gap-3 px-4 py-2 border-b ${colorMap[level]}`}>
            <div className="flex items-center gap-2 text-xs">
                <AlertTriangle className="w-4 h-4 shrink-0" />
                <span className="font-mono">{message}</span>
            </div>
            <div className="flex items-center gap-2">
                <Link to="/subscription"
                    data-testid="banner-subscribe-cta"
                    className="px-3 py-1 text-[10px] font-mono tracking-widest bg-white/10 hover:bg-white/20">
                    {level === "expired" ? "SUBSCRIBE" : "RENEW"}
                </Link>
                <button onClick={dismiss}
                    data-testid="banner-dismiss"
                    className="opacity-60 hover:opacity-100">
                    <X className="w-4 h-4" />
                </button>
            </div>
        </div>
    );
}
