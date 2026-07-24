import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Gauge, ShieldCheck, ChevronDown, ChevronUp } from "lucide-react";

const TIER_COLOR = { fast: "#00FF41", medium: "#FFD700", slow: "#38BDF8" };

export const LearningSpeedsCard = () => {
    const [speeds, setSpeeds] = useState(null);
    const [inv, setInv] = useState(null);
    const [showInv, setShowInv] = useState(false);
    const [hidden, setHidden] = useState(false);

    useEffect(() => {
        let dead = false;
        Promise.all([
            api.get("/learning/speeds"),
            api.get("/learning/safety-invariants"),
        ]).then(([s, i]) => { if (!dead) { setSpeeds(s.data); setInv(i.data); } })
            .catch(() => { if (!dead) setHidden(true); });
        return () => { dead = true; };
    }, []);

    if (hidden || !speeds) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="learning-speeds-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Gauge className="w-4 h-4 text-[#00FF41]" />
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">CONTINUOUS LEARNING</div>
                    <div className="font-display font-bold text-base tracking-tight">Three learning speeds, all bounded</div>
                </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-[#1F1F1F]">
                {speeds.speeds.map(s => (
                    <div key={s.tier} className="p-4" data-testid={`learning-speed-${s.tier}`}>
                        <div className="flex items-center gap-2 mb-1.5">
                            <span className="font-mono text-[10px] tracking-widest font-bold"
                                style={{ color: TIER_COLOR[s.tier] }}>
                                {s.tier.toUpperCase()}
                            </span>
                            <span className="font-mono text-[9px] text-[#52525B] tracking-widest">{s.horizon.toUpperCase()}</span>
                        </div>
                        <ul className="text-[11px] text-[#A1A1AA] space-y-0.5 mb-2">
                            {s.levers.map(l => <li key={l}>· {l}</li>)}
                        </ul>
                        <div className="font-mono text-[9px] text-[#52525B] leading-relaxed">{s.bounded_by.toUpperCase()}</div>
                        <div className="font-mono text-[9px] text-[#71717A] mt-2 space-y-0.5">
                            {Object.entries(s.live || {}).map(([k, v]) => (
                                <div key={k}>{k.replace(/_/g, " ").toUpperCase()}: <span className="text-white">{String(v ?? "—")}</span></div>
                            ))}
                        </div>
                    </div>
                ))}
            </div>
            {inv && (
                <div className="border-t border-[#1F1F1F]">
                    <button onClick={() => setShowInv(v => !v)} data-testid="safety-invariants-toggle"
                        className="w-full px-4 py-2.5 flex items-center gap-2 hover:bg-black/40 transition-colors">
                        <ShieldCheck className="w-3.5 h-3.5 text-[#00FF41]" />
                        <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">
                            {inv.invariants.length} DANGEROUS SELF-LEARNING BEHAVIORS — ALL PREVENTED
                        </span>
                        {showInv ? <ChevronUp className="w-3.5 h-3.5 text-[#52525B] ml-auto" />
                            : <ChevronDown className="w-3.5 h-3.5 text-[#52525B] ml-auto" />}
                    </button>
                    {showInv && (
                        <div className="px-4 pb-3 space-y-1.5" data-testid="safety-invariants-list">
                            {inv.invariants.map(r => (
                                <div key={r.behavior} className="flex items-start gap-2">
                                    <span className="font-mono text-[9px] tracking-widest text-[#FF3B30] w-56 shrink-0 line-through">
                                        {r.behavior.toUpperCase()}
                                    </span>
                                    <span className="text-[10px] text-[#71717A] leading-relaxed">{r.prevented_by}</span>
                                </div>
                            ))}
                            <div className="font-mono text-[9px] tracking-widest text-[#52525B] pt-1">
                                {inv.principle?.toUpperCase()}
                            </div>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
};
