import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { useAuth } from "@/context/AuthContext";
import {
    User, Mail, Save as FloppyDisk, KeyRound, Shield, ShieldCheck, ShieldOff,
    QrCode, Copy, CheckCircle2, AlertTriangle,
} from "lucide-react";

export default function Settings() {
    const { user, refresh } = useAuth();
    const [name, setName] = useState("");
    const [savingProfile, setSavingProfile] = useState(false);
    const [profileMsg, setProfileMsg] = useState("");
    const [profileErr, setProfileErr] = useState("");

    const [pwCurrent, setPwCurrent] = useState("");
    const [pwNew, setPwNew] = useState("");
    const [pwConfirm, setPwConfirm] = useState("");
    const [pwSaving, setPwSaving] = useState(false);
    const [pwMsg, setPwMsg] = useState("");
    const [pwErr, setPwErr] = useState("");

    const [twoFa, setTwoFa] = useState({ enabled: false, recovery_codes_remaining: 0 });
    const [enrollment, setEnrollment] = useState(null); // { qr_png_data_url, secret, otpauth_uri }
    const [enrollCode, setEnrollCode] = useState("");
    const [recoveryCodes, setRecoveryCodes] = useState(null);
    const [twoFaErr, setTwoFaErr] = useState("");
    const [twoFaMsg, setTwoFaMsg] = useState("");
    const [disableForm, setDisableForm] = useState({ password: "", code: "" });
    const [showDisable, setShowDisable] = useState(false);
    const [copied, setCopied] = useState(false);

    const loadStatus = useCallback(async () => {
        try {
            const { data } = await api.get("/auth/2fa/status");
            setTwoFa(data);
        } catch (e) { setTwoFaErr(formatApiError(e)); }
    }, []);

    useEffect(() => {
        if (user) {
            setName(user.name || "");
            loadStatus();
        }
    }, [user, loadStatus]);

    const saveProfile = async (e) => {
        e.preventDefault();
        setSavingProfile(true); setProfileMsg(""); setProfileErr("");
        try {
            await api.put("/auth/profile", { name });
            setProfileMsg("Profile updated.");
            await refresh();
        } catch (e2) { setProfileErr(formatApiError(e2)); }
        finally { setSavingProfile(false); }
    };

    const changePassword = async (e) => {
        e.preventDefault();
        setPwSaving(true); setPwMsg(""); setPwErr("");
        if (pwNew.length < 6) { setPwErr("New password must be at least 6 characters."); setPwSaving(false); return; }
        if (pwNew !== pwConfirm) { setPwErr("New password and confirmation don't match."); setPwSaving(false); return; }
        try {
            await api.post("/auth/change-password", { current_password: pwCurrent, new_password: pwNew });
            setPwMsg("Password changed. You'll stay signed in on this device.");
            setPwCurrent(""); setPwNew(""); setPwConfirm("");
        } catch (e2) { setPwErr(formatApiError(e2)); }
        finally { setPwSaving(false); }
    };

    const startEnroll = async () => {
        setTwoFaErr(""); setTwoFaMsg(""); setRecoveryCodes(null);
        try {
            const { data } = await api.post("/auth/2fa/enroll");
            setEnrollment(data);
        } catch (e) { setTwoFaErr(formatApiError(e)); }
    };

    const verifyEnroll = async (e) => {
        e.preventDefault();
        setTwoFaErr(""); setTwoFaMsg("");
        try {
            const { data } = await api.post("/auth/2fa/verify-enroll", { code: enrollCode });
            setRecoveryCodes(data.recovery_codes || []);
            setEnrollment(null);
            setEnrollCode("");
            setTwoFaMsg("2FA enabled. Save your recovery codes — they are shown only once.");
            await loadStatus();
            await refresh();
        } catch (e2) { setTwoFaErr(formatApiError(e2)); }
    };

    const disable2Fa = async (e) => {
        e.preventDefault();
        setTwoFaErr(""); setTwoFaMsg("");
        try {
            await api.post("/auth/2fa/disable", {
                current_password: disableForm.password,
                code: disableForm.code,
            });
            setTwoFaMsg("2FA disabled.");
            setShowDisable(false);
            setDisableForm({ password: "", code: "" });
            setRecoveryCodes(null);
            await loadStatus();
            await refresh();
        } catch (e2) { setTwoFaErr(formatApiError(e2)); }
    };

    const copySecret = async (text) => {
        try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); }
        catch (e) { console.warn("Clipboard copy failed:", e); }
    };

    if (!user) return (
        <AppLayout><div className="p-8 font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div></AppLayout>
    );

    return (
        <AppLayout>
            <PageHeader
                title="Settings"
                subtitle="Profile, password and two-factor authentication."
                testid="settings-header"
            />

            <div className="p-4 md:p-8 space-y-6 max-w-3xl">
                {/* PROFILE */}
                <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="profile-section">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <User className="w-4 h-4 text-[#00FF41]" />
                        <div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 01 · PROFILE</div>
                            <div className="font-display font-bold text-lg tracking-tight">Identity</div>
                        </div>
                    </div>
                    <form onSubmit={saveProfile} className="p-5 space-y-4">
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">EMAIL</label>
                            <div className="flex items-center bg-[#050505] border border-[#1F1F1F]">
                                <Mail className="w-4 h-4 text-[#52525B] mx-3" />
                                <input value={user.email} disabled
                                    data-testid="profile-email-input"
                                    className="flex-1 bg-transparent px-2 py-2 text-sm font-mono text-[#A1A1AA] outline-none" />
                                <span className="font-mono text-[10px] text-[#52525B] tracking-widest px-3">READ-ONLY</span>
                            </div>
                        </div>
                        <div>
                            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">DISPLAY NAME</label>
                            <input value={name} onChange={e => setName(e.target.value)} required maxLength={120}
                                data-testid="profile-name-input"
                                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none transition-colors" />
                        </div>

                        {profileErr && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="profile-error">{profileErr}</div>}
                        {profileMsg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-3 py-2 text-xs text-[#00FF41] font-mono" data-testid="profile-success">{profileMsg}</div>}

                        <div className="flex justify-end">
                            <button type="submit" disabled={savingProfile}
                                data-testid="save-profile-button"
                                className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium px-5 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                                <FloppyDisk className="w-3.5 h-3.5" /> {savingProfile ? "SAVING…" : "SAVE PROFILE"}
                            </button>
                        </div>
                    </form>
                </section>

                {/* PASSWORD */}
                <section className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="password-section">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <KeyRound className="w-4 h-4 text-[#FFD700]" />
                        <div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 02 · PASSWORD</div>
                            <div className="font-display font-bold text-lg tracking-tight">Change password</div>
                        </div>
                    </div>
                    <form onSubmit={changePassword} className="p-5 space-y-4">
                        <PwInput label="CURRENT PASSWORD" value={pwCurrent} onChange={setPwCurrent} testid="pw-current" />
                        <PwInput label="NEW PASSWORD" value={pwNew} onChange={setPwNew} testid="pw-new" hint="Minimum 6 characters." />
                        <PwInput label="CONFIRM NEW PASSWORD" value={pwConfirm} onChange={setPwConfirm} testid="pw-confirm" />

                        {pwErr && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="pw-error">{pwErr}</div>}
                        {pwMsg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-3 py-2 text-xs text-[#00FF41] font-mono" data-testid="pw-success">{pwMsg}</div>}

                        <div className="flex justify-end">
                            <button type="submit" disabled={pwSaving || !pwCurrent || !pwNew || !pwConfirm}
                                data-testid="change-password-button"
                                className="bg-[#FFD700] hover:bg-[#FFC700] disabled:opacity-50 text-black font-medium px-5 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                                <KeyRound className="w-3.5 h-3.5" /> {pwSaving ? "UPDATING…" : "CHANGE PASSWORD"}
                            </button>
                        </div>
                    </form>
                </section>

                {/* 2FA */}
                <section className={`border ${twoFa.enabled ? "border-[#00FF41]/40" : "border-[#1F1F1F]"} bg-[#0A0A0A]`} data-testid="twofa-section">
                    <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        {twoFa.enabled ? <ShieldCheck className="w-4 h-4 text-[#00FF41]" /> : <Shield className="w-4 h-4 text-[#A1A1AA]" />}
                        <div className="flex-1">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 03 · TWO-FACTOR AUTHENTICATION</div>
                            <div className="font-display font-bold text-lg tracking-tight">Authenticator app (TOTP)</div>
                        </div>
                        <div className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${twoFa.enabled ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#52525B]"}`}
                            data-testid="twofa-status-pill">
                            {twoFa.enabled ? "● ENABLED" : "○ DISABLED"}
                        </div>
                    </div>

                    <div className="p-5 space-y-4">
                        {twoFaErr && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="twofa-error">{twoFaErr}</div>}
                        {twoFaMsg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-3 py-2 text-xs text-[#00FF41] font-mono" data-testid="twofa-success">{twoFaMsg}</div>}

                        {/* DISABLED state — invite to enroll */}
                        {!twoFa.enabled && !enrollment && !recoveryCodes && (
                            <>
                                <p className="text-xs text-[#A1A1AA] leading-relaxed">
                                    Add an extra layer of security by requiring a 6-digit code from your
                                    authenticator app (Google Authenticator, Authy, 1Password) every time you sign in.
                                </p>
                                <button onClick={startEnroll}
                                    data-testid="enable-2fa-button"
                                    className="bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium px-5 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                                    <QrCode className="w-3.5 h-3.5" /> ENABLE 2FA
                                </button>
                            </>
                        )}

                        {/* ENROLLMENT — show QR + ask for first code */}
                        {enrollment && (
                            <div className="space-y-4" data-testid="twofa-enrollment">
                                <p className="text-xs text-[#A1A1AA] leading-relaxed">
                                    1. Scan this QR with your authenticator app, or copy the secret manually.<br/>
                                    2. Enter the 6-digit code your app shows to finish enabling 2FA.
                                </p>
                                <div className="flex flex-col md:flex-row gap-4 items-start">
                                    <img src={enrollment.qr_png_data_url} alt="2FA QR Code"
                                        data-testid="twofa-qr"
                                        className="w-44 h-44 bg-white p-2 border border-[#1F1F1F]" />
                                    <div className="flex-1 space-y-2">
                                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECRET (manual entry)</div>
                                        <div className="flex items-center bg-[#050505] border border-[#1F1F1F]">
                                            <code className="flex-1 px-3 py-2 text-xs font-mono break-all" data-testid="twofa-secret">{enrollment.secret}</code>
                                            <button type="button" onClick={() => copySecret(enrollment.secret)}
                                                data-testid="twofa-copy-secret"
                                                className="px-3 py-2 hover:bg-[#121212] text-[#A1A1AA]">
                                                {copied ? <CheckCircle2 className="w-4 h-4 text-[#00FF41]" /> : <Copy className="w-4 h-4" />}
                                            </button>
                                        </div>
                                        <form onSubmit={verifyEnroll} className="pt-2 space-y-3">
                                            <div>
                                                <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">ENTER CODE FROM APP</label>
                                                <input value={enrollCode} onChange={e => setEnrollCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                                                    inputMode="numeric" autoComplete="one-time-code" required
                                                    data-testid="twofa-verify-input"
                                                    placeholder="123456"
                                                    className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-lg font-mono tracking-[0.4em] outline-none" />
                                            </div>
                                            <button type="submit" disabled={enrollCode.length !== 6}
                                                data-testid="twofa-verify-button"
                                                className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium px-5 py-2 text-xs tracking-widest">
                                                VERIFY &amp; ENABLE
                                            </button>
                                        </form>
                                    </div>
                                </div>
                            </div>
                        )}

                        {/* RECOVERY CODES — show once */}
                        {recoveryCodes && (
                            <div className="border border-[#FFD700]/40 bg-[#FFD700]/5 p-4" data-testid="twofa-recovery-codes">
                                <div className="flex items-center gap-2 mb-3">
                                    <AlertTriangle className="w-4 h-4 text-[#FFD700]" />
                                    <span className="font-display font-bold text-sm">Save these recovery codes</span>
                                </div>
                                <p className="text-xs text-[#A1A1AA] leading-relaxed mb-3">
                                    Use any one of these codes (each works once) if you ever lose access
                                    to your authenticator app. <span className="text-[#FFD700]">These are shown only once.</span>
                                </p>
                                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-3">
                                    {recoveryCodes.map(c => (
                                        <code key={c} className="bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs font-mono text-center">{c}</code>
                                    ))}
                                </div>
                                <button type="button"
                                    onClick={() => copySecret(recoveryCodes.join("\n"))}
                                    data-testid="twofa-copy-recovery"
                                    className="bg-[#121212] hover:bg-[#1F1F1F] border border-[#1F1F1F] text-[#A1A1AA] hover:text-white px-3 py-1.5 text-xs font-mono tracking-widest flex items-center gap-2">
                                    {copied ? <CheckCircle2 className="w-3.5 h-3.5 text-[#00FF41]" /> : <Copy className="w-3.5 h-3.5" />}
                                    {copied ? "COPIED" : "COPY ALL"}
                                </button>
                            </div>
                        )}

                        {/* ENABLED state — invite to disable */}
                        {twoFa.enabled && !showDisable && (
                            <div className="space-y-3">
                                <p className="text-xs text-[#A1A1AA] leading-relaxed">
                                    2FA is active on your account. {twoFa.recovery_codes_remaining} recovery code{twoFa.recovery_codes_remaining === 1 ? "" : "s"} remaining.
                                </p>
                                <button onClick={() => setShowDisable(true)}
                                    data-testid="disable-2fa-button"
                                    className="bg-[#0A0A0A] hover:bg-[#1F1F1F] border border-[#FF3B30]/40 text-[#FF3B30] px-5 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                                    <ShieldOff className="w-3.5 h-3.5" /> DISABLE 2FA
                                </button>
                            </div>
                        )}

                        {/* DISABLE FORM */}
                        {twoFa.enabled && showDisable && (
                            <form onSubmit={disable2Fa} className="space-y-3" data-testid="disable-2fa-form">
                                <p className="text-xs text-[#FF3B30] leading-relaxed">
                                    Confirm your password and a current 2FA code (or any unused recovery code) to disable 2FA.
                                </p>
                                <PwInput label="PASSWORD" value={disableForm.password}
                                    onChange={v => setDisableForm({ ...disableForm, password: v })}
                                    testid="disable-2fa-password" />
                                <div>
                                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">2FA CODE OR RECOVERY CODE</label>
                                    <input value={disableForm.code}
                                        onChange={e => setDisableForm({ ...disableForm, code: e.target.value.replace(/\s/g, "") })}
                                        autoComplete="one-time-code" required
                                        data-testid="disable-2fa-code"
                                        placeholder="123456 or ABCDE12345"
                                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FF3B30] px-3 py-2 text-sm font-mono outline-none" />
                                </div>
                                <div className="flex gap-2">
                                    <button type="submit"
                                        data-testid="confirm-disable-2fa-button"
                                        className="bg-[#FF3B30] hover:bg-[#E53527] text-white font-medium px-5 py-2 text-xs tracking-widest">
                                        CONFIRM DISABLE
                                    </button>
                                    <button type="button" onClick={() => { setShowDisable(false); setDisableForm({ password: "", code: "" }); }}
                                        data-testid="cancel-disable-2fa-button"
                                        className="bg-[#0A0A0A] hover:bg-[#121212] border border-[#1F1F1F] text-[#A1A1AA] px-5 py-2 text-xs tracking-widest">
                                        CANCEL
                                    </button>
                                </div>
                            </form>
                        )}
                    </div>
                </section>
            </div>
        </AppLayout>
    );
}

function PwInput({ label, value, onChange, testid, hint }) {
    return (
        <div>
            <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-2">{label}</label>
            <input type="password" value={value} onChange={e => onChange(e.target.value)} required
                data-testid={testid}
                className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none transition-colors" />
            {hint && <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-1">{hint}</div>}
        </div>
    );
}
