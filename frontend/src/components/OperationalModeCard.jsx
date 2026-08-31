import { useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { matchFixShortcut } from "@/lib/fixShortcuts";
import { toast } from "sonner";
import { Eye, Ghost, FlaskConical, Users, Rocket, Shield, AlertOctagon, ArrowRight, X } from "lucide-react";

const MODES = [
    { key: "observe", label: "OBSERVE", icon: Eye, color: "#38BDF8",
      detail: "Detects and records opportunities — never creates trades (safe default)" },
    { key: "shadow", label: "SHADOW", icon: Ghost, color: "#A855F7",
      detail: "Records full simulated decisions from live data" },
    { key: "demo_autopilot", label: "DEMO AUTOPILOT", icon: FlaskConical, color: "#FFD700",
      detail: "Full system trades on demo accounts only" },
    { key: "supervised_live", label: "SUPERVISED LIVE", icon: Users, color: "#FF8C00",
      detail: "Real capital at half size while you're available" },
    { key: "autonomous_live", label: "AUTONOMOUS LIVE", icon: Rocket, color: "#00FF41",
      detail: "Approved strategies trade automatically — separate promotion, never a default" },
    { key: "defensive", label: "DEFENSIVE", icon: Shield, color: "#F97316",
      detail: "Only manages and reduces existing exposure" },
    { key: "panic", label: "PANIC", icon: AlertOctagon, color: "#FF3B30",
      detail: "New trades blocked — use the PANIC switch to flatten" },
];

export const OperationalModeCard = ({ cfg, setCfg, accountQuery }) => {
    const [saving, setSaving] = useState(false);
    const [blocked, setBlocked] = useState(null);
    const navigate = useNavigate();
    const current = cfg?.operational_mode || "observe";

    const pick = async (key) => {
        if (key === current || saving) return;
        setSaving(true); setBlocked(null);
        try {
            const { data } = await api.put(`/bot/config${accountQuery || ""}`,
                { operational_mode: key });
            setCfg(prev => ({ ...prev, operational_mode: data.operational_mode }));
            toast.success(`Operational mode: ${key.replace(/_/g, " ").toUpperCase()}`);
        } catch (e) {
            const detail = e?.response?.data?.detail;
            if (detail?.code === "mode_promotion_blocked") {
                setBlocked({
                    mode: key,
                    items: (detail.blockers || []).map(text => ({
                        text, fix: matchFixShortcut(text),
                    })),
                });
            } else toast.error(formatApiError(e));
        }
        finally { setSaving(false); }
    };

    const active = MODES.find(m => m.key === current) || MODES[0];

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
            <p className="text-xs text-[#A1A1AA] mb-3">{active.detail}. Downgrading is instant; promotions toward live trading require fresh 2FA and pass the broker-certification gate (audited).</p>
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
            {blocked && (
                <div className="mt-3 border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 space-y-2"
                    data-testid="mode-promotion-blockers-panel">
                    <div className="flex items-center justify-between gap-3">
                        <span className="font-mono text-[10px] tracking-widest text-[#FF3B30]">
                            PROMOTION TO {blocked.mode.replace(/_/g, " ").toUpperCase()} BLOCKED
                        </span>
                        <button onClick={() => setBlocked(null)} data-testid="mode-blockers-dismiss"
                            className="text-[#52525B] hover:text-white shrink-0"><X className="w-3.5 h-3.5" /></button>
                    </div>
                    {blocked.items.map((it, i) => (
                        <div key={i} className="flex items-center justify-between gap-3 border border-[#1F1F1F] bg-[#0A0A0A] px-3 py-2"
                            data-testid={`mode-blocker-${i}`}>
                            <span className="font-mono text-[11px] text-[#A1A1AA]">{it.text}</span>
                            {it.fix && (
                                <button onClick={() => navigate(it.fix.to)}
                                    data-testid={`mode-fix-shortcut-${i}`}
                                    className="shrink-0 flex items-center gap-1 font-mono text-[9px] font-bold tracking-widest px-2 py-1 border border-[#FFD700]/50 text-[#FFD700] hover:bg-[#FFD700]/10 transition-colors">
                                    {it.fix.label} <ArrowRight className="w-3 h-3" />
                                </button>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};
