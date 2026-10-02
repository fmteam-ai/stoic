import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { registerStepUpHandler } from "@/lib/stepUp";
import { useAuth } from "@/context/AuthContext";
import { getPasskeyAssertion, webauthnSupported } from "@/lib/webauthnClient";
import { Fingerprint, ShieldCheck, KeyRound } from "lucide-react";

const ACTION_LABELS = {
    live_activation: "Activate live trading",
    risk_raise: "Raise risk limits",
    panic_release: "Release panic lock",
    api_key_create: "Create an API key",
    release_promote: "Promote release to stable fleet",
    release_rollback: "Roll back the fleet release",
    agent_config_push: "Push config to a host agent",
    canary_set: "Set canary rollout agents",
    release_trust: "Designate a release-trusted agent",
    passkey_enroll: "Add a passkey",
    bridge_token_rotate: "Rotate an EA bridge token",
};

// Actions the server only accepts from an authenticator (TOTP) code when the
// user has TOTP enrolled — a passkey cannot authorise them.
const TOTP_ONLY_ACTIONS = new Set(["passkey_enroll"]);

export function StepUpDialog() {
    const [pending, setPending] = useState(null);
    const [code, setCode] = useState("");
    const [err, setErr] = useState("");
    const [busy, setBusy] = useState(false);
    const [hasPasskey, setHasPasskey] = useState(false);
    const navigate = useNavigate();
    const { user } = useAuth() || {};

    useEffect(() => {
        registerStepUpHandler((detail) => new Promise((resolve, reject) => {
            setCode("");
            setErr("");
            setPending({ detail, resolve, reject });
        }));
        return () => registerStepUpHandler(null);
    }, []);

    useEffect(() => {
        if (!pending) return;
        api.get("/auth/webauthn/credentials")
            .then((r) => setHasPasskey((r.data.credentials || []).length > 0))
            .catch(() => setHasPasskey(false));
    }, [pending]);

    if (!pending) return null;
    const { detail } = pending;
    const needsEnroll = detail?.code === "mfa_enrollment_required";
    const label = ACTION_LABELS[detail?.action] || "This sensitive action";
    const passkeyAllowed = !(TOTP_ONLY_ACTIONS.has(detail?.action) && user?.two_factor_enabled);

    const cancel = () => {
        pending.reject(new Error("step_up_cancelled"));
        setPending(null);
    };

    const submit = async (e) => {
        e.preventDefault();
        if (busy) return;
        setBusy(true);
        setErr("");
        try {
            const { data } = await api.post("/auth/step-up",
                { code: code.trim(), action: detail.action });
            pending.resolve(data.step_up_token);
            setPending(null);
        } catch (e2) {
            setErr(formatApiError(e2));
        } finally {
            setBusy(false);
        }
    };

    const usePasskey = async () => {
        if (busy) return;
        setBusy(true);
        setErr("");
        try {
            const { data } = await api.post("/auth/webauthn/step-up/begin",
                { action: detail.action, origin: window.location.origin });
            const credential = await getPasskeyAssertion(data.options);
            const { data: out } = await api.post("/auth/webauthn/step-up/complete",
                { challenge_id: data.challenge_id, action: detail.action, credential });
            pending.resolve(out.step_up_token);
            setPending(null);
        } catch (e2) {
            setErr(e2?.name === "NotAllowedError"
                ? "Passkey prompt was cancelled or timed out."
                : formatApiError(e2));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4"
            data-testid="step-up-modal">
            <div className="w-full max-w-sm border border-[#1F1F1F] bg-[#0A0A0A] p-6">
                <div className="flex items-center gap-2 text-[#00FF41]">
                    <ShieldCheck className="w-5 h-5" />
                    <span className="font-display font-bold text-sm tracking-widest">
                        SECURITY CHECK
                    </span>
                </div>
                <p className="text-sm text-[#A1A1AA] mt-3" data-testid="step-up-action-label">
                    <span className="text-white font-semibold">{label}</span>{" "}
                    is a live-sensitive operation.
                </p>

                {needsEnroll ? (
                    <div className="mt-4 space-y-4">
                        <p className="text-sm text-[#FFB000]" data-testid="step-up-enroll-message">
                            Two-factor authentication is required for this action
                            but is not enabled on your account yet.
                        </p>
                        <div className="flex gap-2">
                            <button type="button" data-testid="step-up-enroll-link"
                                onClick={() => { cancel(); navigate("/settings"); }}
                                className="flex-1 py-2 bg-[#00FF41] text-black font-display font-bold text-xs tracking-widest hover:bg-[#00E53A]">
                                ENABLE 2FA IN SETTINGS
                            </button>
                            <button type="button" data-testid="step-up-cancel" onClick={cancel}
                                className="px-4 py-2 border border-[#1F1F1F] text-[#A1A1AA] text-xs tracking-widest hover:text-white">
                                CANCEL
                            </button>
                        </div>
                    </div>
                ) : (
                    <form onSubmit={submit} className="mt-4 space-y-4">
                        <label className="block text-[10px] font-mono tracking-widest text-[#A1A1AA]">
                            ENTER A FRESH 6-DIGIT AUTHENTICATOR CODE
                        </label>
                        <div className="flex items-center gap-2 border border-[#1F1F1F] bg-black px-3">
                            <KeyRound className="w-4 h-4 text-[#A1A1AA]" />
                            <input autoFocus value={code} inputMode="numeric"
                                onChange={(e) => setCode(e.target.value)}
                                placeholder="123456" maxLength={16}
                                data-testid="step-up-code-input"
                                className="w-full bg-transparent py-2.5 font-mono text-lg tracking-[0.4em] text-white outline-none placeholder:text-[#333]" />
                        </div>
                        {err && (
                            <p className="text-xs text-[#FF3B30]" data-testid="step-up-error">{err}</p>
                        )}
                        <div className="flex gap-2">
                            <button type="submit" disabled={busy || code.trim().length < 6}
                                data-testid="step-up-submit"
                                className="flex-1 py-2 bg-[#00FF41] text-black font-display font-bold text-xs tracking-widest hover:bg-[#00E53A] disabled:opacity-40 disabled:cursor-not-allowed">
                                {busy ? "VERIFYING…" : "VERIFY & CONTINUE"}
                            </button>
                            <button type="button" data-testid="step-up-cancel" onClick={cancel}
                                className="px-4 py-2 border border-[#1F1F1F] text-[#A1A1AA] text-xs tracking-widest hover:text-white">
                                CANCEL
                            </button>
                        </div>
                        <p className="text-[10px] text-[#555] font-mono">
                            Token is single-use and expires in 5 minutes.
                        </p>
                        {hasPasskey && passkeyAllowed && webauthnSupported() && (
                            <button type="button" onClick={usePasskey} disabled={busy}
                                data-testid="step-up-passkey-button"
                                className="w-full py-2 border border-[#00FF41]/40 text-[#00FF41] font-display font-bold text-xs tracking-widest hover:bg-[#00FF41]/10 disabled:opacity-40 flex items-center justify-center gap-2 transition-colors">
                                <Fingerprint className="w-4 h-4" /> USE PASSKEY INSTEAD
                            </button>
                        )}
                    </form>
                )}
            </div>
        </div>
    );
}
