import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import {
    CheckCircle2, Circle, Copy, Loader2, Plug, Terminal, ArrowRight,
} from "lucide-react";

const Field = ({ label, value, onChange, placeholder, testid }) => (
    <div>
        <label className="block text-[10px] font-mono tracking-widest text-[#52525B] uppercase mb-1">{label}</label>
        <input value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
            data-testid={testid}
            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41]/50 outline-none text-sm text-[#FAFAFA] px-3 py-2 font-mono" />
    </div>
);

export default function Connect() {
    const navigate = useNavigate();
    const [form, setForm] = useState({ label: "", broker: "", server: "", account_number: "" });
    const [starting, setStarting] = useState(false);
    const [conn, setConn] = useState(null);   // {account_id, install_command, ...}
    const [status, setStatus] = useState(null);
    const pollRef = useRef(null);

    const set = (k) => (v) => setForm(f => ({ ...f, [k]: v }));

    const start = async () => {
        setStarting(true);
        try {
            const { data } = await api.post("/connect/start", form);
            setConn(data);
            toast.success("Account registered — run the command below on your MT5 machine.");
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setStarting(false); }
    };

    useEffect(() => {
        if (!conn?.account_id) return;
        const poll = async () => {
            try { setStatus((await api.get(`/connect/${conn.account_id}/status`)).data); }
            catch { /* silent */ }
        };
        poll();
        pollRef.current = setInterval(poll, 5000);
        return () => clearInterval(pollRef.current);
    }, [conn?.account_id]);

    const copy = async () => {
        try { await navigator.clipboard.writeText(conn.install_command); toast.success("Install command copied"); }
        catch { toast.error("Clipboard blocked — select & copy manually."); }
    };

    return (
        <AppLayout>
            <div data-testid="connect-page" className="max-w-3xl">
                <PageHeader title="STOIC Connect"
                    subtitle="Connect an MT5 account like you'd connect Stripe — one form, one command, done."
                    testid="connect-header" />

                {!conn && (
                    <div className="border border-[#1F1F1F] p-5" data-testid="connect-form">
                        <div className="flex items-center gap-2 mb-4">
                            <Plug className="w-4 h-4 text-[#00FF41]" />
                            <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">Step 1 — tell us about the account</span>
                        </div>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                            <Field label="Nickname" value={form.label} onChange={set("label")} placeholder="My Gold Account" testid="connect-label-input" />
                            <Field label="Broker" value={form.broker} onChange={set("broker")} placeholder="ICMarkets" testid="connect-broker-input" />
                            <Field label="Broker server" value={form.server} onChange={set("server")} placeholder="ICMarketsSC-Live04" testid="connect-server-input" />
                            <Field label="MT5 account number" value={form.account_number} onChange={set("account_number")} placeholder="1234567" testid="connect-account-input" />
                        </div>
                        <button onClick={start} disabled={starting} data-testid="connect-start-btn"
                            className="px-4 py-2 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1.5">
                            {starting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ArrowRight className="w-3.5 h-3.5" />}
                            CONNECT THIS ACCOUNT
                        </button>
                        <div className="text-xs text-[#52525B] mt-3">
                            VPS, EA install, certificates and reconciliation are handled by STOIC — you never touch MetaEditor.
                        </div>
                    </div>
                )}

                {conn && (
                    <>
                        <div className="border border-[#FFD700]/30 bg-[#FFD700]/5 p-5 mb-4" data-testid="connect-command-panel">
                            <div className="flex items-center gap-2 mb-3">
                                <Terminal className="w-4 h-4 text-[#FFD700]" />
                                <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">Step 2 — run this once in PowerShell on your MT5 machine / VPS</span>
                            </div>
                            <div className="flex items-start gap-2">
                                <code className="flex-1 text-[11px] font-mono text-[#FFD700] bg-[#0A0A0A] border border-[#1F1F1F] p-3 break-all"
                                    data-testid="connect-install-command">
                                    {conn.install_command}
                                </code>
                                <button onClick={copy} data-testid="connect-copy-btn"
                                    className="p-2 border border-[#1F1F1F] hover:border-[#FFD700]/40 text-[#A1A1AA] shrink-0">
                                    <Copy className="w-4 h-4" />
                                </button>
                            </div>
                            <div className="text-xs text-[#52525B] mt-2">
                                Token expires {new Date(conn.pairing_expires_at).toLocaleTimeString()} — everything after this is automatic.
                            </div>
                        </div>

                        <div className="border border-[#1F1F1F] p-5" data-testid="connect-progress-panel">
                            <div className="flex items-center justify-between mb-4">
                                <span className="text-xs font-mono tracking-widest text-[#A1A1AA] uppercase">Step 3 — watch it connect</span>
                                <span className="text-xs font-mono text-[#00FF41]" data-testid="connect-progress-pct">
                                    {status ? `${status.progress_pct}%` : "…"}
                                </span>
                            </div>
                            <div className="h-1.5 bg-[#1F1F1F] mb-4">
                                <div className="h-full bg-[#00FF41] transition-all duration-700"
                                    style={{ width: `${status?.progress_pct || 0}%` }} />
                            </div>
                            {(status?.steps || []).map(s => (
                                <div key={s.key} className="flex items-start gap-2.5 py-1.5" data-testid={`connect-step-${s.key}`}>
                                    {s.done
                                        ? <CheckCircle2 className="w-4 h-4 text-[#00FF41] mt-0.5 shrink-0" />
                                        : <Circle className="w-4 h-4 text-[#3F3F46] mt-0.5 shrink-0" />}
                                    <div>
                                        <div className={`text-sm ${s.done ? "text-[#FAFAFA]" : "text-[#71717A]"}`}>{s.label}</div>
                                        <div className="text-xs text-[#52525B]">{s.detail}</div>
                                    </div>
                                </div>
                            ))}
                            {status?.state === "CONNECTED" && (
                                <button onClick={() => navigate("/certification")} data-testid="connect-certify-btn"
                                    className="mt-4 px-4 py-2 text-xs font-mono tracking-widest border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1.5">
                                    <ArrowRight className="w-3.5 h-3.5" /> GET IT CERTIFIED
                                </button>
                            )}
                        </div>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
