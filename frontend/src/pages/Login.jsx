import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { formatApiError } from "@/lib/api";
import { Mail as EnvelopeSimple, Lock as LockKey, ShieldCheck, MailWarning, RefreshCw } from "lucide-react";
import { StoicMark } from "@/components/StoicLogo";

export default function Login() {
    const navigate = useNavigate();
    const { login, resendActivation } = useAuth();
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [totpCode, setTotpCode] = useState("");
    const [needs2fa, setNeeds2fa] = useState(false);
    const [error, setError] = useState("");
    const [unverifiedEmail, setUnverifiedEmail] = useState("");
    const [resendCooldown, setResendCooldown] = useState(0);
    const [loading, setLoading] = useState(false);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError("");
        setUnverifiedEmail("");
        setLoading(true);
        try {
            const u = await login(email, password, needs2fa ? totpCode : undefined);
            navigate(u?.must_change_password ? "/settings" : "/");
        } catch (err) {
            const detail = err?.response?.data?.detail;
            // Unverified account: surface friendly UI with resend link
            if (detail?.code === "account_unverified") {
                setUnverifiedEmail(detail.email || email);
                setError("");
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
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SMART TRADING · STEADY WEALTH</div>
                        </div>
                    </div>

                    <div className="space-y-6 max-w-md">
                        <div className="font-mono text-[11px] text-[#00FF41] tracking-widest">// SYSTEM READY</div>
                        <h1 className="font-display font-bold text-5xl tracking-tighter leading-[1.05]">
                            Trade gold &amp; crypto<br/>like a <span className="text-[#00FF41]">Stoic</span>.
                        </h1>
                        <p className="text-[#A1A1AA] text-sm leading-relaxed max-w-sm">
                            Multi-engine AI consensus. Four risk profiles. Veto-first execution. Built for traders who refuse to panic.
                        </p>
                    </div>

                    <div className="grid grid-cols-3 gap-4 max-w-md">
                        {[
                            { label: "Asset Classes", value: "FX • Crypto • Gold" },
                            { label: "AI Model", value: "Claude 4.5" },
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
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">EMAIL</label>
                            <div className="relative">
                                <EnvelopeSimple className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                                <input
                                    type="email"
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
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">PASSWORD</label>
                            <div className="relative">
                                <LockKey className="w-4 h-4 text-[#52525B] absolute left-3 top-1/2 -translate-y-1/2" />
                                <input
                                    type="password"
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

                        <button
                            type="submit"
                            disabled={loading}
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
                </div>
            </div>
        </div>
    );
}
