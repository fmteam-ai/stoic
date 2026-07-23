import { useEffect, useState } from "react";
import api from "@/lib/api";
import { BadgeCheck, XCircle, CheckCircle2 } from "lucide-react";
import { toast } from "sonner";

export function AccountCertification({ accountId }) {
    const [items, setItems] = useState(null);

    useEffect(() => {
        let dead = false;
        api.get("/accounts/certification")
            .then(({ data }) => { if (!dead) setItems(data.items || []); })
            .catch(() => { if (!dead) setItems([]); });
        return () => { dead = true; };
    }, [accountId]);

    const cert = items?.find(i => i.account_id === accountId);
    if (!cert) return null;

    const certify = async () => {
        try {
            await api.post(`/accounts/${accountId}/certify`);
            toast.success("Demo certification stamped");
            const { data } = await api.get("/accounts/certification");
            setItems(data.items || []);
        } catch (e) {
            toast.error("Certification blocked", {
                description: e?.response?.data?.detail?.message || "Fix failing checks first.",
            });
        }
    };

    return (
        <div className="mt-3 border border-[#1F1F1F] bg-black/40 p-3"
            data-testid={`certification-${accountId}`}>
            <div className="flex items-center gap-2 mb-2">
                <BadgeCheck className={`w-3.5 h-3.5 ${cert.certified ? "text-[#00FF41]" : "text-[#FFB000]"}`} />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                    GO-LIVE CERTIFICATION
                </span>
                <span className={`font-mono text-[10px] tracking-widest px-1.5 border ${
                    cert.certified
                        ? "border-[#00FF41]/40 text-[#00FF41]"
                        : "border-[#FFB000]/40 text-[#FFB000]"}`}
                    data-testid={`certification-verdict-${accountId}`}>
                    {cert.certified ? "CERTIFIED" : `${cert.passed}/${cert.total} CHECKS`}
                </span>
                {cert.can_certify && !cert.certified && (
                    <button onClick={certify}
                        data-testid={`certify-btn-${accountId}`}
                        className="ml-auto font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10">
                        CERTIFY NOW
                    </button>
                )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-4 gap-y-1">
                {cert.checks.map(c => (
                    <div key={c.key} className="flex items-center gap-1.5 font-mono text-[10px]"
                        data-testid={`cert-${c.key}-${accountId}`}
                        title={c.hint || undefined}>
                        {c.ok
                            ? <CheckCircle2 className="w-3 h-3 text-[#00FF41] shrink-0" />
                            : <XCircle className="w-3 h-3 text-[#FF3B30] shrink-0" />}
                        <span className="text-[#52525B]">{c.label}</span>
                        <span className={c.ok ? "text-[#A1A1AA]" : "text-[#FF3B30]"}>{c.value}</span>
                    </div>
                ))}
            </div>
        </div>
    );
}
