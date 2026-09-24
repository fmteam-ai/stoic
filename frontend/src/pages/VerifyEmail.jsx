import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { formatApiError } from "@/lib/api";
import { StoicMark } from "@/components/StoicLogo";
import { CheckCircle2, AlertTriangle, Loader2, MailWarning } from "lucide-react";

export default function VerifyEmail() {
    const navigate = useNavigate();
    const [params] = useSearchParams();
    const { verifyEmail } = useAuth();
    const token = params.get("token") || "";

    const [state, setState] = useState({
        status: token ? "loading" : "missing", // loading | success | error | missing
        message: "",
        code: "",
    });

    useEffect(() => {
        if (!token) return;
        let mounted = true;
        verifyEmail(token)
            .then(res => {
                if (!mounted) return;
                setState({ status: "success", message: res?.message || "Email verified.", code: "" });
                // Auto-redirect to dashboard after 1.8s
                setTimeout(() => { if (mounted) navigate("/"); }, 1800);
            })
            .catch(err => {
                if (!mounted) return;
                const detail = err?.response?.data?.detail;
                setState({
                    status: "error",
                    code: detail?.code || "",
                    message: detail?.message || formatApiError(err),
                });
            });
        return () => { mounted = false; };
    }, [token, verifyEmail, navigate]);

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
            <div className="w-full max-w-md" data-testid="verify-email-page">
                <div className="flex items-center gap-3 mb-10">
                    <StoicMark size={40} />
                    <div>
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">RISK-CONTROLLED AUTOMATED TRADING</div>
                    </div>
                </div>

                {state.status === "missing" && (
                    <div className="border border-amber-500/40 bg-amber-500/5 p-6" data-testid="verify-missing">
                        <div className="flex items-center gap-2 mb-2">
                            <MailWarning className="w-5 h-5 text-amber-400" />
                            <div className="font-display font-bold text-lg">No activation token</div>
                        </div>
                        <p className="text-sm text-[#A1A1AA] mb-4">
                            This link is incomplete. Please use the exact link from the activation email.
                        </p>
                        <Link to="/register" className="text-[#00FF41] text-sm hover:underline" data-testid="verify-back-register">
                            Back to register →
                        </Link>
                    </div>
                )}

                {state.status === "loading" && (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6 flex items-center gap-3 text-[#A1A1AA]"
                         data-testid="verify-loading">
                        <Loader2 className="w-5 h-5 animate-spin text-[#FFD700]" />
                        Activating your STOIC membership…
                    </div>
                )}

                {state.status === "success" && (
                    <div className="border border-[#00FF41]/40 bg-[#00FF41]/5 p-6" data-testid="verify-success">
                        <div className="flex items-center gap-2 mb-2">
                            <CheckCircle2 className="w-6 h-6 text-[#00FF41]" />
                            <div className="font-display font-bold text-xl">You're in.</div>
                        </div>
                        <p className="text-sm text-[#A1A1AA]">
                            Your STOIC membership is active. Redirecting to your dashboard…
                        </p>
                    </div>
                )}

                {state.status === "error" && (
                    <div className="border border-red-500/40 bg-red-500/5 p-6" data-testid="verify-error">
                        <div className="flex items-center gap-2 mb-2">
                            <AlertTriangle className="w-5 h-5 text-red-400" />
                            <div className="font-display font-bold text-lg">
                                {state.code === "expired_token" ? "Link expired" : "Activation failed"}
                            </div>
                        </div>
                        <p className="text-sm text-[#A1A1AA] mb-4">{state.message}</p>
                        <div className="text-sm space-x-4">
                            <Link to="/register" className="text-[#00FF41] hover:underline" data-testid="verify-error-register-link">
                                Register again →
                            </Link>
                            <Link to="/login" className="text-[#FFD700] hover:underline" data-testid="verify-error-login-link">
                                Sign in instead →
                            </Link>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
