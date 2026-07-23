import { useEffect, useState } from "react";
import axios from "axios";
import { ChevronDown, ChevronUp, Workflow } from "lucide-react";

import { BACKEND_URL } from "@/lib/api";

const API = BACKEND_URL;

const Dot = ({ status }) => (
    <span className="inline-block w-1.5 h-1.5 rounded-full mr-2 align-middle"
          style={{ backgroundColor: status === "ok" ? "#00FF41" : "#52525B",
                   boxShadow: status === "ok" ? "0 0 6px #00FF41" : "none" }} />
);

const Node = ({ s, last }) => (
    <div className="relative pl-5" data-testid={`arch-stage-${s.key}`}>
        {!last && <span className="absolute left-[7px] top-5 bottom-[-6px] w-px bg-[#27272A]" />}
        <span className="absolute left-1 top-1.5"><Dot status={s.status} /></span>
        <div className="font-mono text-[12px] text-[#E4E4E7] tracking-wide">{s.label}</div>
        <div className="font-mono text-[10px] text-[#71717A] leading-relaxed">{s.detail}</div>
        {(s.children || []).length > 0 && (
            <div className="flex flex-wrap gap-x-6 gap-y-1 mt-1 mb-1">
                {s.children.map(c => (
                    <div key={c.key} className="font-mono text-[10px]" data-testid={`arch-stage-${c.key}`}>
                        <Dot status={c.status} />
                        <span className="text-[#A1A1AA]">{c.label}</span>
                        <span className="text-[#52525B]"> — {c.detail}</span>
                    </div>
                ))}
            </div>
        )}
    </div>
);

export default function ArchitecturePipeline() {
    const [data, setData] = useState(null);
    const [open, setOpen] = useState(false);

    useEffect(() => {
        if (!open || data) return;
        axios.get(`${API}/api/architecture`, { withCredentials: true })
            .then(r => setData(r.data)).catch(() => {});
    }, [open, data]);

    return (
        <div className="border border-[#27272A] bg-[#0C0C0E]" data-testid="architecture-pipeline-card">
            <button onClick={() => setOpen(o => !o)} data-testid="architecture-toggle"
                    className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-[#131316] transition-colors">
                <Workflow className="w-4 h-4 text-[#00FF41]" />
                <span className="font-mono text-xs tracking-[0.2em] text-[#E4E4E7]">SYSTEM ARCHITECTURE</span>
                <span className="font-mono text-[10px] text-[#52525B]">market data → models → ensemble → risk → broker (live)</span>
                {open ? <ChevronUp className="w-4 h-4 ml-auto text-[#52525B]" /> : <ChevronDown className="w-4 h-4 ml-auto text-[#52525B]" />}
            </button>
            {open && (
                <div className="px-4 pb-4 space-y-2.5">
                    {data
                        ? data.stages.map((s, i) => <Node key={s.key} s={s} last={i === data.stages.length - 1} />)
                        : <div className="font-mono text-[10px] text-[#52525B]">loading pipeline…</div>}
                </div>
            )}
        </div>
    );
}
