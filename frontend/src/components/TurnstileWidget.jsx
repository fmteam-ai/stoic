import { useEffect, useRef, useState } from "react";
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

export const TurnstileWidget = ({ siteKey, onToken, onError }) => {
    const containerRef = useRef(null);
    const widgetIdRef = useRef(null);

    useEffect(() => {
        let cancelled = false;
        loadTurnstileScript().then(() => {
            if (cancelled || !containerRef.current || !window.turnstile) return;
            widgetIdRef.current = window.turnstile.render(containerRef.current, {
                sitekey: siteKey,
                theme: "dark",
                callback: (token) => onToken?.(token),
                "expired-callback": () => onToken?.(""),
                "error-callback": () => { onToken?.(""); onError?.(); },
            });
        }).catch(() => onError?.());
        return () => {
            cancelled = true;
            if (widgetIdRef.current && window.turnstile) {
                try { window.turnstile.remove(widgetIdRef.current); } catch { /* noop */ }
            }
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [siteKey]);

    return <div ref={containerRef} data-testid="turnstile-widget" className="flex justify-center min-h-[65px]" />;
};

export const resetTurnstile = () => {
    if (window.turnstile) {
        try { window.turnstile.reset(); } catch { /* noop */ }
    }
};

// Fetches /auth/turnstile-config once; returns { enabled, siteKey, token, setToken }.
export const useTurnstile = () => {
    const [cfg, setCfg] = useState({ enabled: false, site_key: null });
    const [token, setToken] = useState("");
    useEffect(() => {
        api.get("/auth/turnstile-config")
            .then(r => setCfg(r.data || {}))
            .catch(() => setCfg({ enabled: false, site_key: null }));
    }, []);
    return { enabled: !!(cfg.enabled && cfg.site_key), siteKey: cfg.site_key, token, setToken };
};
