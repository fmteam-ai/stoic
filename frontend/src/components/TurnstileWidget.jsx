import { useCallback, useEffect, useRef, useState } from "react";
import api from "@/lib/api";

const SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
let scriptPromise = null;

function loadTurnstileScript() {
    if (window.turnstile) return Promise.resolve();
    if (scriptPromise) return scriptPromise;
    scriptPromise = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = SCRIPT_SRC;
        s.async = true;
        s.onload = resolve;
        s.onerror = () => { scriptPromise = null; reject(new Error("turnstile script failed")); };
        document.head.appendChild(s);
    });
    return scriptPromise;
}

// Audit round 8 P1-2: the widget reports EXPLICIT states and resets by widget id.
// action is FIXED per surface (login | register | password_reset) and the backend
// binds the token to it (turnstile_gate.verify_token action/hostname checks).
export const TurnstileWidget = ({ siteKey, action, onToken, onError, onState, resetRef }) => {
    const containerRef = useRef(null);
    const widgetIdRef = useRef(null);
    const setState = useCallback((s) => onState?.(s), [onState]);

    useEffect(() => {
        let cancelled = false;
        setState("loading");
        loadTurnstileScript().then(() => {
            if (cancelled || !containerRef.current || !window.turnstile) return;
            widgetIdRef.current = window.turnstile.render(containerRef.current, {
                sitekey: siteKey,
                action,
                theme: "dark",
                callback: (token) => { onToken?.(token); setState("ready"); },
                "expired-callback": () => { onToken?.(""); setState("expired"); },
                "error-callback": () => { onToken?.(""); setState("script-error"); onError?.(); },
            });
            setState("enabled");
        }).catch(() => { setState("script-error"); onError?.(); });
        if (resetRef) {
            resetRef.current = () => {
                if (widgetIdRef.current && window.turnstile) {
                    try { window.turnstile.reset(widgetIdRef.current); } catch { /* noop */ }
                }
                onToken?.("");
                setState("enabled");
            };
        }
        return () => {
            cancelled = true;
            if (widgetIdRef.current && window.turnstile) {
                try { window.turnstile.remove(widgetIdRef.current); } catch { /* noop */ }
            }
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [siteKey, action]);

    return <div ref={containerRef} data-testid="turnstile-widget" data-action={action} className="flex justify-center min-h-[65px]" />;
};

// Legacy global reset kept for callers without a widget ref (prefers per-widget reset).
export const resetTurnstile = () => {
    if (window.turnstile) {
        try { window.turnstile.reset(); } catch { /* noop */ }
    }
};

const RECOVERY = {
    "configuration-error": "Human verification could not be configured. Your details are kept — retry in a moment.",
    "script-error": "The verification challenge could not load (blocked script, ad-blocker or network). Sign-in stays locked until it loads — allow challenges.cloudflare.com and retry, or contact support.",
    expired: "The verification expired. Complete the challenge again — your details are kept.",
    misconfigured: "Human verification is misconfigured on the server. Our operators have been alerted — please try again later.",
    "provider-degraded": "The verification provider is degraded. Sign-in may ask for an emailed one-time code instead.",
    "break-glass": "Human verification is temporarily bypassed under an audited incident procedure.",
};

/**
 * Explicit public states from GET /auth/turnstile-config (round 9 P1-04):
 *   loading · disabled · ready · misconfigured · provider-degraded · break-glass · configuration-error
 * plus widget states while ready: enabled · ready · expired · script-error.
 * `canSubmit` is false while the state is unknown, misconfigured, or a required challenge has no token.
 */
export const useTurnstile = (action = "login") => {
    const [cfgState, setCfgState] = useState("loading");
    const [cfg, setCfg] = useState({});
    const [siteKey, setSiteKey] = useState(null);
    const [widgetState, setWidgetState] = useState(null);
    const [token, setToken] = useState("");
    const resetRef = useRef(null);
    const [attempt, setAttempt] = useState(0);

    useEffect(() => {
        let alive = true;
        setCfgState("loading");
        api.get("/auth/turnstile-config")
            .then((r) => {
                if (!alive) return;
                const d = r.data || {};
                setCfg(d);
                const s = d.state || "configuration-error";
                setSiteKey(d.site_key || null);
                setCfgState(s === "provider_degraded" ? "provider-degraded" : s === "break_glass" ? "break-glass" : s);
            })
            .catch(() => { if (alive) setCfgState("configuration-error"); });
        return () => { alive = false; };
    }, [attempt]);

    const bypassed = cfgState === "break-glass" && Array.isArray(cfg.scope) && cfg.scope.includes(action);
    const enabled = !!siteKey && !bypassed && (cfgState === "ready" || cfgState === "provider-degraded" || cfgState === "break-glass");
    const state = enabled ? (widgetState || "loading") : cfgState;
    // round 11 P1-06: tokenless login is offered ONLY when the SERVER reports provider
    // degradation (its own siteverify failures). A client-side script/CDN failure never
    // unlocks submit — it fails closed with a precise retry message.
    const degradedLoginAllowed = action === "login" && cfg.degraded_login === "otp_required" && cfgState === "provider-degraded";
    const canSubmit = cfgState === "disabled" || bypassed || (enabled && state === "ready" && !!token) || degradedLoginAllowed;
    const reset = useCallback(() => { if (resetRef.current) resetRef.current(); else { resetTurnstile(); setToken(""); } }, []);
    const retryConfig = useCallback(() => setAttempt((a) => a + 1), []);

    const bannerState = cfgState === "provider-degraded" && enabled ? "provider-degraded" : bypassed ? "break-glass" : null;
    return { enabled, siteKey, action, token, setToken, state, cfgState, bannerState, canSubmit, reset, retryConfig, resetRef,
             onState: setWidgetState, recoveryMessage: RECOVERY[bannerState || state] || null };
};

/** Visible state + recovery. Rendered whether or not the widget itself is mounted. */
export const TurnstileStatus = ({ t }) => {
    if (!t) return null;
    const shown = t.bannerState || t.state;
    if (shown === "ready" || shown === "disabled" || shown === "enabled") return null;
    const problem = ["configuration-error", "script-error", "expired", "misconfigured"].includes(shown);
    const notice = shown === "provider-degraded" || shown === "break-glass";
    return (
        <div data-testid="turnstile-status" data-state={shown}
            className={`text-xs font-mono px-3 py-2 border ${problem ? "border-[#FFB000]/50 text-[#FFB000]" : notice ? "border-[#38BDF8]/40 text-[#38BDF8]" : "border-[#2A2A2A] text-[#A1A1AA]"}`}>
            {shown === "loading" && "Preparing human verification…"}
            {(problem || notice) && (t.recoveryMessage || "Human verification is unavailable right now.")}
            {(shown === "configuration-error" || shown === "misconfigured") && (
                <button type="button" onClick={t.retryConfig} data-testid="turnstile-retry-config"
                    className="ml-2 underline hover:text-white">retry</button>
            )}
            {(shown === "script-error" || shown === "expired") && (
                <button type="button" onClick={t.reset} data-testid="turnstile-retry-widget"
                    className="ml-2 underline hover:text-white">retry challenge</button>
            )}
        </div>
    );
};
