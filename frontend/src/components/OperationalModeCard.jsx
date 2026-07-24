import { useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Eye, Ghost, FlaskConical, Users, Rocket, Shield, AlertOctagon } from "lucide-react";

const MODES = [
    { key: "observe", label: "OBSERVE", icon: Eye, color: "#38BDF8",
      detail: "Detects and records opportunities — never creates trades" },
    { key: "shadow", label: "SHADOW", icon: Ghost, color: "#A855F7",
      detail: "Records full simulated decisions from live data" },
    { key: "demo_autopilot", label: "DEMO AUTOPILOT", icon: FlaskConical, color: "#FFD700",
      detail: "Full system trades on demo accounts only" },
    { key: "supervised_live", label: "SUPERVISED LIVE", icon: Users, color: "#FF8C00",
      detail: "Real capital at half size while you're available" },
    { key: "autonomous_live", label: "AUTONOMOUS LIVE", icon: Rocket, color: "#00FF41",
      detail: "Approved strategies trade automatically (default)" },
    { key: "defensive", label: "DEFENSIVE", icon: Shield, color: "#F97316",
      detail: "Only manages and reduces existing exposure" },
    { key: "panic", label: "PANIC", icon: AlertOctagon, color: "#FF3B30",
      detail: "New trades blocked — use the PANIC switch to flatten" },
];

export const OperationalModeCard = ({ cfg, setCfg, accountQuery }) => {
    const [saving, setSaving] = useState(false);
    const current = cfg?.operational_mode || "autonomous_live";

    const pick = async (key) => {
        if (key === current || saving) return;
        setSaving(true);
        try {
            const { data } = await api.put(`/bot/config${accountQuery || ""}`,
                { operational_mode: key });
            setCfg(prev => ({ ...prev, operational_mode: data.operational_mode }));
            toast.success(`Operational mode: ${key.replace(/_/g, " ").toUpperCase()}`);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setSaving(false); }
    };

    const active = MODES.find(m => m.key === current) || MODES[4];

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="operational-mode-card">
            <div className="flex items-center gap-2 flex-wrap mb-1">
                <active.icon className="w-4 h-4" style={{ color: active.color }} />
                <span className="font-display font-bold text-sm">Operational Mode</span>
                <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border"
                    style={{ color: active.color, borderColor: `${active.color}66` }}
                    data-testid="operational-mode-current">
                    {active.label}
                </span>
            </div>
            <p className="text-xs text-[#A1A1AA] mb-3">{active.detail}. Downgrading is instant; upgrades toward live trading are the aggressive direction.</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
                {MODES.map(m => {
                    const Icon = m.icon;
                    const on = m.key === current;
                    return (
                        <button key={m.key} onClick={() => pick(m.key)} disabled={saving}
                            data-testid={`mode-${m.key}`}
                            title={m.detail}
                            className={`flex items-center gap-2 px-3 py-2 border text-left transition-colors disabled:opacity-60 ${
                                on ? "bg-black" : "border-[#1F1F1F] hover:border-[#333]"}`}
                            style={on ? { borderColor: m.color } : {}}>
                            <Icon className="w-3.5 h-3.5 shrink-0" style={{ color: m.color }} />
                            <span className={`font-mono text-[10px] tracking-widest ${on ? "text-white" : "text-[#A1A1AA]"}`}>
                                {m.label}
                            </span>
                        </button>
                    );
                })}
            </div>
        </div>
    );
};
