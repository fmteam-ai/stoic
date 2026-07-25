import { useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";

const STEPS = ["path", "provider", "broker", "recommend", "review"];

export function AddVpsWizard({ onDone }) {
    const [step, setStep] = useState(0);
    const [form, setForm] = useState({ path: "new_vps", provider: "simulated", broker: "", mt5_instances: 2, research_workload: false, apiKey: "" });
    const [providers, setProviders] = useState([]);
    const [brokers, setBrokers] = useState([]);
    const [rec, setRec] = useState(null);
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState(null);
    const [pathb, setPathb] = useState({ provider_name: "", label: "", region: "", os: "windows-server", mt5_installed: true });
    const [pathbResult, setPathbResult] = useState(null);

    const createExisting = async () => {
        setBusy(true);
        try {
            const { data } = await api.post("/infra/vps/connect-existing", pathb);
            setPathbResult(data);
            toast.success(`Deployment ${data.deployment_id} — waiting for agent`);
            onDone && onDone();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };

    useEffect(() => {
        api.get("/infra/providers").then(({ data }) => setProviders(data.providers || [])).catch(() => {});
        api.get("/infra/brokers/catalog").then(({ data }) => setBrokers(data.brokers || [])).catch(() => {});
    }, []);

    const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

    const fetchRecommendation = async () => {
        setBusy(true);
        try {
            const { data } = await api.post("/infra/recommend", {
                broker: form.broker, mt5_instances: form.mt5_instances,
                research_workload: form.research_workload,
            });
            setRec(data);
            setStep(3);
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };

    const connectProvider = async () => {
        if (!form.apiKey.trim()) return true;
        try {
            await api.post(`/infra/providers/${form.provider}/connect`, { api_key: form.apiKey.trim() });
            toast.success("Provider connected");
            return true;
        } catch (e) { toast.error(formatApiError(e)); return false; }
    };

    const createDeployment = async () => {
        setBusy(true);
        try {
            const key = `dep-${Date.now()}`;
            const { data } = await api.post("/infra/deployments", {
                path: form.path, provider: form.path === "existing_vps" ? "existing" : form.provider,
                region: rec?.latency?.recommended_region, plan: rec?.capacity?.plan,
                broker_profile: form.broker, mt5_instances: form.mt5_instances,
                issue_bootstrap: true,
            }, { headers: { "Idempotency-Key": key } });
            setResult(data);
            toast.success(`Deployment ${data.deployment_id} created (mode: shadow)`);
            onDone && onDone();
        } catch (e) { toast.error(formatApiError(e)); } finally { setBusy(false); }
    };

    const Btn = ({ onClick, children, disabled, testid, danger }) => (
        <button onClick={onClick} disabled={disabled || busy} data-testid={testid}
            className={`font-mono text-[10px] px-3 py-2 border tracking-widest transition-colors disabled:opacity-40 ${danger ? "text-[#FF3B30] border-[#FF3B30]/40" : "text-[#00FF41] border-[#00FF41]/40 hover:bg-[#00FF41]/10"}`}>
            {children}
        </button>
    );

    if (pathbResult) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="pathb-result">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">CONNECT EXISTING VPS · {pathbResult.deployment_id}</div>
                <div className="font-mono text-[11px] text-white mb-1">Enrollment code: <span className="text-[#00FF41]" data-testid="enrollment-code">{pathbResult.enrollment_code}</span> <span className="text-[#52525B]">(single use, {pathbResult.expires_in_min} min)</span></div>
                <div className="font-mono text-[9px] text-[#FFD700] mb-1">Run via RDP on the VPS as Administrator (recommended — downloads, verifies, then runs):</div>
                <pre className="font-mono text-[9px] text-[#A1A1AA] bg-black border border-[#141414] p-2 overflow-x-auto whitespace-pre-wrap" data-testid="pathb-command">
{(pathbResult.install_commands?.recommended || []).join("\n")}
                </pre>
                <div className="font-mono text-[8px] text-[#3F3F46] mt-1">Quick (less safe): {pathbResult.install_commands?.quick}</div>
                <div className="font-mono text-[9px] text-[#52525B] mt-2">Dashboard updates automatically: Waiting for Agent → Agent Connected → Inspecting Server → Ready for Setup</div>
                <div className="mt-3"><Btn onClick={() => { setPathbResult(null); setStep(0); }} testid="pathb-new">ADD ANOTHER</Btn></div>
            </div>
        );
    }

    if (result) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="wizard-result">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">DEPLOYMENT CREATED · {result.deployment_id}</div>
                <div className="font-mono text-[10px] text-white mb-2">State: {result.state} · Mode: {result.mode} (never above shadow at provision time)</div>
                {result.bootstrap && (
                    <div className="mt-2">
                        <div className="font-mono text-[9px] text-[#FFD700] mb-1">ONE-TIME BOOTSTRAP TOKEN ({result.bootstrap.expires_in_min} min, single use) — run on the VPS as Administrator:</div>
                        <pre className="font-mono text-[9px] text-[#A1A1AA] bg-black border border-[#141414] p-2 overflow-x-auto whitespace-pre-wrap" data-testid="bootstrap-command">
{`powershell -c "iwr '${process.env.REACT_APP_BACKEND_URL}/api/infra/agent/bootstrap/installer?token=${result.bootstrap.token}' -OutFile stoic-agent.ps1; ./stoic-agent.ps1"`}
                        </pre>
                    </div>
                )}
                <div className="mt-3"><Btn onClick={() => { setResult(null); setStep(0); setRec(null); }} testid="wizard-new">ADD ANOTHER</Btn></div>
            </div>
        );
    }

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="add-vps-wizard">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">
                ADD TRADING VPS · STEP {step + 1}/5 — {STEPS[step].toUpperCase()}
            </div>
            {step === 0 && (
                <div className="space-y-2">
                    {[["new_vps", "A. Deploy New Forex VPS", "STOIC provisions and configures the server automatically"],
                      ["existing_vps", "B. Connect Existing VPS", "install the STOIC Agent on your VPS with one command"]].map(([v, l, d]) => (
                        <button key={v} onClick={() => set("path", v)} data-testid={`path-${v}`}
                            className={`block w-full text-left border p-3 ${form.path === v ? "border-[#00FF41]/60" : "border-[#1F1F1F]"}`}>
                            <div className="font-mono text-[11px] text-white">{l}</div>
                            <div className="font-mono text-[9px] text-[#52525B]">{d}</div>
                        </button>
                    ))}
                    <Btn onClick={() => setStep(1)} testid="wizard-next-0">NEXT</Btn>
                </div>
            )}
            {step === 1 && form.path === "existing_vps" && (
                <div className="space-y-2" data-testid="pathb-form">
                    <div className="font-mono text-[9px] text-[#52525B]">Works with almost any Windows Forex VPS. No RDP password requested — you run one command yourself.</div>
                    {[["provider_name", "Provider name (e.g. ForexVPS, Contabo)"], ["label", "VPS label"], ["region", "Approximate region (e.g. London)"]].map(([k, ph]) => (
                        <input key={k} value={pathb[k]} onChange={(e) => setPathb((p) => ({ ...p, [k]: e.target.value }))} placeholder={ph}
                            data-testid={`pathb-${k}`}
                            className="w-full bg-transparent border border-[#1F1F1F] font-mono text-[10px] px-2 py-2 text-white placeholder:text-[#3F3F46]" />
                    ))}
                    <div className="flex items-center gap-3">
                        <select value={pathb.os} onChange={(e) => setPathb((p) => ({ ...p, os: e.target.value }))} data-testid="pathb-os"
                            className="bg-black border border-[#1F1F1F] font-mono text-[10px] px-2 py-2 text-white">
                            <option value="windows-server">Windows Server</option>
                            <option value="windows-10">Windows 10/11</option>
                        </select>
                        <label className="font-mono text-[9px] text-[#52525B] flex items-center gap-1">
                            <input type="checkbox" checked={pathb.mt5_installed} onChange={(e) => setPathb((p) => ({ ...p, mt5_installed: e.target.checked }))} data-testid="pathb-mt5-installed" />
                            MT5 already installed
                        </label>
                    </div>
                    <Btn onClick={createExisting} disabled={!pathb.label.trim()} testid="pathb-create">{busy ? "CREATING…" : "GENERATE INSTALL COMMAND"}</Btn>
                </div>
            )}
            {step === 1 && form.path === "new_vps" && (
                <div className="space-y-2">
                    {providers.map((p) => (
                        <button key={p.name} onClick={() => p.available && set("provider", p.name)} data-testid={`provider-${p.name}`}
                            className={`block w-full text-left border p-3 ${form.provider === p.name ? "border-[#00FF41]/60" : "border-[#1F1F1F]"} ${!p.available ? "opacity-50" : ""}`}>
                            <div className="font-mono text-[11px] text-white flex items-center gap-2">
                                {p.label}
                                {!p.available && <span className="font-mono text-[8px] px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700]">PARTNER INTEGRATION — CONTACT US</span>}
                                {p.connected && p.available && <span className="font-mono text-[8px] px-1.5 py-0.5 border border-[#00FF41]/40 text-[#00FF41]">CONNECTED</span>}
                            </div>
                            <div className="font-mono text-[9px] text-[#52525B]">{p.note}</div>
                        </button>
                    ))}
                    {form.provider === "vultr" && (
                        <input value={form.apiKey} onChange={(e) => set("apiKey", e.target.value)} placeholder="Vultr API key (stored encrypted in the secrets vault)"
                            data-testid="provider-api-key"
                            className="w-full bg-transparent border border-[#1F1F1F] font-mono text-[10px] px-2 py-2 text-white placeholder:text-[#3F3F46]" />
                    )}
                    <Btn onClick={async () => { if (await connectProvider()) setStep(2); }} testid="wizard-next-1">NEXT</Btn>
                </div>
            )}
            {step === 2 && (
                <div className="space-y-2">
                    <div className="font-mono text-[9px] text-[#52525B]">Broker first — the broker's trade servers decide the lowest-latency region.</div>
                    <select value={form.broker} onChange={(e) => set("broker", e.target.value)} data-testid="broker-select"
                        className="w-full bg-black border border-[#1F1F1F] font-mono text-[10px] px-2 py-2 text-white">
                        <option value="">— choose broker —</option>
                        {brokers.map((b) => <option key={b.broker} value={b.broker}>{b.broker} ({b.server})</option>)}
                    </select>
                    <div className="flex items-center gap-2">
                        <span className="font-mono text-[9px] text-[#52525B]">MT5 INSTANCES</span>
                        <input type="number" min="1" max="20" value={form.mt5_instances}
                            onChange={(e) => set("mt5_instances", parseInt(e.target.value || "1", 10))}
                            data-testid="mt5-count" className="w-16 bg-transparent border border-[#1F1F1F] font-mono text-[10px] px-2 py-1 text-white" />
                        <label className="font-mono text-[9px] text-[#52525B] flex items-center gap-1">
                            <input type="checkbox" checked={form.research_workload} onChange={(e) => set("research_workload", e.target.checked)} data-testid="research-toggle" />
                            research / backtesting workload
                        </label>
                    </div>
                    <Btn onClick={fetchRecommendation} disabled={!form.broker} testid="wizard-next-2">{busy ? "PROBING…" : "RECOMMEND REGION + CAPACITY"}</Btn>
                </div>
            )}
            {step === 3 && rec && (
                <div className="space-y-2">
                    <div className="font-mono text-[10px] text-white" data-testid="recommended-region">
                        RECOMMENDED REGION: <span className="text-[#00FF41]">{rec.latency.recommended_region?.toUpperCase()}</span>
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                        {rec.latency.region_estimates.map((r) => (
                            <span key={r.region} className="font-mono text-[8px] px-1.5 py-0.5 border border-[#27272A] text-[#A1A1AA]">{r.region}: ~{r.estimate_ms} ms</span>
                        ))}
                    </div>
                    <div className="font-mono text-[8px] text-[#3F3F46]">{rec.latency.disclaimer} · {rec.latency.at}{rec.latency.backend_probe ? ` · live probe ${rec.latency.backend_probe.median_ms}ms median (${rec.latency.backend_probe.samples} samples)` : ""}</div>
                    <div className="font-mono text-[10px] text-white" data-testid="recommended-plan">
                        CAPACITY: <span className="text-[#00FF41]">{rec.capacity.plan}</span> for {rec.capacity.mt5_instances} MT5 instance(s)
                    </div>
                    {rec.capacity.notes.map((n, i) => <div key={i} className="font-mono text-[9px] text-[#FFD700]">{n}</div>)}
                    <Btn onClick={() => setStep(4)} testid="wizard-next-3">NEXT</Btn>
                </div>
            )}
            {step === 4 && (
                <div className="space-y-2">
                    <div className="font-mono text-[9px] text-[#A1A1AA] space-y-1">
                        <div>PATH: {form.path === "new_vps" ? "Deploy new VPS" : "Connect existing VPS"}</div>
                        {form.path === "new_vps" && <div>PROVIDER: {form.provider}</div>}
                        <div>BROKER: {form.broker || "—"} · MT5 × {form.mt5_instances}</div>
                        <div>REGION: {rec?.latency?.recommended_region || "—"} · PLAN: {rec?.capacity?.plan || "—"}</div>
                        <div className="text-[#FFD700]">INITIAL MODE: SHADOW — promotion goes demo → supervised → autonomous via the certification gates</div>
                    </div>
                    <Btn onClick={createDeployment} testid="wizard-create">{busy ? "CREATING…" : "CREATE DEPLOYMENT"}</Btn>
                </div>
            )}
            {step > 0 && !result && (
                <button onClick={() => setStep(step - 1)} data-testid="wizard-back"
                    className="mt-3 font-mono text-[9px] text-[#52525B] underline">back</button>
            )}
        </div>
    );
}
