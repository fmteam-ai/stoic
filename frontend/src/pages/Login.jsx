import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import api, { formatApiError } from "@/lib/api";
import { Mail as EnvelopeSimple, Lock as LockKey, ShieldCheck, MailWarning, RefreshCw } from "lucide-react";
import { StoicMark } from "@/components/StoicLogo";
import { TurnstileWidget, TurnstileStatus, useTurnstile } from "@/components/TurnstileWidget";

export default function Login() {
    const navigate = useNavigate();
    const { login, resendActivation } = useAuth();
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [totpCode, setTotpCode] = useState("");
    const [needs2fa, setNeeds2fa] = useState(false);
    const [emailOtp, setEmailOtp] = useState("");
    const [needsEmailOtp, setNeedsEmailOtp] = useState(false);
    const [otpMessage, setOtpMessage] = useState("");
    const [otpResendCooldown, setOtpResendCooldown] = useState(0);
    const [error, setError] = useState("");
    const [sysState, setSysState] = useState("checking");
    useEffect(() => {
        let alive = true;
        api.get("/status").then(r => { if (alive) setSysState(r.data?.overall || "unknown"); })
            .catch(() => { if (alive) setSysState("unreachable"); });
        return () => { alive = false; };
    }, []);
    const [unverifiedEmail, setUnverifiedEmail] = useState("");
    const [resendCooldown, setResendCooldown] = useState(0);
    const [loading, setLoading] = useState(false);
    const turnstile = useTurnstile("login");

    const startOtpCooldown = (secs) => {
        setOtpResendCooldown(secs);
        const t = setInterval(() => {
            setOtpResendCooldown(c => {
                if (c <= 1) { clearInterval(t); return 0; }
                return c - 1;
            });
        }, 1000);
    };

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError("");
        setUnverifiedEmail("");
        setLoading(true);
        try {
            const u = await login(email, password,
                needs2fa ? totpCode : undefined,
                needsEmailOtp ? emailOtp : undefined,
                turnstile.enabled ? turnstile.token : undefined);
            navigate(u?.must_change_password ? "/settings" : "/");
        } catch (err) {
            const detail = err?.response?.data?.detail;
            // Turnstile tokens are single-use: the failed attempt consumed
            // this one server-side, so always issue a fresh token before the
            // next submit (wrong password, OTP challenge, 2FA, etc.).
            if (turnstile.enabled) {
                turnstile.reset();
            }
            if (detail?.code === "turnstile_required") {
                setError(detail.message || "Please complete the human verification challenge.");
                return;
            }
            // Unverified account: surface friendly UI with resend link
            if (detail?.code === "account_unverified") {
                setUnverifiedEmail(detail.email || email);
                setError("");
                return;
            }
            // Email OTP challenge issued (or still pending) — normal or Turnstile-degraded path
            if (detail?.code === "email_otp_sent" || detail?.code === "turnstile_degraded_otp_sent") {
                setNeedsEmailOtp(true);
                setOtpMessage(detail.message || "Enter the 6-digit code we emailed you.");
                setError("");
                if (detail.resend_in) startOtpCooldown(detail.resend_in);
                return;
            }
            if (detail?.code === "invalid_email_otp" || detail?.code === "turnstile_degraded_otp_invalid") {
                setError(`${detail.message}${detail.attempts_left ? ` (${detail.attempts_left} attempts left)` : ""}`);
                return;
            }
            if (detail?.code === "email_otp_expired" || detail?.code === "turnstile_degraded_otp_expired") {
                setEmailOtp("");
                setError(detail.message || "Code expired. Request a new one.");
                setOtpResendCooldown(0);
                return;
            }
            if (detail?.code === "email_otp_send_failed" || detail?.code === "turnstile_unavailable") {
                setError(detail.message);
                return;
            }
            const msg = formatApiError(err);
            // Backend returns 401 + detail "2FA code required" → show TOTP input
            if (!needs2fa && msg && msg.toLowerCase().includes("2fa code required")) {
                setNeeds2fa(true);
                setError("");
            } else {
                setError(msg);
            }
        } finally { setLoading(false); }
    };

    const handleResend = async () => {
        if (resendCooldown > 0 || !unverifiedEmail) return;
        try {
            await resendActivation(unverifiedEmail);
            setResendCooldown(60);
            const t = setInterval(() => {
                setResendCooldown(c => {
                    if (c <= 1) { clearInterval(t); return 0; }
                    return c - 1;
                });
            }, 1000);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            if (detail?.code === "rate_limited") {
                const m = (detail.message || "").match(/(\d+)s/);
                if (m) setResendCooldown(parseInt(m[1], 10));
            }
        }
    };

    const handleOtpResend = async () => {
        if (otpResendCooldown > 0) return;
        setError("");
        try {
            await login(email, password, undefined, undefined, turnstile.enabled ? turnstile.token : undefined);
        } catch (err) {
            const detail = err?.response?.data?.detail;
            if (turnstile.enabled) turnstile.reset();
            if (detail?.code === "email_otp_sent" || detail?.code === "turnstile_degraded_otp_sent") {
                setOtpMessage("A new code is on its way. Check your inbox.");
                if (detail.resend_in) startOtpCooldown(detail.resend_in);
            } else {
                setError(formatApiError(err));
            }
        }
    };

    return (
        <div className="min-h-screen flex bg-[#050505]">
            {/* Left panel - hidden on mobile */}
            <div className="hidden lg:flex lg:w-1/2 relative border-r border-[#1F1F1F] grid-bg">
                <div className="absolute inset-0 bg-gradient-to-br from-transparent via-transparent to-[#050505]" />
                <div className="relative z-10 flex flex-col justify-between p-12 w-full">
                    <div className="flex items-center gap-3">
                        <StoicMark size={48} />
                        <div>
                            <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">RISK-CONTROLLED AUTOMATED TRADING</div>
                        </div>
                    </div>

                    <div className="space-y-6 max-w-md">
                        <div className={`font-mono text-[11px] tracking-widest ${sysState === "operational" ? "text-[#00FF41]" : sysState === "checking" ? "text-[#52525B]" : "text-[#FFB000]"}`} data-testid="login-system-state">
                            {sysState === "operational" ? "// SYSTEM OPERATIONAL" : sysState === "checking" ? "// CHECKING SYSTEM STATE…" : sysState === "unreachable" ? "// BACKEND UNREACHABLE" : `// SYSTEM ${sysState.toUpperCase().replaceAll("_", " ")}`}
                        </div>
                        <h1 className="font-display font-bold text-5xl tracking-tighter leading-[1.05]">
                            Automated gold &amp; crypto<br/>trading, <span className="text-[#00FF41]">risk-controlled</span>.
                        </h1>
                        <p className="text-[#A1A1AA] text-sm leading-relaxed max-w-sm">
                            Multi-engine AI consensus. Four risk profiles. Veto-first execution. Built for traders who refuse to panic.
                        </p>
                    </div>

                    <div className="grid grid-cols-3 gap-4 max-w-md">
                        {[
                            { label: "Asset Classes", value: "FX • Crypto • Gold" },
                            { label: "AI Engines", value: "Multi-model consensus" },
                            { label: "Risk Tiers", value: "4 Profiles" },
                        ].map(s => (
                            <div key={s.label} className="border border-[#1F1F1F] p-3">
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">{s.label.toUpperCase()}</div>
                                <div className="text-xs font-medium">{s.value}</div>
                            </div>
                        ))}
                    </div>
                </div>
            </div>

            {/* Right panel - login form */}
            <div className="flex-1 flex items-center justify-center p-6">
                <div className="w-full max-w-sm">
                    <div className="lg:hidden flex items-center gap-2.5 mb-10">
                        <StoicMark size={28} />
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                    </div>

                    <div className="font-mono text-[11px] text-[#00FF41] tracking-widest mb-3">// AUTHENTICATE</div>
                    <h2 className="font-display font-bold text-3xl tracking-tight mb-2">Welcome back</h2>
                    <p className="text-sm text-[#A1A1AA] mb-8">Sign in to your trading terminal.</p>

                    <form onSubmit={handleSubmit} className="space-y-4" data-testid="login-form">
                        <div>
                                <label htmlFor="login-email" className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">EMAIL</label>
                            <div className="relative">
                                <EnvelopeSimple className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                                <input
                                    type="email"
                                    id="login-email"
                                    name="email"
                                    autoComplete="email"
                                    value={email}
                                    onChange={e => setEmail(e.target.value)}
                                    required
                                    data-testid="login-email-input"
                                    className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-10 py-3 text-sm transition-colors duration-150"
                                    placeholder="you@example.com"
                                />
                            </div>
                        </div>

                        <div>
                                <label htmlFor="login-password" className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">PASSWORD</label>
                            <div className="relative">
                                <LockKey className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                                <input
                                    type="password"
                                    id="login-password"
                                    name="password"
                                    autoComplete="current-password"
                                    value={password}
                                    onChange={e => setPassword(e.target.value)}
                                    required
                                    data-testid="login-password-input"
                                    className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-10 py-3 text-sm transition-colors duration-150"
                                    placeholder="••••••••"
                                />
                            </div>
                        </div>

                        {needs2fa && (
                            <div data-testid="login-2fa-block">
                                <label className="font-mono text-[10px] text-[#00FF41] tracking-widest block mb-2 flex items-center gap-1.5">
                                    <ShieldCheck className="w-3 h-3" /> 2FA CODE
                                </label>
                                <input
                                    type="text"
                                    value={totpCode}
                                    onChange={e => setTotpCode(e.target.value.replace(/\s/g, "").slice(0, 12))}
                                    inputMode="numeric"
                                    autoComplete="one-time-code"
                                    autoFocus
                                    required
                                    data-testid="login-2fa-input"
                                    placeholder="123456 or recovery code"
                                    className="w-full bg-[#0A0A0A] border border-[#00FF41]/40 focus:border-[#00FF41] outline-none px-3 py-3 text-lg font-mono tracking-[0.3em] transition-colors duration-150"
                                />
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-1.5">
                                    ENTER THE CODE FROM YOUR AUTHENTICATOR APP
                                </div>
                            </div>
                        )}

                        {needsEmailOtp && (
                            <div data-testid="login-email-otp-block">
                                <label className="font-mono text-[10px] text-[#00FF41] tracking-widest block mb-2 flex items-center gap-1.5">
                                    <EnvelopeSimple className="w-3 h-3" /> EMAIL CODE
                                </label>
                                <input
                                    type="text"
                                    value={emailOtp}
                                    onChange={e => setEmailOtp(e.target.value.replace(/\D/g, "").slice(0, 6))}
                                    inputMode="numeric"
                                    autoComplete="one-time-code"
                                    autoFocus
                                    required
                                    data-testid="login-email-otp-input"
                                    placeholder="123456"
                                    className="w-full bg-[#0A0A0A] border border-[#00FF41]/40 focus:border-[#00FF41] outline-none px-3 py-3 text-lg font-mono tracking-[0.3em] transition-colors duration-150"
                                />
                                <div className="flex items-center justify-between mt-1.5">
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                                        {otpMessage || "ENTER THE 6-DIGIT CODE WE EMAILED YOU"}
                                    </div>
                                    <button
                                        type="button"
                                        onClick={handleOtpResend}
                                        disabled={otpResendCooldown > 0}
                                        data-testid="login-email-otp-resend"
                                        className="font-mono text-[10px] tracking-widest text-[#00FF41] disabled:text-[#52525B] hover:underline flex items-center gap-1 flex-shrink-0 ml-2"
                                    >
                                        <RefreshCw className="w-3 h-3" />
                                        {otpResendCooldown > 0 ? `RESEND IN ${otpResendCooldown}s` : "RESEND CODE"}
                                    </button>
                                </div>
                            </div>
                        )}

                        {error && (
                            <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30]" data-testid="login-error">
                                {error}
                            </div>
                        )}

                        {unverifiedEmail && (
                            <div className="bg-amber-500/10 border border-amber-500/30 p-3 text-xs space-y-2" data-testid="login-unverified-block">
                                <div className="flex items-center gap-2 text-amber-400">
                                    <MailWarning className="w-4 h-4" />
                                    <strong>Activate your account</strong>
                                </div>
                                <p className="text-[#A1A1AA] leading-5">
                                    We sent an activation link to <strong className="text-white">{unverifiedEmail}</strong>. Click it to unlock your dashboard.
                                </p>
                                <button
                                    type="button"
                                    onClick={handleResend}
                                    disabled={resendCooldown > 0}
                                    data-testid="login-resend-activation-btn"
                                    className="w-full border border-amber-500/40 hover:bg-amber-500/10 disabled:opacity-50 text-amber-400 py-2 font-mono tracking-widest flex items-center justify-center gap-2"
                                >
                                    <RefreshCw className="w-3 h-3" />
                                    {resendCooldown > 0 ? `RESEND IN ${resendCooldown}s` : "RESEND ACTIVATION EMAIL"}
                                </button>
                            </div>
                        )}

                        {turnstile.enabled && (
                            <><TurnstileWidget siteKey={turnstile.siteKey} action="login" onToken={turnstile.setToken} onState={turnstile.onState} resetRef={turnstile.resetRef} /><TurnstileStatus t={turnstile} /></>
                        )}
                        {!turnstile.enabled && <TurnstileStatus t={turnstile} />}

                        <button
                            type="submit"
                            disabled={loading || !turnstile.canSubmit}
                            data-testid="login-submit-button"
                            className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium py-3 text-sm transition-colors duration-150"
                        >
                            {loading ? "AUTHENTICATING..." : (needs2fa ? "VERIFY &amp; SIGN IN →" : "SIGN IN →")}
                        </button>

                        <div className="text-right">
                            <Link to="/forgot-password" data-testid="login-forgot-password-link"
                                className="font-mono text-[10px] text-[#FFD700] tracking-widest hover:underline">
                                FORGOT PASSWORD? →
                            </Link>
                        </div>
                    </form>

                    <div className="mt-6 text-sm text-[#A1A1AA]">
                        New to the terminal?{" "}
                        <Link to="/register" data-testid="login-register-link" className="text-[#00FF41] hover:underline">Create account</Link>
                    </div>

                    <div className="mt-3 text-sm">
                        <Link to="/affiliates" data-testid="login-affiliate-link"
                            className="font-mono text-[10px] text-[#FFD700] tracking-widest hover:underline">
                            EARN 20% RECURRING · BECOME AN AFFILIATE →
                        </Link>
                    </div>

                    {/* audit F-15 — legal navigation on every public page */}
                    <div className="mt-6 pt-4 border-t border-[#1F1F1F] flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] tracking-widest text-[#52525B]"
                        data-testid="login-legal-links">
                        <Link to="/terms" className="hover:text-[#A1A1AA]">TERMS OF USE</Link>
                        <Link to="/privacy" className="hover:text-[#A1A1AA]">PRIVACY</Link>
                        <Link to="/risk-disclosure" className="hover:text-[#A1A1AA]">RISK DISCLOSURE</Link>
                    </div>
                    <p className="mt-2 font-mono text-[9px] text-[#52525B] leading-relaxed" data-testid="login-risk-note">
                        Trading involves substantial risk of loss. STOIC is automated trading
                        software, not a broker, fund or investment advisor. Past performance
                        does not guarantee future results.
                    </p>
                </div>
            </div>
        </div>
    );
}
