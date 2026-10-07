import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import api, { formatApiError } from "@/lib/api";
import { StoicMark } from "@/components/StoicLogo";
import { MailCheck, RefreshCw, Loader2 } from "lucide-react";
import { TurnstileWidget, TurnstileStatus, useTurnstile } from "@/components/TurnstileWidget";

export default function Register() {
    const navigate = useNavigate();
    const { register, resendActivation } = useAuth();
    const [email, setEmail] = useState("");
    const [name, setName] = useState("");
    const [password, setPassword] = useState("");
    const [termsAgreed, setTermsAgreed] = useState(false);
    const [signups, setSignups] = useState(null);   // testing period: {closed, message} | {unavailable: true}
    useEffect(() => {
        // A16-1 — fail CLOSED: if the status call fails we do not know whether sign-ups are open → no form
        api.get("/auth/signups-status").then((r) => setSignups(r.data)).catch(() => setSignups({ unavailable: true }));
    }, []);
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);

    // Post-registration "check your inbox" state
    const [registered, setRegistered] = useState(null); // { email, activationLinkDevOnly? }
    const [resendCooldown, setResendCooldown] = useState(0);
    const turnstile = useTurnstile("register");
    const [resending, setResending] = useState(false);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError("");
        if (password.length < 8) { setError("Password must be at least 8 characters."); return; }
        if (!termsAgreed) { setError("You must accept the Terms of Use to continue."); return; }
        setLoading(true);
        try {
            const data = await register(email, password, name || undefined,
                { terms_agreed: true, ...(turnstile.enabled && turnstile.token ? { turnstile_token: turnstile.token } : {}) });
            setRegistered({
                email,
                activationLinkDevOnly: data?.activation_link_dev_only || null,
                emailDeliveryError: null,
            });
        } catch (err) {
            // Single-use token was consumed by the failed attempt — reset.
            if (turnstile.enabled) {
                turnstile.reset();
            }
            setError(formatApiError(err));
        } finally { setLoading(false); }
    };

    const handleResend = async () => {
        if (resendCooldown > 0 || resending) return;
        setResending(true);
        try {
            await resendActivation(registered.email);
            setResendCooldown(60);
            const interval = setInterval(() => {
                setResendCooldown(c => {
                    if (c <= 1) { clearInterval(interval); return 0; }
                    return c - 1;
                });
            }, 1000);
        } catch (err) {
            // 429 cooldown → mirror remaining seconds
            const detail = err?.response?.data?.detail;
            if (detail?.code === "rate_limited") {
                const match = (detail.message || "").match(/(\d+)s/);
                if (match) setResendCooldown(parseInt(match[1], 10));
            } else {
                setError(formatApiError(err));
            }
        } finally { setResending(false); }
    };

    // ─── A16-1 — status still loading: no form yet ───
    if (signups === null) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6" data-testid="register-status-loading">
                <div className="font-mono text-xs text-[#52525B] tracking-widest">// CHECKING REGISTRATION STATUS…</div>
            </div>
        );
    }

    // ─── A16-1 — status unknown: fail closed (no form) ───
    if (signups?.unavailable) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
                <div className="w-full max-w-sm" data-testid="register-status-unavailable">
                    <div className="flex items-center gap-3 mb-10">
                        <StoicMark size={40} />
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                    </div>
                    <div className="font-mono text-[11px] text-[#FFB020] tracking-widest mb-3">// REGISTRATION STATUS UNAVAILABLE</div>
                    <p className="text-sm text-[#A1A1AA] leading-relaxed" data-testid="register-unavailable-message">
                        Registration status unavailable — try again later. During the closed testing period new sign-ups are only accepted when the server confirms they are open.
                    </p>
                    <Link to="/login" data-testid="register-unavailable-login-link"
                          className="inline-block mt-8 font-mono text-xs tracking-widest text-[#00FF41] hover:underline">SIGN IN →</Link>
                </div>
            </div>
        );
    }

    // ─── Testing period: registrations closed ───
    if (signups?.closed) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
                <div className="w-full max-w-sm" data-testid="register-closed">
                    <div className="flex items-center gap-3 mb-10">
                        <StoicMark size={40} />
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                    </div>
                    <div className="font-mono text-[11px] text-[#FFB020] tracking-widest mb-3">// CLOSED TESTING PERIOD — REGISTRATIONS CLOSED</div>
                    <p className="text-sm text-[#A1A1AA] leading-relaxed" data-testid="register-closed-message">{signups.message}</p>
                    <Link to="/login" data-testid="register-closed-login-link"
                          className="inline-block mt-8 font-mono text-xs tracking-widest text-[#00FF41] hover:underline">SIGN IN →</Link>
                </div>
            </div>
        );
    }

    // ─── Success screen (post-register) ───
    if (registered) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
                <div className="w-full max-w-md" data-testid="register-success-screen">
                    <div className="flex items-center gap-3 mb-10">
                        <StoicMark size={40} />
                        <div>
                            <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">VETO-FIRST AUTOMATED TRADING · NOTHING TRADES UNTIL READINESS PASSES</div>
                        </div>
                    </div>

                    <div className="border border-[#00FF41]/40 bg-[#00FF41]/5 p-6 mb-6">
                        <div className="flex items-center gap-3 mb-3">
                            <MailCheck className="w-6 h-6 text-[#00FF41]" />
                            <div className="font-display font-bold text-xl tracking-tight">Check your inbox</div>
                        </div>
                        <p className="text-sm text-[#A1A1AA] leading-6">
                            We've sent an activation link to <strong className="text-white">{registered.email}</strong>.
                            Click the link inside to activate your STOIC membership.
                        </p>
                        <p className="text-xs text-[#52525B] mt-3 font-mono tracking-wide">
                            LINK EXPIRES IN 24 HOURS · CHECK SPAM IF YOU DON'T SEE IT
                        </p>
                    </div>

                    {registered.emailDeliveryError && (
                        <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30] mb-4">
                            Email delivery problem: {registered.emailDeliveryError}. Try the resend button below.
                        </div>
                    )}

                    {registered.activationLinkDevOnly && (
                        <div className="bg-[#FFD700]/10 border border-[#FFD700]/30 px-3 py-3 text-xs text-[#FFD700] mb-4 break-all font-mono"
                             data-testid="register-activation-link-dev">
                            <div className="tracking-widest mb-1">DEV-ONLY ACTIVATION LINK (no email provider configured):</div>
                            <a href={registered.activationLinkDevOnly} className="underline">
                                {registered.activationLinkDevOnly}
                            </a>
                        </div>
                    )}

                    <button
                        onClick={handleResend}
                        disabled={resendCooldown > 0 || resending}
                        data-testid="register-resend-btn"
                        className="w-full border border-[#1F1F1F] hover:border-[#FFD700] text-[#FFD700] disabled:opacity-50 disabled:hover:border-[#1F1F1F] py-2.5 text-xs font-mono tracking-widest transition-colors flex items-center justify-center gap-2"
                    >
                        {resending ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                   : <RefreshCw className="w-3.5 h-3.5" />}
                        {resendCooldown > 0
                            ? `RESEND IN ${resendCooldown}s`
                            : "RESEND ACTIVATION EMAIL"}
                    </button>

                    <div className="mt-6 text-center text-sm text-[#A1A1AA]">
                        <Link to="/login" data-testid="register-success-login-link" className="text-[#00FF41] hover:underline">
                            Already activated? Sign in →
                        </Link>
                    </div>
                </div>
            </div>
        );
    }

    // ─── Default register form ───
    return (
        <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
            <div className="w-full max-w-sm">
                <div className="flex items-center gap-3 mb-10">
                    <StoicMark size={40} />
                    <div>
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">VETO-FIRST AUTOMATED TRADING · NOTHING TRADES UNTIL READINESS PASSES</div>
                    </div>
                </div>

                <div className="font-mono text-[11px] text-[#00FF41] tracking-widest mb-3">// CREATE ACCOUNT</div>
                <h2 className="font-display font-bold text-3xl tracking-tight mb-2">Create your workspace</h2>
                <p className="text-sm text-[#A1A1AA] mb-8">Closed testing period — demo accounts only, no live capital.</p>

                <form onSubmit={handleSubmit} className="space-y-4" data-testid="register-form">
                    <div>
                        <label htmlFor="register-name" className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">NAME (OPTIONAL)</label>
                        <input
                            type="text" value={name} onChange={e => setName(e.target.value)}
                            id="register-name" name="name" autoComplete="name"
                            data-testid="register-name-input"
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-3 text-sm transition-colors duration-150"
                            placeholder="Trader Joe"
                        />
                    </div>
                    <div>
                        <label htmlFor="register-email" className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">EMAIL</label>
                        <input
                            type="email" value={email} onChange={e => setEmail(e.target.value)} required
                            id="register-email" name="email" autoComplete="email"
                            data-testid="register-email-input"
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-3 text-sm transition-colors duration-150"
                            placeholder="you@example.com"
                        />
                    </div>
                    <div>
                        <label htmlFor="register-password" className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">PASSWORD</label>
                        <input
                            type="password" value={password} onChange={e => setPassword(e.target.value)} required minLength={8}
                            id="register-password" name="new-password" autoComplete="new-password"
                            data-testid="register-password-input"
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-3 text-sm transition-colors duration-150"
                            placeholder="At least 8 characters"
                        />
                    </div>

                    <label className="flex items-start gap-2.5 mt-3 cursor-pointer" data-testid="register-terms-checkbox-wrapper">
                        <input
                            type="checkbox"
                            checked={termsAgreed}
                            onChange={e => setTermsAgreed(e.target.checked)}
                            data-testid="register-terms-checkbox"
                            className="mt-0.5 w-4 h-4 accent-[#00FF41] bg-[#0A0A0A] border-[#1F1F1F] cursor-pointer flex-shrink-0"
                            required
                        />
                        <span className="text-xs text-[#A1A1AA] leading-relaxed">
                            I have read and agree to STOIC's{" "}
                            <Link to="/terms" target="_blank" data-testid="register-terms-link"
                                className="text-[#FFD700] hover:underline">
                                Terms of Use
                            </Link>
                            . I understand trading carries risk of loss and STOIC is software, not financial advice.
                        </span>
                    </label>

                    {error && (
                        <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30]" data-testid="register-error">
                            {error}
                        </div>
                    )}

                    {turnstile.enabled && (
                        <><TurnstileWidget siteKey={turnstile.siteKey} action="register" onToken={turnstile.setToken} onState={turnstile.onState} resetRef={turnstile.resetRef} /><TurnstileStatus t={turnstile} /></>
                    )}
                    {!turnstile.enabled && <TurnstileStatus t={turnstile} />}

                    <button
                        type="submit" disabled={loading || !termsAgreed || !turnstile.canSubmit} data-testid="register-submit-button"
                        className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-40 disabled:cursor-not-allowed text-black font-medium py-3 text-sm transition-colors duration-150"
                    >
                        {loading ? "CREATING..." : "CREATE ACCOUNT →"}
                    </button>
                </form>

                <div className="mt-6 text-sm text-[#A1A1AA]">
                    Already have access?{" "}
                    <Link to="/login" data-testid="register-login-link" className="text-[#00FF41] hover:underline">Sign in</Link>
                </div>

                <div className="mt-3 text-sm">
                    <Link to="/affiliates" data-testid="register-affiliate-link"
                        className="font-mono text-[10px] text-[#FFD700] tracking-widest hover:underline">
                        EARN 20% RECURRING · AFFILIATE PROGRAM →
                    </Link>
                </div>
            </div>
        </div>
    );
}
