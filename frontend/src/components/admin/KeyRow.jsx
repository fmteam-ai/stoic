import { Lock } from "lucide-react";

/** One vault key on Admin → Integrations: name · SET/MISSING · source · masked value · SET/UPDATE button. */
export function KeyRow({ name, k, onEdit }) {
    return (
        <div data-testid={`integration-key-${name}`}
            className="flex items-center justify-between gap-3 py-2 border-b border-[#1F1F1F] last:border-0">
            <div className="min-w-0">
                <div className="flex items-center gap-2">
                    <span className="font-mono text-xs text-[#E4E4E7]">{name}</span>
                    {k.secret && <Lock className="w-3 h-3 text-[#52525B]" />}
                    <span className={`text-[10px] font-mono tracking-widest px-1.5 py-0.5 border ${
                        k.configured ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}
                        data-testid={`integration-key-${name}-status`}>
                        {k.configured ? `SET · ${k.source.toUpperCase()}` : "MISSING"}
                    </span>
                </div>
                <div className="text-[11px] text-[#71717A] truncate">{k.description}</div>
                {k.configured && <div className="text-[11px] font-mono text-[#A1A1AA]">{k.display}
                    {k.updated_at && <span className="text-[#52525B]"> · updated {k.updated_at.slice(0, 16).replace("T", " ")} by {k.updated_by}</span>}</div>}
            </div>
            <button onClick={() => onEdit(name)} data-testid={`integration-key-${name}-edit`}
                className="shrink-0 px-2.5 py-1 text-[10px] font-mono tracking-widest border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00FF41]/50 hover:text-[#00FF41]">
                {k.configured ? "UPDATE" : "SET"}
            </button>
        </div>
    );
}

export default KeyRow;
