import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { CheckCircle2, AlertTriangle, Loader2, Send } from "lucide-react";

/** Admin → Integrations: prove the security Telegram chat works before the demo (pairing alerts land there). */
export function SecurityAlertsCard() {
    const [st, setSt] = useState(null);
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState(null);
    const load = async () => {
        try { const { data } = await api.get("/admin/security-alerts/status"); setSt(data); }
        catch (e) { setSt({ configured: false, hint: formatApiError(e) }); }
    };
    useEffect(() => { load(); }, []);
    const sendTest = async () => {
        setBusy(true); setResult(null);
        try { const { data } = await api.post("/admin/security-alerts/test"); setResult(data); }
        catch (e) { setResult({ ok: false, detail: formatApiError(e) }); }
        finally { setBusy(false); load(); }
    };
    if (!st) return null;
    return (
        <div data-testid="integration-card-security-telegram" className="border border-[#1F1F1F] bg-[#0A0A0A] p-4">
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    {st.configured ? <CheckCircle2 className="w-4 h-4 text-[#00FF41]" /> : <AlertTriangle className="w-4 h-4 text-[#FFB020]" />}
                    <h3 className="text-sm font-semibold text-[#E4E4E7]">Security alerts (Telegram)</h3>
                </div>
                <button onClick={sendTest} disabled={busy || !st.configured} data-testid="security-alert-test-btn"
                    className="px-2.5 py-1 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5 disabled:opacity-50">
                    {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />} SEND TEST ALERT
                </button>
            </div>
            <div className="text-[11px] font-mono text-[#52525B] mb-2" data-testid="security-alert-status">
                {st.configured
                    ? <>bot token from <span className="text-[#A1A1AA]">{st.token_source === "secrets_file" ? "secrets/security_telegram_token" : "backend/.env"}</span> · chat <span className="text-[#A1A1AA]">{st.chat_id_masked}</span></>
                    : <span className="text-[#FFB020]">{st.hint || "not configured"}</span>}
                {st.last_test?.at && <span className="block">last test {st.last_test.at.slice(0, 16)} · {st.last_test.ok ? "delivered" : "FAILED"} · by {st.last_test.by}</span>}
            </div>
            {result && <div data-testid="security-alert-test-result"
                className={`text-xs mb-2 px-2 py-1.5 border ${result.ok ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}>{result.detail}</div>}
            <div className="text-[11px] text-[#52525B]">Pairing alerts (VPS terminal silent for 10 min) and security-agent findings are pushed to this chat. Max 3 tests per 10 minutes; every test is written to the audit chain.</div>
        </div>
    );
}

export default SecurityAlertsCard;
