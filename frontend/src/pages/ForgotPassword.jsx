import { useState } from "react";
import { Link } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { StoicMark } from "@/components/StoicLogo";
import { Mail as EnvelopeSimple, MailCheck, Loader2, ArrowLeft } from "lucide-react";
import { TurnstileWidget, TurnstileStatus, useTurnstile } from "@/components/TurnstileWidget";

export default function ForgotPassword() {
    const [email, setEmail] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [sent, setSent] = useState(false);
    const [error, setError] = useState("");
    const [cooldown, setCooldown] = useState(0);
    const turnstile = useTurnstile("password_reset");

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError(""); setSubmitting(true);
        try {
            await api.post("/auth/forgot-password",
                { email, ...(turnstile.enabled && turnstile.token ? { turnstile_token: turnstile.token } : {}) });
            setSent(true);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            // Single-use token was consumed by the failed attempt — reset.
            if (turnstile.enabled) {
                turnstile.reset();
            }
            if (detail?.code === "turnstile_required") {
                setError(detail.message || "Please complete the human verification challenge.");
                setSubmitting(false);
                return;
            }
            if (detail?.code === "rate_limited") {
                const m = (detail.message || "").match(/(\d+)s/);
                const remaining = m ? parseInt(m[1], 10) : 60;
                setCooldown(remaining);
                setSent(true);
                const t = setInterval(() => {
                    setCooldown(c => {
                        if (c <= 1) { clearInterval(t); return 0; }
                        return c - 1;
                    });
                }, 1000);
            } else {
                setError(formatApiError(err));
            }
        } finally { setSubmitting(false); }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
            <div className="w-full max-w-sm" data-testid="forgot-password-page">
                <div className="flex items-center gap-3 mb-10">
                    <StoicMark size={40} />
                    <div>
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">RISK-CONTROLLED AUTOMATED TRADING</div>
                    </div>
                </div>

                {sent ? (
                    <div className="border border-[#FFD700]/40 bg-[#FFD700]/5 p-6" data-testid="forgot-sent-screen">
                        <div className="flex items-center gap-2 mb-3">
                            <MailCheck className="w-5 h-5 text-[#FFD700]" />
                            <div className="font-display font-bold text-xl">Check your inbox</div>
                        </div>
                        <p className="text-sm text-[#A1A1AA] leading-6 mb-3">
                            If an account exists for <strong className="text-white">{email}</strong>, a
                            password-reset link has been sent. The link expires in 1 hour.
                        </p>
                        {cooldown > 0 && (
                            <p className="text-xs text-[#FFD700] font-mono tracking-wide mb-3">
                                RESEND AVAILABLE IN {cooldown}s
                            </p>
                        )}
                        <p className="text-xs text-[#52525B] font-mono tracking-wide">
                            CHECK SPAM IF YOU DON'T SEE IT
                        </p>
                        <div className="mt-5">
                            <Link to="/login" data-testid="forgot-back-to-login"
                                className="text-sm text-[#00FF41] hover:underline flex items-center gap-1">
                                <ArrowLeft className="w-3 h-3" /> Back to sign in
                            </Link>
                        </div>
                    </div>
                ) : (
                    <>
                        <div className="font-mono text-[11px] text-[#FFD700] tracking-widest mb-3">// PASSWORD RESET</div>
                        <h2 className="font-display font-bold text-3xl tracking-tight mb-2">Forgot password?</h2>
                        <p className="text-sm text-[#A1A1AA] mb-8">
                            Enter your account email and we'll send you a link to choose a new one.
                        </p>

                        <form onSubmit={handleSubmit} className="space-y-4" data-testid="forgot-password-form">
                            <div>
                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">EMAIL</label>
                                <div className="relative">
                                    <EnvelopeSimple className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                                    <input
                                        type="email" required
                                        value={email}
                                        onChange={e => setEmail(e.target.value)}
                                        data-testid="forgot-email-input"
                                        className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#FFD700] outline-none px-10 py-3 text-sm transition-colors duration-150"
                                        placeholder="you@example.com"
                                        autoFocus
                                    />
                                </div>
                            </div>

                            {error && (
                                <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30]" data-testid="forgot-error">
                                    {error}
                                </div>
                            )}

                            {turnstile.enabled && (
                                <><TurnstileWidget siteKey={turnstile.siteKey} action="password_reset" onToken={turnstile.setToken} onState={turnstile.onState} resetRef={turnstile.resetRef} /><TurnstileStatus t={turnstile} /></>
                            )}
                            {!turnstile.enabled && <TurnstileStatus t={turnstile} />}

                            <button
                                type="submit" disabled={submitting || !email || !turnstile.canSubmit}
                                data-testid="forgot-submit-btn"
                                className="w-full bg-[#FFD700] hover:bg-[#E5C200] disabled:opacity-50 text-black font-medium py-3 text-sm transition-colors flex items-center justify-center gap-2"
                            >
                                {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
                                {submitting ? "SENDING..." : "SEND RESET LINK →"}
                            </button>
                        </form>

                        <div className="mt-6 text-sm text-[#A1A1AA]">
                            Remembered it?{" "}
                            <Link to="/login" data-testid="forgot-login-link" className="text-[#00FF41] hover:underline">
                                Back to sign in
                            </Link>
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}
