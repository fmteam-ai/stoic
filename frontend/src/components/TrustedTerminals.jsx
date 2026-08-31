import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { MonitorSmartphone, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

/* iter-173 — Trusted terminals per account: what is verified, since when,
 * and a two-step one-tap revoke. */
const METHOD_LABEL = {
    user_trust: "ONE-CLICK TRUST",
    installer: "INSTALLER",
    pairing: "PAIRING",
};

export function TrustedTerminals({ accountId, refreshKey }) {
    const [data, setData] = useState(null);
    const [confirming, setConfirming] = useState(null);
    const [revoking, setRevoking] = useState(null);

    const load = useCallback(async () => {
        try {
            const { data: d } = await api.get(`/accounts/${accountId}/installations`);
            setData(d);
        } catch { /* silent */ }
    }, [accountId]);

    useEffect(() => { load(); }, [load, refreshKey]);

    const revoke = async (instId) => {
        setRevoking(instId);
        try {
            await api.post(`/accounts/${accountId}/installations/${instId}/revoke`);
            toast.success("Terminal revoked — it can no longer execute live trades");
            setConfirming(null);
            await load();
        } catch (e) {
            toast.error("Revoke failed", { description: formatApiError(e) });
        } finally {
            setRevoking(null);
        }
    };

    if (!data || (data.installations || []).length === 0) return null;

    return (
        <div className="mt-3 border border-[#1F1F1F] bg-[#0A0A0A] p-3"
             data-testid={`trusted-terminals-${accountId}`}>
            <div className="flex items-center gap-2 mb-2">
                <MonitorSmartphone className="w-4 h-4 text-[#A1A1AA]" />
                <span className="font-mono text-[10px] tracking-widest text-[#52525B]">
                    TRUSTED TERMINALS
                </span>
            </div>
            <div className="space-y-1.5">
                {data.installations.map((inst) => (
                    <div key={inst.installation_id}
                         className="flex items-center justify-between gap-2 flex-wrap font-mono text-[10px] border border-[#141414] px-2.5 py-1.5"
                         data-testid={`terminal-row-${inst.installation_id}`}>
                        <div className="flex items-center gap-2 flex-wrap min-w-0">
                            <span className="px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700]">
                                {METHOD_LABEL[inst.method] || (inst.method || "").toUpperCase()}
                            </span>
                            <span className="text-[#A1A1AA] truncate">
                                {inst.fingerprint?.account_login
                                    ? `MT5 ${inst.fingerprint.account_login}`
                                    : inst.host || inst.installation_id}
                            </span>
                            {inst.created_at && (
                                <span className="text-[#52525B]">
                                    since {new Date(inst.created_at).toLocaleDateString()}
                                </span>
                            )}
                            {inst.is_current && (
                                <span className="flex items-center gap-1 text-[#00FF41]">
                                    <ShieldCheck className="w-3 h-3" /> VERIFIED
                                </span>
                            )}
                        </div>
                        {confirming === inst.installation_id ? (
                            <div className="flex items-center gap-1.5">
                                <button onClick={() => revoke(inst.installation_id)}
                                        disabled={revoking === inst.installation_id}
                                        data-testid={`confirm-revoke-${inst.installation_id}`}
                                        className="px-2 py-0.5 font-bold tracking-widest bg-[#FF3B30] text-white hover:bg-[#E5352B] disabled:opacity-50">
                                    {revoking === inst.installation_id ? "REVOKING…" : "CONFIRM REVOKE"}
                                </button>
                                <button onClick={() => setConfirming(null)}
                                        data-testid={`cancel-revoke-${inst.installation_id}`}
                                        className="px-2 py-0.5 text-[#A1A1AA] hover:text-white">
                                    CANCEL
                                </button>
                            </div>
                        ) : (
                            <button onClick={() => setConfirming(inst.installation_id)}
                                    data-testid={`revoke-terminal-${inst.installation_id}`}
                                    className="px-2 py-0.5 tracking-widest border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10">
                                REVOKE
                            </button>
                        )}
                    </div>
                ))}
            </div>
            <div className="mt-1.5 text-[9px] font-mono text-[#52525B]">
                Revoking a terminal blocks its live execution instantly — re-trust or re-pair to restore.
            </div>
        </div>
    );
}
