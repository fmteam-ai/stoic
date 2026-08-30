import axios from "axios";
import { requestStepUp } from "./stepUp";

// Same-origin fallback: production builds served behind nginx proxy /api on
// the same host don't need an explicit REACT_APP_BACKEND_URL.
export const BACKEND_URL = process.env.REACT_APP_BACKEND_URL
    || (typeof window !== "undefined" ? window.location.origin : "");

if (!process.env.REACT_APP_BACKEND_URL) {
    // eslint-disable-next-line no-console
    console.warn("REACT_APP_BACKEND_URL not set — using same-origin API routing:", BACKEND_URL);
}

export const API = `${BACKEND_URL}/api`;

const api = axios.create({
    baseURL: API,
    withCredentials: true,
    headers: { "Content-Type": "application/json" },
});

// ---------------------------------------------------------------- CSRF
function readCookie(name) {
    const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
    return m ? decodeURIComponent(m[1]) : null;
}

function attachCsrf(config) {
    const method = (config.method || "get").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
        const token = readCookie("csrf_token");
        if (token) config.headers["X-CSRF-Token"] = token;
    }
    config.withCredentials = true;
    return config;
}

// Register on BOTH the shared instance and the global axios module so
// components still importing axios directly stay CSRF-compliant.
api.interceptors.request.use(attachCsrf);
axios.interceptors.request.use(attachCsrf);

// ------------------------------------------- 401 silent refresh (single-flight)
let refreshPromise = null;

function silentRefresh() {
    if (!refreshPromise) {
        refreshPromise = axios
            .post(`${API}/auth/refresh`, {}, { withCredentials: true })
            .finally(() => { refreshPromise = null; });
    }
    return refreshPromise;
}

function makeResponseInterceptor(client) {
    return async (error) => {
        const cfg = error.config || {};
        const status = error.response?.status;
        const url = String(cfg.url || "");
        const isAuthPath = url.includes("/auth/login") || url.includes("/auth/refresh")
            || url.includes("/auth/register") || url.includes("/auth/logout");
        // Access token expired → refresh once, retry once.
        if (status === 401 && !cfg._retried && !isAuthPath) {
            cfg._retried = true;
            try {
                await silentRefresh();
                return client(cfg);
            } catch (_) { /* fall through to original error */ }
        }
        // Legacy session without a csrf cookie → bootstrap it, retry once.
        if (status === 403 && !cfg._csrfRetried
            && error.response?.data?.detail?.code === "csrf_failed") {
            cfg._csrfRetried = true;
            try {
                await axios.get(`${API}/auth/csrf`, { withCredentials: true });
                return client(cfg);
            } catch (_) { /* fall through */ }
        }
        // Step-up MFA — sensitive action needs a fresh TOTP verification.
        const detail = error.response?.data?.detail;
        if (status === 403 && !cfg._stepUpRetried
            && (detail?.code === "step_up_required"
                || detail?.code === "mfa_enrollment_required")) {
            cfg._stepUpRetried = true;
            try {
                const token = await requestStepUp(detail);
                cfg.headers = { ...(cfg.headers || {}), "X-Step-Up-Token": token };
                return client(cfg);
            } catch (_) { /* user cancelled / not enrolled — fall through */ }
        }
        return Promise.reject(error);
    };
}

api.interceptors.response.use((r) => r, makeResponseInterceptor(api));
axios.interceptors.response.use((r) => r, makeResponseInterceptor(axios));

const _TRANSIENT = "Temporarily unreachable — retrying automatically. If this persists, check Bot Health.";

function _sanitizeErrorText(text) {
    const s = String(text ?? "").trim();
    if (!s) return null;
    // Never surface raw edge/proxy error pages (Cloudflare 52x prose, HTML
    // bodies, nginx pages) to users — they read like the app is broken.
    const low = s.toLowerCase();
    if (low.includes("<html") || low.includes("<!doctype")
        || low.includes("cloudflare") || low.includes("origin web server")
        || low.includes("bad gateway") || low.includes("nginx"))
        return _TRANSIENT;
    return s.length > 240 ? `${s.slice(0, 240)}…` : s;
}

export function formatApiError(err) {
    // 429s are transient (rate limiting) — never surface a raw axios error
    if (err?.response?.status === 429)
        return "Too many requests — the server is rate limiting. It will recover automatically in a moment.";
    const status = err?.response?.status;
    // Edge/gateway failures (Cloudflare 52x, 502/503/504) are transient
    // infrastructure hiccups, not application errors.
    if (status >= 502 || (!err?.response && err?.request))
        return _TRANSIENT;
    const detail = err?.response?.data?.detail;
    if (detail == null)
        return _sanitizeErrorText(
            typeof err?.response?.data === "string" ? err.response.data
                : err?.message) || "Something went wrong.";
    if (typeof detail === "string")
        return _sanitizeErrorText(detail) || "Something went wrong.";
    if (Array.isArray(detail))
        return detail.map(e => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
    // Structured error: backend now returns {code, message, ...} for friendly
    // surfaces — duplicate_account, account_suspended, terms_required, etc.
    if (detail && typeof detail.message === "string") return detail.message;
    if (detail && typeof detail.msg === "string") return detail.msg;
    return _sanitizeErrorText(String(detail)) || "Something went wrong.";
}

export default api;
