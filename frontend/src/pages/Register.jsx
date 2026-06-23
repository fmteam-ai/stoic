import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { formatApiError } from "@/lib/api";
import { StoicMark } from "@/components/StoicLogo";

export default function Register() {
    const navigate = useNavigate();
    const { register } = useAuth();
    const [email, setEmail] = useState("");
    const [name, setName] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState("");
    const [loading, setLoading] = useState(false);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError("");
        if (password.length < 6) { setError("Password must be at least 6 characters."); return; }
        setLoading(true);
        try {
            await register(email, password, name || undefined);
            navigate("/");
        } catch (err) {
            setError(formatApiError(err));
        } finally { setLoading(false); }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-[#050505] p-6">
            <div className="w-full max-w-sm">
                <div className="flex items-center gap-3 mb-10">
                    <StoicMark size={40} />
                    <div>
                        <div className="font-display font-bold tracking-[0.18em]">STOIC</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SMART TRADING · STEADY WEALTH</div>
                    </div>
                </div>

                <div className="font-mono text-[11px] text-[#00FF41] tracking-widest mb-3">// CREATE ACCOUNT</div>
                <h2 className="font-display font-bold text-3xl tracking-tight mb-2">Join the discipline</h2>
                <p className="text-sm text-[#A1A1AA] mb-8">Spin up your AI trading workspace.</p>

                <form onSubmit={handleSubmit} className="space-y-4" data-testid="register-form">
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">NAME (OPTIONAL)</label>
                        <input
                            type="text" value={name} onChange={e => setName(e.target.value)}
                            data-testid="register-name-input"
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-3 text-sm transition-colors duration-150"
                            placeholder="Trader Joe"
                        />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">EMAIL</label>
                        <input
                            type="email" value={email} onChange={e => setEmail(e.target.value)} required
                            data-testid="register-email-input"
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-3 text-sm transition-colors duration-150"
                            placeholder="you@example.com"
                        />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">PASSWORD</label>
                        <input
                            type="password" value={password} onChange={e => setPassword(e.target.value)} required minLength={6}
                            data-testid="register-password-input"
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-3 text-sm transition-colors duration-150"
                            placeholder="At least 6 characters"
                        />
                    </div>

                    {error && (
                        <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 text-xs text-[#FF3B30]" data-testid="register-error">
                            {error}
                        </div>
                    )}

                    <button
                        type="submit" disabled={loading} data-testid="register-submit-button"
                        className="w-full bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium py-3 text-sm transition-colors duration-150"
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
