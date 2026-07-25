import { useEffect, useState } from "react";
import api from "@/lib/api";
import { GraduationCap, ChevronDown, ChevronUp } from "lucide-react";

export default function CoachPanel() {
    const [c, setC] = useState(null);
    const [open, setOpen] = useState(false);
    useEffect(() => {
        api.get("/coach/cards").then(({ data }) => setC(data)).catch(() => {});
    }, []);
    if (!c) return null;
    const hasContent = (c.waiting?.length || c.rejected?.length || c.risk || c.lesson);
    if (!hasContent) return null;
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="coach-panel">
            <button onClick={() => setOpen(o => !o)} data-testid="coach-toggle"
                className="w-full flex items-center gap-2 px-4 py-3 text-left">
                <GraduationCap size={14} className="text-[#A78BFA]" />
                <span className="font-mono text-[10px] text-[#A1A1AA] tracking-widest">AI COACH · WHY THE BOT IS DOING WHAT IT'S DOING</span>
                {c.confidence && (
                    <span className="hidden sm:inline font-mono text-[9px] text-[#52525B] ml-2">
                        says {c.confidence.predicted}% → wins {c.confidence.actual}%
                    </span>
                )}
                <span className="ml-auto text-[#52525B]">{open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}</span>
            </button>
            {open && (
                <div className="px-4 pb-4 space-y-4">
                    {c.waiting?.length > 0 && (
                        <div data-testid="coach-waiting">
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">WHY IT'S WAITING RIGHT NOW</div>
                            {c.waiting.map((w, i) => (
                                <div key={i} className="font-mono text-[10px] text-[#A1A1AA] py-1 border-t border-[#141414]">
                                    <span className="text-[#0099FF]">{w.account}</span>
                                    {w.symbol && <span className="text-[#52525B]"> · {w.symbol}</span>}
                                    <span className="text-[#71717A]"> — {w.reason}</span>
                                </div>
                            ))}
                        </div>
                    )}
                    {c.rejected?.length > 0 && (
                        <div data-testid="coach-rejected">
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">SETUPS REJECTED IN THE LAST 24H — AND WHY THAT'S GOOD</div>
                            {c.rejected.map((r, i) => (
                                <div key={i} className="py-1 border-t border-[#141414]" data-testid={`coach-rejected-${r.stage}`}>
                                    <div className="font-mono text-[10px]">
                                        <span className="text-[#FFD700]">{r.stage}</span>
                                        <span className="text-[#52525B]"> · {r.count}×</span>
                                        {r.symbol && <span className="text-[#52525B]"> · {r.symbol}</span>}
                                    </div>
                                    <div className="font-mono text-[9px] text-[#71717A]">{r.coach}</div>
                                </div>
                            ))}
                        </div>
                    )}
                    {c.risk && (
                        <div data-testid="coach-risk">
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">WHY RISK CHANGED</div>
                            <div className="font-mono text-[10px] text-[#A1A1AA]">
                                {c.risk.symbol} risk set to <span className="text-white">{c.risk.risk_pct}%</span>
                                {c.risk.multiplier != null && <span className="text-[#52525B]"> ({c.risk.multiplier}× base)</span>}
                            </div>
                            {c.risk.drivers?.map((d, i) => (
                                <div key={i} className="font-mono text-[9px] text-[#71717A]">· {d}</div>
                            ))}
                            <div className="font-mono text-[9px] text-[#3F3F46] mt-1">{c.risk.coach}</div>
                        </div>
                    )}
                    {c.lesson && (
                        <div data-testid="coach-lesson">
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1.5">LATEST LESSON THE BOT LEARNED</div>
                            <div className="font-mono text-[10px] text-[#A78BFA]">"{c.lesson.lesson}"</div>
                            {c.lesson.top_mistakes?.length > 0 && (
                                <div className="flex flex-wrap gap-2 mt-1.5">
                                    {c.lesson.top_mistakes.map((m) => (
                                        <span key={m.mistake} className="font-mono text-[9px] px-1.5 py-0.5 border border-[#141414] text-[#71717A]">
                                            {m.mistake} ×{m.count}
                                        </span>
                                    ))}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
