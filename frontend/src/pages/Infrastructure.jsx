import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { AddVpsWizard } from "../components/InfraWizard";
import { AgentHealthCard, DiscoveredTerminals, FailureMatrixCard, PathBStatusLadder } from "../components/PathBPanels";

const SECTIONS = ["VPS Servers", "MT5 Instances", "Deployment Jobs", "Health Monitoring", "Backups"];
const STATE_CLS = {
    READY: "text-[#00FF41] border-[#00FF41]/40",
    FAILED: "text-[#FF3B30] border-[#FF3B30]/40",
};

export default function Infrastructure() {
    const [deployments, setDeployments] = useState([]);
    const [overview, setOverview] = useState(null);
    const [cert, setCert] = useState(null);
    const [showWizard, setShowWizard] = useState(false);

    const load = useCallback(() => {
        api.get("/infra/deployments").then(({ data }) => setDeployments(data.deployments || [])).catch(() => {});
        api.get("/infra/overview").then(({ data }) => setOverview(data)).catch(() => {});
        api.get("/infra/certification").then(({ data }) => setCert(data)).catch(() => {});
    }, []);
    useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

    return (
        <div className="space-y-6" data-testid="infrastructure-page">
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div>
                    <h1 className="font-mono text-xl text-white tracking-widest">INFRASTRUCTURE</h1>
                    <div className="font-mono text-[9px] text-[#52525B] mt-1">{SECTIONS.join(" · ")}</div>
                </div>
                <button onClick={() => setShowWizard((s) => !s)} data-testid="add-trading-vps-btn"
                    className="font-mono text-[10px] px-3 py-2 border border-[#00FF41]/40 text-[#00FF41] tracking-widest hover:bg-[#00FF41]/10">
                    {showWizard ? "CLOSE" : "+ ADD TRADING VPS"}
                </button>
            </div>

            {showWizard && <AddVpsWizard onDone={load} />}

            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="deployment-jobs">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">DEPLOYMENT JOBS</div>
                {deployments.length === 0 && <div className="font-mono text-[9px] text-[#3F3F46]">No deployments yet — add a trading VPS to begin.</div>}
                <div className="space-y-2">
                    {deployments.map((d) => (
                        <div key={d.deployment_id} className="border border-[#141414] p-2" data-testid={`deployment-${d.deployment_id}`}>
                            <div className="flex items-center gap-2 flex-wrap">
                                <span className="font-mono text-[10px] text-white">{d.meta?.label ? `${d.meta.label} · ` : ""}{d.deployment_id}</span>
                                <span className={`font-mono text-[8px] px-1.5 py-0.5 border ${STATE_CLS[d.state] || "text-[#FFD700] border-[#FFD700]/40"}`}>{d.state}</span>
                                <span className="font-mono text-[8px] text-[#52525B]">{d.provider} · {d.region || "—"} · {d.plan || "—"} · mode {d.mode}</span>
                                {d.server?.ip && <span className="font-mono text-[8px] text-[#A1A1AA]">ip {d.server.ip}</span>}
                            </div>
                            <div className="flex flex-wrap gap-1 mt-1">
                                {(d.state_history || []).map((s, i) => (
                                    <span key={i} className="font-mono text-[7px] px-1 py-0.5 border border-[#1F1F1F] text-[#52525B]">{s.state}</span>
                                ))}
                            </div>
                            {d.path === "existing_vps" && (
                                <>
                                    <PathBStatusLadder deploymentId={d.deployment_id} />
                                    <DiscoveredTerminals deploymentId={d.deployment_id} />
                                </>
                            )}
                        </div>
                    ))}
                </div>
            </div>

            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="vps-servers">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">VPS SERVERS · STOIC AGENTS (infrastructure truth — separate from trading truth)</div>
                {(overview?.agents || []).length === 0 && <div className="font-mono text-[9px] text-[#3F3F46]">No agents registered.</div>}
                {(overview?.agents || []).map((a) => (
                    <div key={a.agent_id} className="border border-[#141414] p-2 mb-2" data-testid={`agent-${a.agent_id}`}>
                        <div className="font-mono text-[10px] text-white">{a.agent_id} <span className="text-[#52525B]">· {a.deployment_id}</span>
                            <span className={`ml-2 font-mono text-[8px] ${a.heartbeat_age_sec != null && a.heartbeat_age_sec < 180 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>
                                hb {a.heartbeat_age_sec == null ? "never" : `${a.heartbeat_age_sec}s ago`}
                            </span>
                        </div>
                        {a.metrics && (
                            <div className="font-mono text-[8px] text-[#A1A1AA] mt-1">
                                cpu {a.metrics.cpu_percent}% · ram {a.metrics.ram_percent}% · disk {a.metrics.disk_free_gb}GB free · clock ±{a.metrics.clock_offset_ms}ms · MT5 ×{a.metrics.mt5_processes}
                            </div>
                        )}
                        {a.hardening_missing?.length > 0 && (
                            <div className="font-mono text-[8px] text-[#FFD700] mt-1">hardening pending: {a.hardening_missing.join(", ")}</div>
                        )}
                        <AgentHealthCard agentId={a.agent_id} />
                    </div>
                ))}
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="mt5-instances">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">MT5 INSTANCES (one per account, isolated directories)</div>
                    {(overview?.mt5_instances || []).length === 0 && <div className="font-mono text-[9px] text-[#3F3F46]">None registered.</div>}
                    {(overview?.mt5_instances || []).map((i) => (
                        <div key={i.account_ref} className="font-mono text-[9px] text-[#A1A1AA] border-t border-[#141414] py-1">
                            acct {i.account_ref} · {i.directory} · {i.status} · EA {i.ea_version || "—"}
                        </div>
                    ))}
                </div>
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="vps-backups">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">BACKUPS</div>
                    {(overview?.backups || []).length === 0 && <div className="font-mono text-[9px] text-[#3F3F46]">No backups yet.</div>}
                    {(overview?.backups || []).map((b, i) => (
                        <div key={i} className="font-mono text-[9px] text-[#A1A1AA] border-t border-[#141414] py-1">{b.backup_id} · {b.deployment_id} · {b.at}</div>
                    ))}
                </div>
            </div>

            {cert && (
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="shadow-certification">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">
                        SHADOW CERTIFICATION · {cert.passed}/{cert.total} {cert.certified ? "— CERTIFIED" : "— NOT CERTIFIED"}
                    </div>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                        {Object.entries(cert.checks).map(([k, v]) => (
                            <div key={k} className="border border-[#141414] p-2" data-testid={`cert-${k}`}>
                                <div className="font-mono text-[8px] text-[#52525B] uppercase">{k.replace(/_/g, " ")}</div>
                                <div className={`font-mono text-xs ${v ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{v ? "PASS" : "FAIL"}</div>
                            </div>
                        ))}
                    </div>
                    <div className="font-mono text-[8px] text-[#3F3F46] mt-2">{cert.note}</div>
                </div>
            )}

            <FailureMatrixCard />
        </div>
    );
}
