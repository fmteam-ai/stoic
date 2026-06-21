import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { formatApiError } from "@/lib/api";
import { Zap as Lightning, Mail as EnvelopeSimple, Lock as LockKey } from "lucide-react";

export default function Login() {
    const navigate = useNavigate();
    const { login } = useAuth();
    const [email, setEmail] = useState("admin@trading.bot");
    const [password, setPassword] = useState("admin123");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError("");
        setLoading(true);
        try {
            await login(email, password);
            navigate("/");
        } catch (err) {
            setError(formatApiError(err));
        } finally { setLoading(false); }
    };

    return (
        <div className="min-h-screen flex bg-[#050505]">
            {/* Left panel - hidden on mobile */}
            <div className="hidden lg:flex lg:w-1/2 relative border-r border-[#1F1F1F] grid-bg">
                <div className="absolute inset-0 bg-gradient-to-br from-transparent via-transparent to-[#050505]" />
                <div className="relative z-10 flex flex-col justify-between p-12 w-full">
                    <div className="flex items-center gap-2">
                        <div className="w-8 h-8 bg-[#00FF41] flex items-center justify-center">
                            <Lightning className="w-5 h-5 text-black" />
                        </div>
                        <div>
                            <div className="font-display font-bold tracking-tight">EMERGENT</div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">AI TRADING TERMINAL</div>
                        </div>
                    </div>

                    <div className="space-y-6 max-w-md">
                        <div className="font-mono text-[11px] text-[#00FF41] tracking-widest">// SYSTEM READY</div>
                        <h1 className="font-display font-bold text-5xl tracking-tighter leading-[1.05]">
                            Trade gold & crypto<br/>with an <span className="text-[#00FF41]">AI co-pilot</span>.
                        </h1>
                        <p className="text-[#A1A1AA] text-sm leading-relaxed max-w-sm">
                            Claude-powered market analysis, four risk profiles, live MT5 execution. Built for traders who don't sleep on opportunity.
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
                    <div className="lg:hidden flex items-center gap-2 mb-10">
                        <div className="w-7 h-7 bg-[#00FF41] flex items-center justify-center">
                            <Lightning className="w-4 h-4 text-black" />
                        </div>
                        <div className="font-display font-bold tracking-tight">EMERGENT</div>
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

                        {error && (
                            <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30]" data-testid="login-error">
                                {error}
                            </div>
                        )}

                        <button
                            type="submit"
                            disabled={loading}
                            data-testid="login-submit-button"
                            className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium py-3 text-sm transition-colors duration-150"
                        >
                            {loading ? "AUTHENTICATING..." : "SIGN IN →"}
                        </button>
                    </form>

                    <div className="mt-6 text-sm text-[#A1A1AA]">
                        New to the terminal?{" "}
                        <Link to="/register" data-testid="login-register-link" className="text-[#00FF41] hover:underline">Create account</Link>
                    </div>

                    <div className="mt-10 pt-6 border-t border-[#1F1F1F]">
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">DEMO CREDENTIALS</div>
                        <div className="font-mono text-xs text-[#A1A1AA]">
                            admin@trading.bot / admin123
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
