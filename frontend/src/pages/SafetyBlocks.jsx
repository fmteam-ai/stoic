import { useEffect, useState, useCallback, useMemo } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { ShieldCheck, RefreshCw, AlertTriangle, ChevronDown, ChevronUp } from "lucide-react";
import { toast } from "sonner";

const REASON_COLOR = {
    equity_known:            "#FF3B30",
    equity_vs_balance_floor: "#FF3B30",
    free_margin_floor:       "#FFB000",
    risk_inputs_present:     "#A1A1AA",
    per_trade_risk_cap:      "#FFB000",
    lot_vs_equity_sanity:    "#FFB000",
    daily_loss_cap:          "#FF3B30",
    total_open_risk_cap:     "#FF3B30",
    max_concurrent_cap:      "#A1A1AA",
};

function fmtTime(iso) {
    if (!iso) return "—";
    try {
        const d = new Date(iso);
        return d.toLocaleString(undefined, { month: "short", day: "2-digit",
                                              hour: "2-digit", minute: "2-digit" });
    } catch { return iso; }
}

function Sparkline({ data, color = "#00FF41" }) {
    if (!data?.length) return null;
    const max = Math.max(1, ...data.map((d) => d.count));
    const w = 280, h = 60, step = w / Math.max(1, data.length - 1);
    const points = data.map((d, i) => {
        const x = i * step;
        const y = h - (d.count / max) * (h - 6) - 3;
        return `${x},${y}`;
    }).join(" ");
    return (
        <svg width={w} height={h} className="overflow-visible" data-testid="sb-sparkline">
            <polyline fill="none" stroke={color} strokeWidth="1.5" points={points} />
            {data.map((d, i) => {
                const x = i * step;
                const y = h - (d.count / max) * (h - 6) - 3;
                return (
                    <g key={i}>
                        <circle cx={x} cy={y} r={d.count > 0 ? 3 : 1.5}
                                fill={d.count > 0 ? color : "#52525B"} />
                        <title>{`${d.date}: ${d.count} block${d.count !== 1 ? "s" : ""}`}</title>
                    </g>
                );
            })}
        </svg>
    );
}

function ReasonBar({ rows }) {
    if (!rows?.length) {
        return (
            <div className="text-center py-8 text-[#52525B] font-mono text-xs tracking-widest">
                NO BLOCKS — bot operating within safety envelope
            </div>
        );
    }
    const total = rows.reduce((s, r) => s + r.count, 0);
    return (
        <div className="space-y-2">
            {rows.map((r) => {
                const pct = total > 0 ? (r.count / total) * 100 : 0;
                const color = REASON_COLOR[r.blocked_by] || "#A1A1AA";
                return (
                    <div key={r.blocked_by} data-testid={`sb-reason-${r.blocked_by}`}>
                        <div className="flex items-center justify-between text-xs mb-1">
                            <span className="text-[#E4E4E7]">{r.label}</span>
                            <span className="font-mono text-[10px] text-[#A1A1AA]">
                                {r.count} · {pct.toFixed(0)}%
                            </span>
                        </div>
                        <div className="h-1.5 bg-[#0F0F0F] border border-[#1F1F1F] overflow-hidden">
                            <div className="h-full" style={{ width: `${pct}%`, background: color }} />
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

function BlockRow({ block, expanded, onToggle }) {
    const color = REASON_COLOR[block.blocked_by] || "#A1A1AA";
    return (
        <>
            <tr className="border-b border-[#161616] hover:bg-[#0F0F0F] cursor-pointer"
                onClick={onToggle} data-testid={`sb-row-${block.id}`}>
                <td className="px-3 py-2.5 text-[11px] font-mono text-[#A1A1AA] whitespace-nowrap">
                    {fmtTime(block.blocked_at)}
                </td>
                <td className="px-3 py-2.5 text-xs text-[#E4E4E7]">{block.symbol || "—"}</td>
                <td className="px-3 py-2.5 text-xs">
                    <span className={`font-mono text-[10px] px-1.5 py-0.5 border ${
                        block.action === "BUY"
                            ? "border-[#00FF41]/40 text-[#00FF41]"
                            : "border-[#FF3B30]/40 text-[#FF3B30]"
                    }`}>{block.action || "?"}</span>
                </td>
                <td className="px-3 py-2.5 text-xs font-mono text-[#A1A1AA]">
                    {block.lot_size != null ? Number(block.lot_size).toFixed(2) : "—"}
                </td>
                <td className="px-3 py-2.5 text-xs">
                    <span style={{ color }} className="font-mono text-[10px] tracking-widest">
                        {block.blocked_by}
                    </span>
                </td>
                <td className="px-3 py-2.5 text-xs text-[#A1A1AA]">{block.reason_label}</td>
                <td className="px-3 py-2.5">
                    {expanded ? <ChevronUp className="w-3.5 h-3.5 text-[#52525B]" />
                              : <ChevronDown className="w-3.5 h-3.5 text-[#52525B]" />}
                </td>
            </tr>
            {expanded && (
                <tr className="border-b border-[#161616] bg-[#080808]">
                    <td colSpan={7} className="px-3 py-4">
                        <div className="grid md:grid-cols-2 gap-4 text-[11px]">
                            <div>
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                                    ACCOUNT STATE AT BLOCK
                                </div>
                                {block.context && (
                                    <table className="w-full">
                                        <tbody>
                                            {Object.entries(block.context).map(([k, v]) => (
                                                <tr key={k}>
                                                    <td className="text-[#A1A1AA] py-0.5">{k}</td>
                                                    <td className="text-[#E4E4E7] py-0.5 text-right font-mono">
                                                        {typeof v === "number" ? v.toFixed(2) : String(v)}
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                )}
                            </div>
                            <div>
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                                    AUDIT TRAIL
                                </div>
                                <div className="space-y-1">
                                    {(block.audit || []).map((a, i) => (
                                        <div key={i} className="flex items-start gap-2">
                                            <span className={`font-mono text-[9px] px-1 mt-0.5 ${
                                                a.ok ? "text-[#00FF41]" : "text-[#FF3B30]"
                                            }`}>
                                                {a.ok ? "PASS" : "FAIL"}
                                            </span>
                                            <div className="flex-1">
                                                <div className="text-[#E4E4E7]">{a.name}</div>
                                                {a.reason && (
                                                    <div className="text-[#A1A1AA] text-[10px]">{a.reason}</div>
                                                )}
                                                {a.value !== undefined && a.value !== null && (
                                                    <div className="text-[#52525B] text-[10px] font-mono">
                                                        {String(a.value)}
                                                    </div>
                                                )}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    </td>
                </tr>
            )}
        </>
    );
}

export default function SafetyBlocks() {
    const [days, setDays] = useState(7);
    const [stats, setStats] = useState(null);
    const [list, setList] = useState(null);
    const [loading, setLoading] = useState(true);
    const [expanded, setExpanded] = useState(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [s, l] = await Promise.all([
                api.get(`/safety-blocks/stats?days=${days}`),
                api.get(`/safety-blocks/list?days=${days}&limit=100`),
            ]);
            setStats(s.data);
            setList(l.data);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setLoading(false);
        }
    }, [days]);

    useEffect(() => { load(); }, [load]);

    const goldilocks = useMemo(() => {
        if (!stats) return null;
        if (stats.total === 0) return { msg: "GOLDILOCKS · Bot trading freely within all safety floors", tone: "good" };
        if (stats.total < 5) return { msg: `OK · ${stats.total} blocks in ${days}d — bot occasionally clipped by floors`, tone: "ok" };
        if (stats.total < 20) return { msg: `WATCH · ${stats.total} blocks in ${days}d — config may be too aggressive`, tone: "warn" };
        return { msg: `LOOSEN OR REVIEW · ${stats.total} blocks in ${days}d — guardian frequently rejecting trades`, tone: "bad" };
    }, [stats, days]);

    return (
        <AppLayout>
            <PageHeader
                eyebrow="Safety / risk envelope"
                title="Safety Blocks"
                description="Trades the guardian refused before they could reach the broker."
                actions={
                    <div className="flex items-center gap-2">
                        <select value={days} onChange={(e) => setDays(Number(e.target.value))}
                                className="bg-[#0A0A0A] border border-[#1F1F1F] text-xs text-[#E4E4E7] px-2.5 py-1.5 font-mono"
                                data-testid="sb-days-select">
                            <option value={1}>Last 24h</option>
                            <option value={7}>Last 7 days</option>
                            <option value={30}>Last 30 days</option>
                        </select>
                        <button onClick={load} disabled={loading}
                                className="px-3 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:bg-[#1F1F1F] inline-flex items-center gap-2 font-mono text-[10px] tracking-widest disabled:opacity-40"
                                data-testid="sb-refresh">
                            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                            REFRESH
                        </button>
                    </div>
                }
            />

            <div className="space-y-4">
                {goldilocks && (
                    <div className="border border-[#1F1F1F] px-4 py-3 flex items-center gap-3 bg-[#0A0A0A]"
                         data-testid="sb-goldilocks">
                        <ShieldCheck className="w-4 h-4" style={{
                            color: goldilocks.tone === "good" ? "#00FF41"
                                 : goldilocks.tone === "ok" ? "#A1A1AA"
                                 : goldilocks.tone === "warn" ? "#FFB000" : "#FF3B30",
                        }} />
                        <div className="text-xs font-mono tracking-widest text-[#E4E4E7]">{goldilocks.msg}</div>
                    </div>
                )}

                <div className="grid md:grid-cols-2 gap-4">
                    <div className="border border-[#1F1F1F] p-4">
                        <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-3">
                            BLOCKS BY REASON ({days}D)
                        </div>
                        <ReasonBar rows={stats?.by_reason || []} />
                    </div>

                    <div className="border border-[#1F1F1F] p-4">
                        <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-3">
                            BLOCKS PER DAY
                        </div>
                        <Sparkline data={stats?.by_day || []}
                                   color={stats?.total > 0 ? "#FF3B30" : "#00FF41"} />
                        <div className="mt-3 text-[10px] text-[#52525B] font-mono">
                            Total: {stats?.total ?? 0} block{stats?.total === 1 ? "" : "s"} · Window: {days}d
                        </div>
                    </div>
                </div>

                {stats?.thresholds && (
                    <details className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="sb-thresholds">
                        <summary className="cursor-pointer px-4 py-2.5 font-mono text-[10px] text-[#A1A1AA] tracking-widest hover:bg-[#0F0F0F] inline-flex items-center gap-2">
                            <AlertTriangle className="w-3 h-3" />
                            CURRENT GUARDIAN THRESHOLDS (server-enforced, override via SAFETY_* env)
                        </summary>
                        <div className="px-4 py-3 border-t border-[#1F1F1F] grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-1.5 text-[11px]">
                            {Object.entries(stats.thresholds).map(([k, v]) => (
                                <div key={k} className="flex justify-between">
                                    <span className="text-[#A1A1AA]">{k}</span>
                                    <span className="text-[#E4E4E7] font-mono">{v}%</span>
                                </div>
                            ))}
                        </div>
                    </details>
                )}

                <div className="border border-[#1F1F1F]">
                    <div className="px-4 py-2.5 border-b border-[#1F1F1F] font-mono text-[10px] text-[#FFB000] tracking-widest">
                        REFUSED TRADES ({list?.blocks?.length || 0})
                    </div>
                    {loading && !list && (
                        <div className="text-center py-10 text-[#A1A1AA] text-xs font-mono">Loading…</div>
                    )}
                    {!loading && (list?.blocks?.length || 0) === 0 && (
                        <div className="text-center py-10 text-[#52525B] text-xs font-mono tracking-widest">
                            NO BLOCKS IN WINDOW
                        </div>
                    )}
                    {(list?.blocks?.length || 0) > 0 && (
                        <div className="overflow-x-auto">
                            <table className="w-full text-left">
                                <thead>
                                    <tr className="border-b border-[#1F1F1F] bg-[#0F0F0F]">
                                        <th className="px-3 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">TIME</th>
                                        <th className="px-3 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">SYMBOL</th>
                                        <th className="px-3 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">SIDE</th>
                                        <th className="px-3 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">LOT</th>
                                        <th className="px-3 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">CODE</th>
                                        <th className="px-3 py-2 font-mono text-[9px] text-[#52525B] tracking-widest">REASON</th>
                                        <th className="px-3 py-2" />
                                    </tr>
                                </thead>
                                <tbody>
                                    {(list?.blocks || []).map((b) => (
                                        <BlockRow key={b.id} block={b}
                                                  expanded={expanded === b.id}
                                                  onToggle={() => setExpanded(expanded === b.id ? null : b.id)} />
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            </div>
        </AppLayout>
    );
}
