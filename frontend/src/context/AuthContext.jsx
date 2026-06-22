import { createContext, useContext, useEffect, useState, useCallback } from "react";
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

    const login = async (email, password) => {
        const { data } = await api.post("/auth/login", { email, password });
        setUser(data);
        return data;
    };

    const register = async (email, password, name) => {
        const { data } = await api.post("/auth/register", { email, password, name });
        setUser(data);
        return data;
    };

    const logout = async () => {
        try { await api.post("/auth/logout"); }
        catch (err) { console.warn("[auth] logout network error (ignored)", err?.message); }
        setUser(false);
    };

    return (
        <AuthContext.Provider value={{ user, login, register, logout, refresh, formatApiError }}>
            {children}
        </AuthContext.Provider>
    );
}

export const useAuth = () => useContext(AuthContext);
