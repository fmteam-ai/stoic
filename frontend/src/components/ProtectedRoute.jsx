import { Navigate } from "react-router-dom";
import { Loader2, ShieldAlert } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { BackendOutage } from "@/components/BackendOutage";

export function ProtectedRoute({ children, requireAdmin = false, publicFallback = "/login" }) {
    const { user, outage, refresh } = useAuth();
    // Round-6 P0: exactly three explicit states — outage screen, login
    // boundary, or the authenticated app. NEVER the public welcome page.
    if (outage && !user) return <BackendOutage outage={outage} onRetry={refresh} />;
    if (user === null) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505]" data-testid="route-loading-screen">
                <div className="flex flex-col items-center gap-4">
                    <Loader2 className="h-8 w-8 text-emerald-400 animate-spin" />
                    <div className="font-mono text-sm text-emerald-300 tracking-widest animate-pulse">INITIALIZING TERMINAL...</div>
                </div>
            </div>
        );
    }
    if (user === false) return <Navigate to={publicFallback} replace state={{ from: window.location.pathname }} />;
    if (requireAdmin && user?.role !== "admin") return <Navigate to="/" replace />;
    if (requireAdmin && user?.admin_mfa_enforced && !user?.two_factor_enabled) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-4"
                data-testid="admin-mfa-gate">
                <div className="max-w-md w-full bg-[#0A0A0A] border border-[#FFB000]/40 p-8 text-center">
                    <ShieldAlert className="w-10 h-10 text-[#FFB000] mx-auto mb-4" />
                    <div className="font-display text-xl text-white mb-2">Admin MFA required</div>
                    <p className="text-sm text-[#A1A1AA] leading-6 mb-6">
                        Administrator accounts must enroll authenticator (TOTP) two-factor
                        authentication before admin functions unlock. It takes about a minute.
                    </p>
                    <a href="/settings" data-testid="admin-mfa-gate-settings-link"
                        className="inline-block px-5 py-2.5 bg-[#00FF41] text-black text-xs font-mono tracking-widest">
                        ENROLL 2FA IN SETTINGS →
                    </a>
                </div>
            </div>
        );
    }
    return children;
}
