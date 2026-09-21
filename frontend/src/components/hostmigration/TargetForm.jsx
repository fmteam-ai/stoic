import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Copy, Loader2, KeyRound, Radar, CheckCircle2, XCircle } from "lucide-react";
import { fmtBytes } from "./Shared";

const CHECK_LABEL = {
    ssh: "SSH login with the migrator key", docker: "Docker installed", compose_v2: "Docker Compose v2",
    docker_access: "Deploy user may run docker", rsync: "rsync present", path_empty: "Install path empty",
    disk: "Enough free disk (3× DB + backups + 6 GB)", ports_free: "Ports 80/443/8001/3000/27017 free",
    memory: "Enough RAM (16 GB with forecast profile, else 4 GB)", release_tag: "Source is on a v* release tag",
};

const Field = ({ label, testid, ...p }) => (
    <label className="block">
        <span className="text-[10px] font-mono tracking-widest text-[#52525B]">{label}</span>
        <input {...p} data-testid={testid}
            className="mt-1 w-full bg-[#050505] border border-[#1F1F1F] px-3 py-2 text-sm font-mono text-[#E4E4E7] focus:border-[#00FF41]/50 outline-none" />
    </label>
);

export function TargetForm({ state, onPreflight }) {
    const [pub, setPub] = useState("");
    const [f, setF] = useState({ host: state?.target?.host || "", user: state?.target?.user || "stoic",
        port: state?.target?.port || 22, path: state?.target?.path || "/home/stoic/stoic" });
    const [busy, setBusy] = useState(false);
    const facts = state?.facts;
    const busyState = state?.status === "running";

    useEffect(() => { api.get("/admin/host-migration/public-key").then(r => setPub(r.data.public_key)).catch(() => {}); }, []);

    const run = async () => {
        setBusy(true);
        try {
            await api.post("/admin/host-migration/preflight", { ...f, port: Number(f.port) });
            toast.success("Preflight finished");
            onPreflight();
        } catch (e) { toast.error(formatApiError(e)); onPreflight(); }
        finally { setBusy(false); }
    };
    const copy = () => { navigator.clipboard?.writeText(pub); toast.success("Public key copied"); };

    return (
        <div className="grid lg:grid-cols-2 gap-5" data-testid="hm-target-form">
            <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-5 space-y-4">
                <div className="flex items-center gap-2"><KeyRound className="w-3.5 h-3.5 text-[#00FF41]" />
                    <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">1 · AUTHORIZE THE MIGRATOR ON THE NEW HOST</span></div>
                <p className="text-xs text-[#71717A]">On the new server, create the deploy user (in the docker group) and append this public key to its
                    <code className="text-[#A1A1AA]"> ~/.ssh/authorized_keys</code>. The private key never leaves the sidecar.</p>
                <div className="relative">
                    <pre className="bg-[#050505] border border-[#1F1F1F] p-3 text-[11px] font-mono text-[#00FF41] break-all whitespace-pre-wrap" data-testid="hm-public-key">{pub || "generating…"}</pre>
                    <button onClick={copy} data-testid="hm-copy-key" className="absolute top-2 right-2 text-[#71717A] hover:text-[#00FF41]"><Copy className="w-3.5 h-3.5" /></button>
                </div>
                <pre className="bg-[#050505] border border-[#1F1F1F] p-3 text-[11px] font-mono text-[#A1A1AA] overflow-auto">{`# on the NEW host (as root)
useradd -m ${f.user || "stoic"} && usermod -aG docker ${f.user || "stoic"}
mkdir -p /home/${f.user || "stoic"}/.ssh && echo '<public key>' >> /home/${f.user || "stoic"}/.ssh/authorized_keys
chown -R ${f.user || "stoic"}: /home/${f.user || "stoic"}/.ssh && chmod 700 /home/${f.user || "stoic"}/.ssh
curl -fsSL https://get.docker.com | sh && dnf install -y rsync   # if docker/rsync are missing`}</pre>
                <div className="flex items-center gap-2 pt-2"><Radar className="w-3.5 h-3.5 text-[#00FF41]" />
                    <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">2 · NEW HOST</span></div>
                <div className="grid grid-cols-2 gap-3">
                    <Field label="HOST / IP" testid="hm-host" value={f.host} onChange={e => setF({ ...f, host: e.target.value })} placeholder="203.0.113.10" />
                    <Field label="SSH PORT" testid="hm-port" value={f.port} onChange={e => setF({ ...f, port: e.target.value })} />
                    <Field label="SSH USER" testid="hm-user" value={f.user} onChange={e => setF({ ...f, user: e.target.value })} />
                    <Field label="INSTALL PATH" testid="hm-path" value={f.path} onChange={e => setF({ ...f, path: e.target.value })} />
                </div>
                <button onClick={run} disabled={busy || busyState || !f.host} data-testid="hm-preflight-btn"
                    className="w-full py-2.5 text-xs font-mono tracking-widest border border-[#00FF41]/50 text-[#00FF41] hover:bg-[#00FF41]/10 disabled:opacity-40 flex items-center justify-center gap-2">
                    {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Radar className="w-3.5 h-3.5" />} {busy ? "CHECKING NEW HOST…" : "RUN PREFLIGHT"}
                </button>
            </div>
            <PreflightResult facts={facts} error={state?.status === "failed" ? state?.error : null} />
        </div>
    );
}

function PreflightResult({ facts, error }) {
    const checks = facts?.target?.checks;
    return (
        <div className="bg-[#0A0A0A] border border-[#1F1F1F] p-5" data-testid="hm-preflight-result">
            <div className="font-mono text-[10px] tracking-widest text-[#A1A1AA] mb-3">PREFLIGHT RESULT</div>
            {!checks && !error && <div className="text-xs text-[#52525B]">Run the preflight to see what the new host is missing.</div>}
            {error && <div className="text-xs text-[#FF3B30] font-mono mb-3 break-all" data-testid="hm-preflight-error">{error}</div>}
            {checks && (
                <div className="space-y-1.5">
                    {Object.entries(checks).map(([k, ok]) => (
                        <div key={k} className="flex items-center gap-2 text-xs" data-testid={`hm-check-${k}`}>
                            {ok ? <CheckCircle2 className="w-3.5 h-3.5 text-[#00FF41]" /> : <XCircle className="w-3.5 h-3.5 text-[#FF3B30]" />}
                            <span className={ok ? "text-[#A1A1AA]" : "text-[#E4E4E7]"}>{CHECK_LABEL[k] || k}</span>
                        </div>
                    ))}
                    <div className="grid grid-cols-2 gap-2 pt-3 text-[11px] font-mono text-[#71717A]">
                        <span>host key <span className="text-[#A1A1AA] break-all">{facts.host_key?.fingerprints?.[0]}</span></span>
                        <span>target OS <span className="text-[#A1A1AA]">{facts.target?.OS || "?"}</span></span>
                        <span>free disk <span className="text-[#A1A1AA]">{fmtBytes(Number(facts.target?.DISK_AVAIL))} / need {fmtBytes(facts.target?.disk_needed)}</span></span>
                        <span>public IP <span className="text-[#A1A1AA]">{facts.target?.public_ip || "?"}</span></span>
                        <span>source release <span className="text-[#A1A1AA]">{facts.source?.tag || facts.source?.sha?.slice(0, 12)}</span></span>
                        <span>source DB <span className="text-[#A1A1AA]">{fmtBytes(facts.source?.db_bytes)} · backups {fmtBytes(facts.source?.backups_bytes)}</span></span>
                        <span>profile <span className="text-[#A1A1AA]">{facts.source?.tls ? `production · ${facts.source.domain}` : "dev"}{facts.source?.forecast ? " · forecast" : ""}{facts.source?.registry ? " · registry" : ""}</span></span>
                        <span>connected accounts <span className="text-[#A1A1AA]">{facts.source?.connected_accounts ?? "?"}</span></span>
                    </div>
                </div>
            )}
        </div>
    );
}
