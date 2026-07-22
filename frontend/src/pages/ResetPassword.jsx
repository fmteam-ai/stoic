import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { StoicMark } from "@/components/StoicLogo";
import { Lock, CheckCircle2, AlertTriangle, Loader2 } from "lucide-react";

export default function ResetPassword() {
    const [params] = useSearchParams();
    const navigate = useNavigate();
    const token = params.get("token") || "";

    const [pwd, setPwd] = useState("");
    const [confirm, setConfirm] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [success, setSuccess] = useState(false);
    const [error, setError] = useState("");
    const [code, setCode] = useState("");

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError(""); setCode("");
        if (pwd.length < 8) { setError("Password must be at least 8 characters."); return; }
        if (pwd !== confirm) { setError("Passwords do not match."); return; }
        setSubmitting(true);
        try {
            await api.post("/auth/reset-password", { token, new_password: pwd });
            setSuccess(true);
            setTimeout(() => navigate("/login"), 2000);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            setCode(detail?.code || "");
            setError(detail?.message || formatApiError(err));
        } finally { setSubmitting(false); }
    };

    if (!token) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
                <div className="w-full max-w-md" data-testid="reset-password-missing-token">
                    <div className="flex items-center gap-3 mb-10">
                        <StoicMark size={40} />
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                    </div>
                    <div className="border border-amber-500/40 bg-amber-500/5 p-6">
                        <div className="flex items-center gap-2 mb-2">
                            <AlertTriangle className="w-5 h-5 text-amber-400" />
                            <div className="font-display font-bold text-lg">Missing reset token</div>
                        </div>
                        <p className="text-sm text-[#A1A1AA] mb-4">
                            This link is incomplete. Request a new one from the forgot-password page.
                        </p>
                        <Link to="/forgot-password" className="text-[#FFD700] hover:underline text-sm">
                            Send a new reset link →
                        </Link>
                    </div>
                </div>
            </div>
        );
    }

    if (success) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
                <div className="w-full max-w-md" data-testid="reset-success">
                    <div className="flex items-center gap-3 mb-10">
                        <StoicMark size={40} />
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                    </div>
                    <div className="border border-[#00FF41]/40 bg-[#00FF41]/5 p-6">
                        <div className="flex items-center gap-2 mb-2">
                            <CheckCircle2 className="w-6 h-6 text-[#00FF41]" />
                            <div className="font-display font-bold text-xl">Password updated</div>
                        </div>
                        <p className="text-sm text-[#A1A1AA]">
                            Redirecting you to sign in with your new password…
                        </p>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
            <div className="w-full max-w-sm" data-testid="reset-password-page">
                <div className="flex items-center gap-3 mb-10">
                    <StoicMark size={40} />
                    <div>
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SMART TRADING · STEADY WEALTH</div>
                    </div>
                </div>

                <div className="font-mono text-[11px] text-[#FFD700] tracking-widest mb-3">// CHOOSE A NEW PASSWORD</div>
                <h2 className="font-display font-bold text-3xl tracking-tight mb-2">Reset password</h2>
                <p className="text-sm text-[#A1A1AA] mb-8">Pick a password you'll remember — at least 8 characters.</p>

                <form onSubmit={handleSubmit} className="space-y-4" data-testid="reset-password-form">
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">NEW PASSWORD</label>
                        <div className="relative">
                            <Lock className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                            <input
                                type="password" required minLength={8}
                                value={pwd}
                                onChange={e => setPwd(e.target.value)}
                                data-testid="reset-password-input"
                                className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#FFD700] outline-none px-10 py-3 text-sm transition-colors duration-150"
                                placeholder="At least 8 characters"
                                autoFocus
                            />
                        </div>
                    </div>
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">CONFIRM PASSWORD</label>
                        <div className="relative">
                            <Lock className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                            <input
                                type="password" required minLength={8}
                                value={confirm}
                                onChange={e => setConfirm(e.target.value)}
                                data-testid="reset-password-confirm-input"
                                className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#FFD700] outline-none px-10 py-3 text-sm transition-colors duration-150"
                                placeholder="Re-enter password"
                            />
                        </div>
                    </div>

                    {error && (
                        <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30] space-y-1" data-testid="reset-error">
                            <div>{error}</div>
                            {(code === "invalid_token" || code === "expired_token") && (
                                <Link to="/forgot-password" data-testid="reset-error-new-link" className="text-[#FFD700] hover:underline block">
                                    Request a new reset link →
                                </Link>
                            )}
                        </div>
                    )}

                    <button
                        type="submit" disabled={submitting}
                        data-testid="reset-submit-btn"
                        className="w-full bg-[#FFD700] hover:bg-[#E5C200] disabled:opacity-50 text-black font-medium py-3 text-sm transition-colors flex items-center justify-center gap-2"
                    >
                        {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                        {submitting ? "UPDATING..." : "UPDATE PASSWORD →"}
                    </button>
                </form>
            </div>
        </div>
    );
}
