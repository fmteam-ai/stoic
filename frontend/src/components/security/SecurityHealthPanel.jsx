import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { ObserveScorecard } from "./ObserveScorecard";
import { ShieldAlert, ShieldCheck, Loader2, RefreshCw, Send, FileDown } from "lucide-react";
import { FindingDetail, SEV_CLS, SevPill } from "./FindingDetail";
import { ActiveBlocksList, CheckStatusTable, WouldHaveDoneList } from "./SecurityTables";

const LIGHT = {
    green: { cls: "bg-[#00FF41]", label: "NO OPEN HIGH / CRITICAL" },
    amber: { cls: "bg-[#FFB000]", label: "OPEN HIGH FINDINGS" },
    red: { cls: "bg-[#FF3B30]", label: "CRITICAL OPEN OR CAP REACHED" },
};
const AREAS = ["access", "bridge", "secrets", "dependencies", "integrity", "platform", "trading"];
const SEVS = ["critical", "high", "medium", "low"];

const sel = "bg-[#0A0A0A] border border-[#1F1F1F] text-[11px] font-mono text-[#A1A1AA] px-2 py-1 focus:outline-none focus:border-[#00FF41]/50";
const btn = "px-3 py-1.5 text-[11px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41] flex items-center gap-1.5 disabled:opacity-40";

export function SecurityHealthPanel({ initialFindingId }) {
    const [status, setStatus] = useState(null);
    const [findings, setFindings] = useState([]);
    const [filters, setFilters] = useState({ area: "", severity: "", status: "" });
    const [selected, setSelected] = useState(initialFindingId || null);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        try {
            const p = new URLSearchParams();
            Object.entries(filters).forEach(([k, v]) => v && p.set(k, v));
            const [s, f] = await Promise.all([api.get("/admin/security/status"), api.get(`/admin/security/findings?${p}`)]);
            setStatus(s.data); setFindings(f.data.findings || []);
        } catch (e) { toast.error(formatApiError(e)); }
    }, [filters]);
    useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t); }, [load]);

    const setMode = async (mode, rulesEnabled) => {
        setBusy(true);
        try {
            const body = { mode };
            if (rulesEnabled) body.rules_enabled = rulesEnabled;
            const { data } = await api.post("/admin/security/mode", body);
            toast.success(`Agent mode: ${data.mode.toUpperCase()} · rules ${data.rules_enabled.length ? data.rules_enabled.join(", ") : "none"}`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };
    const toggleRule = (r) => {
        const cur = status.rules_enabled || [];
        setMode(status.mode, cur.includes(r) ? cur.filter(x => x !== r) : [...cur, r]);
    };
    const testAlert = async () => {
        setBusy(true);
        try {
            const { data } = await api.post("/admin/security/test-alert");
            toast.success(`Test finding ${data.created ? "opened" : "already open"} — alert goes out on the next tick${data.telegram_configured ? "" : " (Telegram not configured: email/log only)"}`);
            await load();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };
    // S16 — fetched through the authenticated api client (cookies + CSRF), so it works when the
    // backend lives on another domain than the app; served as a blob download.
    const openReport = async (kind) => {
        try {
            const { data } = await api.get(`/admin/security/reports/${kind}`, { params: { format: "html", build: true }, responseType: "blob" });
            const url = URL.createObjectURL(data);
            const a = document.createElement("a");
            a.href = url; a.download = `stoic-security-${kind}-${new Date().toISOString().slice(0, 10)}.html`; a.click();
            setTimeout(() => URL.revokeObjectURL(url), 10_000);
        } catch (e) { toast.error(e?.response?.data?.detail?.message || "Report download failed"); }
    };

    if (!status) return <div className="flex justify-center py-16" data-testid="security-panel-loading"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>;
    const light = LIGHT[status.light] || LIGHT.green;
    const open = status.open_by_severity || {};

    return (
        <div className="space-y-4" data-testid="security-health-panel">
            <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-4 flex flex-wrap items-center gap-4" data-testid="security-status-strip">
                <div className="flex items-center gap-3">
                    <span className={`w-3.5 h-3.5 rounded-full ${light.cls} shadow-[0_0_12px_currentColor]`} data-testid="security-light" data-light={status.light} />
                    <div>
                        <div className="font-display text-sm text-white">Security &amp; Health Agent</div>
                        <div className="font-mono text-[10px] tracking-widest text-[#71717A]">{light.label}</div>
                    </div>
                </div>
                <div className="flex gap-2 font-mono text-[11px]" data-testid="security-open-counts">
                    {SEVS.map(s => <span key={s} className={`px-2 py-0.5 border ${SEV_CLS[s]}`}>{s.toUpperCase()} {open[s] || 0}</span>)}
                </div>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                    <span className="font-mono text-[10px] text-[#71717A]">MODE</span>
                    <div className="flex border border-[#1F1F1F]" data-testid="security-mode-switch">
                        {["observe", "enforce"].map(m => (
                            <button key={m} disabled={busy || status.mode === m} onClick={() => setMode(m)} data-testid={`security-mode-${m}`}
                                className={`px-3 py-1 text-[11px] font-mono tracking-widest ${status.mode === m ? (m === "enforce" ? "bg-[#FF3B30]/20 text-[#FF3B30]" : "bg-[#00FF41]/10 text-[#00FF41]") : "text-[#52525B] hover:text-white"}`}>
                                {m.toUpperCase()}
                            </button>
                        ))}
                    </div>
                    <button className={btn} onClick={testAlert} disabled={busy} data-testid="security-test-alert-btn"><Send className="w-3 h-3" /> TEST ALERT</button>
                    <button className={btn} onClick={() => openReport("daily")} data-testid="security-report-daily-btn"><FileDown className="w-3 h-3" /> DAILY</button>
                    <button className={btn} onClick={() => openReport("weekly")} data-testid="security-report-weekly-btn"><FileDown className="w-3 h-3" /> WEEKLY</button>
                    <button className={btn} onClick={load} data-testid="security-refresh-btn"><RefreshCw className="w-3 h-3" /></button>
                </div>
            </div>
            {status.mode === "observe" && (
                <div className="font-mono text-[10px] text-[#71717A] border border-[#1F1F1F] px-3 py-2" data-testid="security-observe-note">
                    OBSERVE MODE — rules R1–R8 are evaluated and logged as “would have done”; nothing is blocked. Worker lease: {status.worker_lease?.holder || "—"} · {status.checks_total} checks · {status.protected_ips_count} protected IPs/CIDRs configured.
                </div>
            )}
            <div className="bg-[#0A0A0A] border border-[#1F1F1F] px-3 py-2 flex flex-wrap items-center gap-2" data-testid="security-rules-strip">
                <span className="font-mono text-[10px] tracking-widest text-[#71717A]">RULES {status.mode === "enforce" ? "ENFORCED" : "ARMED FOR ENFORCE"}</span>
                {["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"].map(r => {
                    const on = (status.rules_enabled || []).includes(r);
                    return (
                        <button key={r} disabled={busy} onClick={() => toggleRule(r)} data-testid={`security-rule-toggle-${r}`} data-on={on}
                            className={`px-2.5 py-1 font-mono text-[10px] tracking-widest border ${on ? "border-[#00FF41]/60 text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#52525B] hover:text-white"}`}>
                            {r}
                        </button>
                    );
                })}
                <span className="font-mono text-[10px] text-[#52525B] ml-auto">switch on one at a time · R1 and R4 first · step-up required</span>
            </div>

            <ObserveScorecard status={status} onToggleRule={toggleRule} busy={busy} />

            <div className="grid xl:grid-cols-3 gap-3">
                <div className="xl:col-span-2 bg-[#0A0A0A] border border-[#1F1F1F]" data-testid="security-findings-panel">
                    <div className="px-4 py-2.5 border-b border-[#1F1F1F] flex flex-wrap items-center gap-2">
                        <ShieldAlert className="w-3.5 h-3.5 text-[#00FF41]" />
                        <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA] uppercase">Findings · {findings.length}</span>
                        <div className="ml-auto flex gap-2">
                            <select className={sel} value={filters.area} onChange={e => setFilters({ ...filters, area: e.target.value })} data-testid="security-filter-area">
                                <option value="">ALL AREAS</option>{AREAS.map(a => <option key={a} value={a}>{a.toUpperCase()}</option>)}
                            </select>
                            <select className={sel} value={filters.severity} onChange={e => setFilters({ ...filters, severity: e.target.value })} data-testid="security-filter-severity">
                                <option value="">ALL SEVERITIES</option>{SEVS.map(a => <option key={a} value={a}>{a.toUpperCase()}</option>)}
                            </select>
                            <select className={sel} value={filters.status} onChange={e => setFilters({ ...filters, status: e.target.value })} data-testid="security-filter-status">
                                <option value="">OPEN (ALL)</option>
                                {["open", "contained", "acknowledged", "resolved", "false_positive"].map(a => <option key={a} value={a}>{a.replace(/_/g, " ").toUpperCase()}</option>)}
                            </select>
                        </div>
                    </div>
                    <div className="divide-y divide-[#141414] max-h-[520px] overflow-y-auto">
                        {findings.length === 0 && <div className="p-6 text-xs text-[#52525B] flex items-center gap-2" data-testid="security-findings-empty"><ShieldCheck className="w-4 h-4 text-[#00FF41]" /> No findings match.</div>}
                        {findings.map(f => (
                            <button key={f.id} onClick={() => setSelected(f.id)} data-testid={`security-finding-row-${f.check_id}`}
                                className={`w-full text-left px-4 py-2.5 hover:bg-[#111] flex items-start gap-3 ${selected === f.id ? "bg-[#111]" : ""}`}>
                                <SevPill sev={f.severity} />
                                <div className="min-w-0 flex-1">
                                    <div className="text-xs text-white truncate">{f.check_id} · {f.title}</div>
                                    <div className="text-[11px] text-[#71717A] truncate">{f.what_happened}</div>
                                </div>
                                <div className="text-right font-mono text-[10px] text-[#52525B] shrink-0">
                                    <div>{f.status.toUpperCase()}</div><div>×{f.occurrences} · {String(f.last_seen).slice(11, 16)}Z</div>
                                </div>
                            </button>
                        ))}
                    </div>
                </div>
                <FindingDetail id={selected} onChanged={load} onClose={() => setSelected(null)} />
            </div>

            <div className="grid xl:grid-cols-2 gap-3">
                <WouldHaveDoneList onChanged={load} />
                <div className="space-y-3">
                    <ActiveBlocksList onChanged={load} />
                    <CheckStatusTable />
                </div>
            </div>
        </div>
    );
}
