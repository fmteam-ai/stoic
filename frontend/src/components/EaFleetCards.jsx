import { useCallback, useEffect, useState } from "react";
import { Cpu, RefreshCw } from "lucide-react";
import api from "@/lib/api";

const short = (h) => (h ? `${h.slice(0, 12)}…` : "—");
const ago = (sec) => (sec == null ? "never" : sec < 90 ? `${sec}s` : sec < 5400 ? `${Math.round(sec / 60)}m` : `${Math.round(sec / 3600)}h`);

// v1.60.43 — Stable EX5 panel: the EX5 hashes the server accepts (signed record: current + signed previous) and how many
// terminals run each; any ENABLED terminal on an unaccepted build is the 14 Oct failure shape (DEMO voided → CLOSE_ONLY).
export function StableEx5Card({ data }) {
    const d = data;
    const bad = d?.unaccepted_enabled || [];
    return (
        <div className={`border ${bad.length ? "border-[#FF3B30]/50" : "border-[#1F1F1F]"} bg-[#0A0A0A]`} data-testid="stable-ex5-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Cpu className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-display font-bold text-sm">Accepted EA Builds (EX5)</span>
                <span className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${bad.length ? "border-[#FF3B30]/50 text-[#FF3B30]" : "border-[#00FF41]/40 text-[#00FF41]"}`}
                    data-testid="stable-ex5-verdict">
                    {d ? (bad.length ? `${bad.length} TERMINAL(S) ON UNACCEPTED BUILD` : d.record?.signed ? "FLEET ACCEPTED" : "NO SIGNED RECORD") : "…"}
                </span>
            </div>
            <div className="px-4 py-2 font-mono text-[11px]">
                {(d?.accepted || []).map((a) => (
                    <div key={a.sha256} className="flex justify-between gap-3 py-1 border-b border-[#141414] last:border-0" data-testid={`accepted-ex5-${a.role}`}>
                        <span className="text-[#A1A1AA]">{a.role.toUpperCase()}{a.version ? ` · EA ${a.version}` : ""} <span className="text-[#52525B]">{short(a.sha256)}</span></span>
                        <span className="text-[#71717A]">{a.terminals} terminal{a.terminals === 1 ? "" : "s"}</span>
                    </div>
                ))}
                {bad.map((t) => (
                    <div key={t.account_id} className="flex justify-between gap-3 py-1 text-[#FF3B30]" data-testid={`unaccepted-terminal-${t.account_id}`}>
                        <span>{t.label} · EA {t.ea_version} · {short(t.ex5_sha256)}</span><span>NOT ACCEPTED</span>
                    </div>
                ))}
                {d && !d.accepted?.length && <div className="text-[#52525B]">no accepted EX5 — ea-release has not signed a record</div>}
            </div>
        </div>
    );
}

// v1.60.43 — Canary Soak panel: terminals on the CURRENT (candidate) EA while the fleet's stable is the signed previous.
export function CanarySoakCard({ data, onRefresh }) {
    const c = data?.canary;
    const rows = c?.terminals || [];
    return (
        <div className={`border ${c && rows.length && !c.soak_ok ? "border-[#FF3B30]/50" : "border-[#1F1F1F]"} bg-[#0A0A0A]`} data-testid="canary-soak-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <span className="font-display font-bold text-sm">EA Canary Soak{c?.version ? ` · ${c.version}` : ""}</span>
                <span className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${!rows.length ? "border-[#52525B] text-[#52525B]" : c.soak_ok ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/50 text-[#FF3B30]"}`}
                    data-testid="canary-soak-verdict">
                    {!data ? "…" : !rows.length ? "NO CANARY TERMINAL" : c.soak_ok ? `${rows.length} SOAKING · 0 FAILURES` : "FAILURES"}
                </span>
                <button onClick={onRefresh} className="ml-auto text-[#52525B] hover:text-white" data-testid="canary-soak-refresh" aria-label="Refresh"><RefreshCw className="w-3 h-3" /></button>
            </div>
            <div className="px-4 py-2 font-mono text-[11px]">
                {rows.map((t) => (
                    <div key={t.account_id} className="py-1.5 border-b border-[#141414] last:border-0" data-testid={`canary-terminal-${t.account_id}`}>
                        <div className="flex justify-between gap-3"><span className="text-[#A1A1AA]">{t.label} · {t.broker_server}</span>
                            <span className="text-[#71717A]">heartbeat EA {t.ea_version} · {ago(t.heartbeat_age_sec)} ago · {t.attested || "unattested"}</span></div>
                        <div className="flex flex-wrap gap-x-4 text-[#71717A] mt-0.5">
                            <span>M1 payloads/h <b className="text-[#A1A1AA]">{t.m1_payloads_per_hour}</b> ({t.m1_payloads_24h}/24h)</span>
                            <span>TP-confirmed acks <b className="text-[#A1A1AA]">{t.tp_confirmed}</b></span>
                            <span>acks ok <b className="text-[#A1A1AA]">{t.acks_ok}</b></span>
                            <span className={t.acks_failed ? "text-[#FF3B30]" : ""}>failures <b>{t.acks_failed}</b>{t.last_cmd_error ? ` · ${t.last_cmd_error}` : ""}</span>
                        </div>
                    </div>
                ))}
                {data && !rows.length && <div className="text-[#52525B]">no terminal reports the candidate EA yet — install it on RoboForex Demo first (docs/EA_1_65_RELEASE.md)</div>}
            </div>
        </div>
    );
}

export function EaFleetCards() {
    const [d, setD] = useState(null);
    const load = useCallback(() => api.get("/ops/ea-fleet").then((r) => setD(r.data)).catch(() => setD({ accepted: [], unaccepted_enabled: [], canary: { terminals: [] }, error: true })), []);
    useEffect(() => { load(); const t = setInterval(load, 60000); return () => clearInterval(t); }, [load]);
    return (<><StableEx5Card data={d} /><CanarySoakCard data={d} onRefresh={load} /></>);
}
