import { useEffect, useState, useCallback } from "react";
import { ClosedBetaBanner } from "@/components/ClosedBetaBanner";
import axios from "axios";
import { StoicMark } from "@/components/StoicLogo";
import {
    Activity, Database, Cpu, Plug, CreditCard, Mail, RefreshCw, Loader2,
} from "lucide-react";

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;

const COMPONENT_META = [
    { key: "api", label: "API", icon: Activity, desc: "Application endpoints" },
    { key: "database", label: "Database", icon: Database, desc: "Primary data store" },
    { key: "bot_engine", label: "Bot Engine", icon: Cpu, desc: "Signal scanning & execution loop" },
    { key: "ea_bridge", label: "EA Bridge", icon: Plug, desc: "MT5 terminal connectivity" },
    { key: "payments", label: "Payments", icon: CreditCard, desc: "Stripe checkout" },
    { key: "email", label: "Email", icon: Mail, desc: "Transactional delivery" },
];

const STATUS_STYLE = {
    operational: { cls: "text-[#00FF41] border-[#00FF41]/40 bg-[#00FF41]/10", label: "OPERATIONAL" },
    idle: { cls: "text-[#A1A1AA] border-[#1F1F1F] bg-[#0F0F0F]", label: "IDLE" },
    degraded: { cls: "text-[#FFB000] border-[#FFB000]/40 bg-[#FFB000]/10", label: "DEGRADED" },
    down: { cls: "text-[#FF3B30] border-[#FF3B30]/40 bg-[#FF3B30]/10", label: "DOWN" },
    not_configured: { cls: "text-[#52525B] border-[#1F1F1F] bg-[#0F0F0F]", label: "NOT CONFIGURED" },
    unknown: { cls: "text-[#52525B] border-[#1F1F1F] bg-[#0F0F0F]", label: "UNKNOWN" },
};

const STATUS_TIMEOUT_MS = 8000;   // A16-2 — after this the page says "unavailable" instead of spinning

const OVERALL = {
    operational: { text: "Platform available · trading ready", cls: "text-[#00FF41] border-[#00FF41]/40" },
    degraded: { text: "Platform available · trading degraded", cls: "text-[#FFB000] border-[#FFB000]/40" },
    major_outage: { text: "Major outage", cls: "text-[#FF3B30] border-[#FF3B30]/40" },
};

export default function StatusPage() {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(false);
    const [checkedAt, setCheckedAt] = useState(null);        // A16-2 — time of the last result (success or failure)
    const [timedOut, setTimedOut] = useState(false);

    const load = useCallback(() => {
        axios.get(`${API}/status`, { timeout: STATUS_TIMEOUT_MS })
            .then(r => { setData(r.data); setErr(false); })
            .catch(() => setErr(true))
            .finally(() => { setCheckedAt(new Date()); setTimedOut(false); });
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 60000);
        return () => clearInterval(t);
    }, [load]);
    useEffect(() => {
        // A16-2 — a spinner is not a status: after STATUS_TIMEOUT_MS without any result say so, with the time
        if (data || err) return undefined;
        const t = setTimeout(() => { setTimedOut(true); setCheckedAt(new Date()); }, STATUS_TIMEOUT_MS);
        return () => clearTimeout(t);
    }, [data, err]);

    const utc = (d) => d ? `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC` : "";
    const overall = (err || timedOut)
        ? { text: `Status unavailable (checked ${utc(checkedAt)})`, cls: "text-[#FF3B30] border-[#FF3B30]/40" }
        : OVERALL[data?.overall] || null;

    return (
        <div className="min-h-screen bg-[#050505] text-[#FAFAFA]">
            <ClosedBetaBanner />
            <div className="max-w-3xl mx-auto px-4 py-12">
                <div className="flex items-center gap-3 mb-10">
                    <StoicMark className="w-8 h-8" />
                    <div>
                        <div className="font-display font-bold tracking-widest">STOIC</div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SYSTEM STATUS</div>
                    </div>
                </div>

                {!data && !err && !timedOut ? (
                    <div className="flex items-center justify-center gap-3 py-20 font-mono text-xs text-[#71717A] tracking-widest" data-testid="status-checking">
                        <Loader2 className="w-5 h-5 animate-spin text-[#52525B]" /> CHECKING STATUS…
                    </div>
                ) : (
                    <>
                        {overall && (
                            <div className={`border px-5 py-4 mb-3 font-display text-lg ${overall.cls}`}
                                data-testid="status-overall">
                                {(err || timedOut) ? overall.text : (data?.headline || overall.text)}
                                <div className="font-mono text-[10px] text-[#71717A] tracking-widest mt-1" data-testid="status-last-result">
                                    LAST RESULT {utc(checkedAt) || "—"}{(err || timedOut) && data ? " · showing the previous snapshot below" : ""}
                                </div>
                            </div>
                        )}
                        {data?.trading && (
                            <div className="border border-[#1F1F1F] bg-[#0A0A0A] px-5 py-3 mb-8 font-mono text-[11px] text-[#A1A1AA] grid sm:grid-cols-2 gap-x-6 gap-y-1"
                                data-testid="status-trading-provenance">
                                <div>readiness: <span className="text-white" data-testid="status-readiness-state">{data.trading.readiness?.state || "UNKNOWN"}</span>
                                    {data.trading.readiness?.dominant_code && <span className="text-[#FFB000]"> · {data.trading.readiness.dominant_code}</span>}</div>
                                <div>connectivity: <span className="text-white">{data.trading.connectivity || "—"}</span></div>
                                <div>decision: <span className="text-white">{data.trading.readiness?.decision_id || "—"}</span>
                                    {data.trading.attestation?.input_version != null && <span> · input v{data.trading.attestation.input_version}</span>}</div>
                                <div>attestation: <span className={data.trading.attestation?.attested ? "text-[#00FF41]" : "text-[#FFB000]"}>
                                    {data.trading.attestation?.attested ? "attested" : "not attested"}</span>
                                    {data.trading.attestation?.basis && <span> · {data.trading.attestation.basis}</span>}
                                    {data.trading.attestation?.inventory_hash && <span> · inv {data.trading.attestation.inventory_hash}</span>}</div>
                                <div>new exposure: <span className="text-white">{data.trading.readiness?.new_exposure_allowed ? "allowed" : "refused"}</span></div>
                                <div data-testid="status-release-identity">release: <span className={data.release?.signed ? "text-[#00FF41]" : "text-[#FFB000]"}>
                                    {data.release?.signed ? `signed ${data.release.release_id}` : (data.release?.note || "unsigned")}</span>
                                    {data.release?.short_commit && <span> · commit {data.release.short_commit}</span>}</div>
                                <div>as of: <span className="text-white">{data.trading.attestation?.as_of || data.checked_at || "—"}</span></div>
                                {Array.isArray(data.trading.requirements) && data.trading.requirements.length > 0 && (
                                    <div className="sm:col-span-2 border-t border-[#1F1F1F] pt-2 mt-1" data-testid="status-requirements">
                                        <div className="text-[#52525B] tracking-widest mb-1">READINESS REQUIREMENTS — nothing trades until every line passes</div>
                                        <ul className="space-y-0.5">
                                            {data.trading.requirements.map((r) => (
                                                <li key={r.id} data-testid={`status-requirement-${r.id}`} className="flex items-start gap-2">
                                                    <span className={r.ok ? "text-[#00FF41]" : "text-[#FF3B30]"}>{r.ok ? "PASS" : "FAIL"}</span>
                                                    <span className={r.ok ? "text-[#A1A1AA]" : "text-white"}>{r.label}</span>
                                                </li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                            </div>
                        )}
                        <div className="space-y-2" data-testid="status-components">
                            {COMPONENT_META.map(m => {
                                const comp = data?.components?.[m.key];
                                const st = STATUS_STYLE[comp?.status] || STATUS_STYLE.unknown;
                                return (
                                    <div key={m.key} data-testid={`status-${m.key}`}
                                        className="flex items-center gap-4 bg-[#0A0A0A] border border-[#1F1F1F] px-4 py-3">
                                        <m.icon className="w-4 h-4 text-[#52525B]" />
                                        <div className="flex-1">
                                            <div className="text-sm">{m.label}</div>
                                            <div className="text-[11px] text-[#52525B]">{comp?.note || m.desc}</div>
                                        </div>
                                        <span className={`px-2 py-0.5 border text-[10px] font-mono tracking-widest ${st.cls}`}>
                                            {st.label}
                                        </span>
                                    </div>
                                );
                            })}
                        </div>
                        <div className="flex items-center justify-between mt-6 text-[10px] font-mono text-[#52525B] tracking-widest">
                            <span>CHECKED {utc(checkedAt) || "—"} · AUTO-REFRESH 60s</span>
                            <button onClick={load} data-testid="status-refresh"
                                className="flex items-center gap-1.5 hover:text-white">
                                <RefreshCw className="w-3 h-3" /> REFRESH
                            </button>
                        </div>
                        <div className="mt-10 text-[10px] font-mono text-[#52525B] tracking-widest">
                            NEED HELP? <a href="/support" className="text-[#00FF41] hover:underline">OPEN A SUPPORT TICKET</a>
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}
