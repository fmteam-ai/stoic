import { createContext, useContext, useEffect, useState, useCallback, useMemo } from "react";
import api, { formatApiError } from "@/lib/api";

const AuthContext = createContext(null);
export const AUTH_CHECK_TIMEOUT_MS = 8000;

export function AuthProvider({ children }) {
    // null = checking, false = logged out (explicit 401/403), object = user
    const [user, setUser] = useState(null);
    // Round-6 P0: a protected route must render ONE of three explicit states —
    // app, login boundary, or OUTAGE. A backend/network failure on /auth/me is
    // NOT "logged out": keep user=null and expose the outage with correlation id.
    const [outage, setOutage] = useState(null);

    const refresh = useCallback(async () => {
        // A20-P2-02 / M117-3: the auth check has an 8 s deadline — a hung backend shows the outage
        // screen (with retry) instead of INITIALIZING TERMINAL forever.
        let timer;
        const deadline = new Promise((_, reject) => {
            timer = setTimeout(() => reject(Object.assign(new Error("auth check timed out after 8 s"), { code: "AUTH_TIMEOUT" })), AUTH_CHECK_TIMEOUT_MS);
        });
        try {
            const { data } = await Promise.race([api.get("/auth/me", { timeout: AUTH_CHECK_TIMEOUT_MS }), deadline]);
            setOutage(null);
            setUser(data);
            return data;
        } catch (err) {
            const status = err?.response?.status;
            if (status === 401 || status === 403) {
                setOutage(null);
                setUser(false);
                return null;
            }
            const headers = err?.response?.headers || {};
            const timedOut = err?.code === "AUTH_TIMEOUT" || err?.code === "ECONNABORTED";
            setOutage((prev) => ({
                status: status || 0,
                correlationId: headers["x-request-id"] || headers["x-trace-id"]
                    || `client_${Math.random().toString(16).slice(2, 14)}`,
                message: status ? `backend responded ${status}`
                    : (timedOut ? "backend did not answer the sign-in check within 8 s" : "backend unreachable (network error / timeout)"),
                at: new Date().toISOString(),
                attempt: (prev?.attempt || 0) + 1,
            }));
            setUser(null);
            return null;
        } finally {
            clearTimeout(timer);
        }
    }, []);

    useEffect(() => { refresh(); }, [refresh]);

    // automatic bounded retry while in outage (5s → 10s → 20s → 30s cap)
    useEffect(() => {
        if (!outage) return undefined;
        const delay = Math.min(30000, 5000 * 2 ** Math.min(3, outage.attempt - 1));
        const id = setTimeout(() => { refresh(); }, delay);
        return () => clearTimeout(id);
    }, [outage, refresh]);

    const login = useCallback(async (email, password, totp_code, email_otp, turnstile_token) => {
        const body = { email, password };
        if (totp_code) body.totp_code = totp_code;
        if (email_otp) body.email_otp = email_otp;
        if (turnstile_token) body.turnstile_token = turnstile_token;
        const { data } = await api.post("/auth/login", body);
        setUser(data);
        return data;
    }, []);

    const register = useCallback(async (email, password, name, opts = {}) => {
        const body = { email, password, name, ...opts };
        const { data } = await api.post("/auth/register", body);
        // Do NOT setUser here — the user must verify email before being
        // considered authenticated. Caller routes to the "check inbox" screen.
        return data;
    }, []);

    const verifyEmail = useCallback(async (token) => {
        const { data } = await api.post("/auth/verify-email", { token });
        if (data?.user) setUser(data.user);
        return data;
    }, []);

    const resendActivation = useCallback(async (email) => {
        const { data } = await api.post("/auth/resend-activation", { email });
        return data;
    }, []);

    const logout = useCallback(async () => {
        try { await api.post("/auth/logout"); }
        catch (err) { console.warn("[auth] logout network error (ignored)", err?.message); }
        setUser(false);
    }, []);

    const value = useMemo(
        () => ({ user, outage, login, register, verifyEmail, resendActivation, logout, refresh, formatApiError }),
        [user, outage, login, register, verifyEmail, resendActivation, logout, refresh],
    );

    return (
        <AuthContext.Provider value={value}>
            {children}
        </AuthContext.Provider>
    );
}

export const useAuth = () => useContext(AuthContext);
