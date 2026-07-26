import { createContext, useContext, useEffect, useState, useCallback, useMemo } from "react";
import api, { formatApiError } from "@/lib/api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
    // null = checking, false = logged out, object = user
    const [user, setUser] = useState(null);

    const refresh = useCallback(async () => {
        try {
            const { data } = await api.get("/auth/me");
            setUser(data);
            return data;
        } catch {
            setUser(false);
            return null;
        }
    }, []);

    useEffect(() => { refresh(); }, [refresh]);

    const login = useCallback(async (email, password, totp_code, email_otp) => {
        const body = { email, password };
        if (totp_code) body.totp_code = totp_code;
        if (email_otp) body.email_otp = email_otp;
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
        () => ({ user, login, register, verifyEmail, resendActivation, logout, refresh, formatApiError }),
        [user, login, register, verifyEmail, resendActivation, logout, refresh],
    );

    return (
        <AuthContext.Provider value={value}>
            {children}
        </AuthContext.Provider>
    );
}

export const useAuth = () => useContext(AuthContext);
