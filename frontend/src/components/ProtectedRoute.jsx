import { Navigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";

export function ProtectedRoute({ children, requireAdmin = false }) {
    const { user } = useAuth();
    if (user === null) {
        return (
            <div className="min-h-screen flex items-center justify-center bg-[#050505]">
                <div className="font-mono text-xs text-[#52525B] tracking-wider">INITIALIZING TERMINAL...</div>
            </div>
        );
    }
    if (user === false) return <Navigate to="/welcome" replace />;
    if (requireAdmin && user?.role !== "admin") return <Navigate to="/" replace />;
    return children;
}
