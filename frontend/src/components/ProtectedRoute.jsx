import { Navigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/context/AuthContext";

export function ProtectedRoute({ children, requireAdmin = false }) {
    const { user } = useAuth();
    if (user === null) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505]" data-testid="route-loading-screen">
                <div className="flex flex-col items-center gap-4">
                    <Loader2 className="h-8 w-8 text-emerald-400 animate-spin" />
                    <div className="font-mono text-sm text-emerald-300 tracking-widest animate-pulse">INITIALIZING TERMINAL...</div>
                </div>
            </div>
        );
    }
    if (user === false) return <Navigate to="/welcome" replace />;
    if (requireAdmin && user?.role !== "admin") return <Navigate to="/" replace />;
    return children;
}
