import { useEffect, useState } from "react";
import api from "@/lib/api";
import { Scale, CheckCircle2, XCircle } from "lucide-react";

function Flag({ ok, yes = "YES", no = "NO" }) {
    return ok
        ? <span className="inline-flex items-center gap-1 text-[#00FF41]"><CheckCircle2 className="w-3 h-3" />{yes}</span>
        : <span className="inline-flex items-center gap-1 text-[#FFB000]"><XCircle className="w-3 h-3" />{no}</span>;
}

export function BrokerMatrix() {
    const [d, setD] = useState(null);

    useEffect(() => {
        api.get("/accounts/broker-matrix").then(r => setD(r.data)).catch(() => {});
    }, []);

    if (!d?.brokers?.length) return null;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] mt-6" data-testid="broker-matrix">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Scale className="w-3.5 h-3.5 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Compatibility Matrix</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    CAPABILITIES · QUIRKS · LEARNED EXECUTION STATS
                </span>
            </div>
            <div className="overflow-x-auto">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="border-b border-[#1F1F1F] font-mono text-[9px] tracking-widest text-[#52525B]">
                            <th className="p-3 text-left">BROKER</th>
                            <th className="p-3 text-left">TYPE</th>
                            <th className="p-3 text-left">EA</th>
                            <th className="p-3 text-left">ORDERCHECK</th>
                            <th className="p-3 text-right">SYMBOLS</th>
                            <th className="p-3 text-left">CERTIFIED</th>
                            <th className="p-3 text-left">LEARNED FILLS</th>
                        </tr>
                    </thead>
                    <tbody>
                        {d.brokers.map(b => (
                            <tr key={b.broker} className="border-b border-[#1F1F1F] align-top"
                                data-testid={`broker-matrix-row-${b.broker}`}>
                                <td className="p-3">
                                    <div className="font-mono text-white">{b.broker}</div>
                                    <div className="font-mono text-[9px] text-[#52525B]">
                                        {b.accounts} acct · {(b.modes || []).join("/")}
                                    </div>
                                </td>
                                <td className="p-3 font-mono text-[#A1A1AA] uppercase">
                                    {(b.account_types || ["—"]).join(" / ")}
                                </td>
                                <td className="p-3 font-mono text-[#A1A1AA]">{b.ea_version ? `v${b.ea_version}` : "—"}</td>
                                <td className="p-3 font-mono text-[10px]"><Flag ok={b.ordercheck} /></td>
                                <td className="p-3 font-mono text-right text-[#A1A1AA]">{b.symbols_mapped}</td>
                                <td className="p-3 font-mono text-[10px]"><Flag ok={b.certified} yes="DEMO ✓" no="PENDING" /></td>
                                <td className="p-3 font-mono text-[10px] text-[#A1A1AA]">
                                    {b.learned?.totals
                                        ? `${b.learned.totals.fills ?? 0} fills · ${b.learned.totals.rejects ?? 0} rejects`
                                        : "no data"}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
            <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-4 border-t border-[#1F1F1F]">
                {d.brokers.map(b => (
                    (b.quirks?.length || b.recent_retcodes?.length) ? (
                        <div key={b.broker} data-testid={`broker-quirks-${b.broker}`}>
                            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1">
                                {b.broker.toUpperCase()} — QUIRKS
                            </div>
                            {(b.quirks || []).map((q, i) => (
                                <div key={i} className="flex items-start gap-2 py-0.5 font-mono text-[10px] text-[#A1A1AA]">
                                    <span className="text-[#FFB000]">▸</span>{q}
                                </div>
                            ))}
                            {(b.recent_retcodes || []).map((rc, i) => (
                                <div key={`rc-${i}`} className="flex items-start gap-2 py-0.5 font-mono text-[10px] text-[#FF3B30]">
                                    <span>▸</span>retcode {rc.retcode}{rc.label ? ` — ${rc.label}` : ""}
                                </div>
                            ))}
                        </div>
                    ) : null
                ))}
            </div>
        </div>
    );
}
