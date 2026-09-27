import { AlertTriangle, ShieldAlert } from "lucide-react";

const money = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}`);

function EffectDetail({ item }) {
    if (item.bots?.length) {
        return (
            <ul className="mt-1 space-y-0.5">
                {item.bots.slice(0, 8).map(b => (
                    <li key={b.id} className="font-mono text-[10px] text-[#A1A1AA] flex gap-2">
                        <span className="text-[#FAFAFA]">{b.account}</span>
                        <span>· {b.preset || "custom"}</span>
                        <span>· risk {String(b.risk_level || "?").toUpperCase()}{b.to ? ` → ${b.to.toUpperCase()}` : ""}</span>
                        <span className={b.active ? "text-[#00FF41]" : "text-[#52525B]"}>· {b.active ? "ACTIVE" : "OFF"}</span>
                    </li>
                ))}
                {item.bots.length > 8 && <li className="font-mono text-[10px] text-[#52525B]">+{item.bots.length - 8} more</li>}
            </ul>
        );
    }
    if (item.trades?.length) {
        return (
            <ul className="mt-1 space-y-0.5">
                {item.trades.slice(0, 8).map(t => (
                    <li key={t.id} className="font-mono text-[10px] text-[#A1A1AA] flex gap-2">
                        <span className="text-[#FAFAFA]">{t.symbol}</span>
                        <span>· {t.side}</span>
                        <span>· {t.lots} lots @ {t.entry}</span>
                        <span className={(t.live_pnl || 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}>· {money(t.live_pnl)}</span>
                    </li>
                ))}
                {item.trades.length > 8 && <li className="font-mono text-[10px] text-[#52525B]">+{item.trades.length - 8} more</li>}
            </ul>
        );
    }
    if (item.trigger) {
        return (
            <div className="mt-1 font-mono text-[10px] text-[#A1A1AA]">
                fires when <span className="text-[#FAFAFA]">{item.trigger.symbol}</span> {item.trigger.condition} {item.trigger.threshold_pct}% · then {item.trigger.then.join(", ") || "nothing"} (auto, no further prompt)
            </div>
        );
    }
    return null;
}

export const ProposalPreview = ({ preview, msgIndex }) => {
    if (!preview) return null;
    return (
        <div className="space-y-2" data-testid={`proposal-preview-${msgIndex}`}>
            <div className="flex items-center gap-2 font-mono text-[10px] tracking-widest">
                {preview.capital_touching
                    ? <span className="flex items-center gap-1 text-[#FF3B30]" data-testid={`preview-capital-flag-${msgIndex}`}><ShieldAlert className="w-3 h-3" /> TOUCHES CAPITAL</span>
                    : <span className="flex items-center gap-1 text-[#FFB000]"><AlertTriangle className="w-3 h-3" /> CHANGES BOT STATE</span>}
                <span className="text-[#52525B]">· {preview.total_effects} effect(s)</span>
                <span className="text-[#52525B]" title="Preview fingerprint — confirm is refused if the portfolio changes">· #{String(preview.fingerprint || "").slice(0, 8)}</span>
            </div>
            {preview.actions.map((a, j) => (
                <div key={j} className="border border-[#1F1F1F] bg-[#050505] p-2" data-testid={`preview-action-${msgIndex}-${j}`}>
                    <div className="font-mono text-[10px] tracking-widest text-[#FFB000]">{a.type}{a.target && a.target !== "all" ? ` · ${a.target}` : ""}</div>
                    <div className="text-xs text-[#FAFAFA] mt-0.5">{a.effect}</div>
                    {a.count === 0 && <div className="font-mono text-[10px] text-[#52525B] mt-0.5">Nothing matches right now — executing would be a no-op.</div>}
                    <EffectDetail item={a} />
                </div>
            ))}
        </div>
    );
};
