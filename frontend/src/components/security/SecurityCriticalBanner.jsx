import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import api from "@/lib/api";
import { ShieldAlert } from "lucide-react";

// Section 6: Critical findings → banner on every admin page while open.
export function SecurityCriticalBanner() {
    const { user } = useAuth();
    const location = useLocation();
    const [s, setS] = useState(null);
    const isAdmin = user?.role === "admin";
    useEffect(() => {
        if (!isAdmin) return undefined;
        let live = true;
        const load = () => api.get("/admin/security/status").then(r => live && setS(r.data)).catch(() => {});
        load();
        const t = setInterval(load, 60000);
        return () => { live = false; clearInterval(t); };
    }, [isAdmin]);
    const critical = s?.open_by_severity?.critical || 0;
    const onSecurityTab = location.pathname === "/admin/ops" && /tab=security|finding=/.test(location.search);
    if (!isAdmin || !critical || onSecurityTab) return null;
    return (
        <div className="mx-4 md:mx-8 mt-3 border border-[#FF3B30]/60 bg-[#FF3B30]/10 px-3 py-2 flex items-center gap-3" data-testid="security-critical-banner">
            <ShieldAlert className="w-4 h-4 text-[#FF3B30] shrink-0" />
            <div className="text-xs text-[#FECACA] flex-1">
                <span className="font-mono tracking-widest text-[#FF3B30]">SECURITY · {critical} CRITICAL</span> finding{critical > 1 ? "s" : ""} open — the Security &amp; Health Agent needs your review.
            </div>
            <Link to="/admin/ops?tab=security" className="font-mono text-[10px] tracking-widest text-[#FF3B30] hover:text-white border border-[#FF3B30]/60 px-2 py-1" data-testid="security-critical-banner-link">OPEN</Link>
        </div>
    );
}
