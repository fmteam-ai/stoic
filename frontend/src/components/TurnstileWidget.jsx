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
    "script-error": "The verification challenge could not load (blocked script or network). Your details are kept — retry.",
    expired: "The verification expired. Complete the challenge again — your details are kept.",
};

/**
 * Explicit configuration states — never a silent {enabled:false} on error:
 *   loading · enabled · disabled-by-policy · configuration-error · script-error · expired · ready
 * `canSubmit` is false while the state is unknown or a required challenge is unavailable.
 */
export const useTurnstile = (action = "login") => {
    const [cfgState, setCfgState] = useState("loading");
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
                if (d.enabled && d.site_key) { setSiteKey(d.site_key); setCfgState("enabled"); }
                else { setSiteKey(null); setCfgState("disabled-by-policy"); }
            })
            .catch(() => { if (alive) setCfgState("configuration-error"); });
        return () => { alive = false; };
    }, [attempt]);

    const state = cfgState === "enabled" ? (widgetState || "loading") : cfgState;
    const enabled = cfgState === "enabled";
    const canSubmit = cfgState === "disabled-by-policy" || (enabled && state === "ready" && !!token);
    const reset = useCallback(() => { if (resetRef.current) resetRef.current(); else { resetTurnstile(); setToken(""); } }, []);
    const retryConfig = useCallback(() => setAttempt((a) => a + 1), []);

    return { enabled, siteKey, action, token, setToken, state, canSubmit, reset, retryConfig, resetRef,
             onState: setWidgetState, recoveryMessage: RECOVERY[state] || null };
};

/** Visible state + recovery. Rendered whether or not the widget itself is mounted. */
export const TurnstileStatus = ({ t }) => {
    if (!t || t.state === "ready" || t.state === "disabled-by-policy" || t.state === "enabled") return null;
    const problem = t.state === "configuration-error" || t.state === "script-error" || t.state === "expired";
    return (
        <div data-testid="turnstile-status" data-state={t.state}
            className={`text-xs font-mono px-3 py-2 border ${problem ? "border-[#FFB000]/50 text-[#FFB000]" : "border-[#2A2A2A] text-[#A1A1AA]"}`}>
            {t.state === "loading" && "Preparing human verification…"}
            {problem && (t.recoveryMessage || "Human verification is unavailable right now.")}
            {t.state === "configuration-error" && (
                <button type="button" onClick={t.retryConfig} data-testid="turnstile-retry-config"
                    className="ml-2 underline hover:text-white">retry</button>
            )}
            {(t.state === "script-error" || t.state === "expired") && (
                <button type="button" onClick={t.reset} data-testid="turnstile-retry-widget"
                    className="ml-2 underline hover:text-white">retry challenge</button>
            )}
        </div>
    );
};
