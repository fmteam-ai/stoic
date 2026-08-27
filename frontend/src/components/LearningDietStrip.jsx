import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Filter } from "lucide-react";

export function LearningDietStrip() {
    const [q, setQ] = useState(null);
    useEffect(() => {
        api.get("/learning/quality")
            .then(r => setQ(r.data?.quality || null))
            .catch(() => { });
    }, []);
    if (!q || q.total_closed == null) return null;
    const byCat = Object.entries(q.excluded_by_category || {})
        .sort((a, b) => b[1] - a[1]);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-2"
            data-testid="learning-diet-strip">
            <div className="flex items-center gap-2">
                <Filter className="w-4 h-4 text-[#00FF41]" />
                <div className="font-display font-bold text-sm">AI Learning Diet</div>
                <span className="font-mono text-[10px] text-[#52525B]">broker noise never retrains the strategy</span>
            </div>
            <div className="font-mono text-[10px] text-[#A1A1AA]">
                FED TO MODEL <span className="text-[#00FF41]" data-testid="diet-accepted">{q.accepted ?? 0}</span>
                <span className="text-[#52525B]"> ({q.alpha_clean_accepted ?? 0} alpha-clean, {q.unattributed_included ?? 0} legacy)</span>
            </div>
            <div className="font-mono text-[10px] text-[#A1A1AA]">
                FILTERED OUT <span className="text-[#FF3B30]" data-testid="diet-filtered">{q.excluded_noise ?? 0}</span>
            </div>
            {byCat.length > 0 && (
                <div className="font-mono text-[9px] text-[#52525B]" data-testid="diet-categories">
                    {byCat.slice(0, 4).map(([c, n]) => `${c.replace(/_/g, " ")} ×${n}`).join(" · ")}
                </div>
            )}
        </div>
    );
}
