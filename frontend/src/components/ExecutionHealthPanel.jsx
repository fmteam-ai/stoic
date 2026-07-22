import { Server } from "lucide-react";

const dotCls = alive => alive ? "bg-[#00FF41]" : "bg-[#FF3B30]";

function Chip({ label, value, tone = "neutral", testid }) {
    const tones = {
        neutral: "border-[#1F1F1F] text-[#A1A1AA]",
        good: "border-[#00FF41]/40 text-[#00FF41]",
        warn: "border-[#FFB000]/40 text-[#FFB000]",
        bad: "border-[#FF3B30]/40 text-[#FF3B30]",
    };
    return (
        <div className={`border px-3 py-2 ${tones[tone]}`} data-testid={testid}>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</div>
            <div className="font-mono text-sm mt-0.5">{value}</div>
        </div>
    );
}

export function ExecutionHealthPanel({ data }) {
    if (!data) return null;
    const ob = data.outbox || {};
    const slots = data.submission_slots || {};
    const prot = data.protection || {};
    const lifecycle = Object.entries(data.lifecycle || {});
    const workers = data.workers || [];
    const leases = data.account_leases || [];
    const stuck = data.stuck_pending || [];
    const costs = data.costs || [];
    const latency = data.latency || {};
    const counters = data.counters || {};
    const infra = data.infra || {};

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="execution-health-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Server className="w-3.5 h-3.5 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Execution Health</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">
                    OUTBOX · LEASES · LIFECYCLE · COSTS
                </span>
            </div>

            <div className="p-4 grid grid-cols-2 md:grid-cols-4 gap-3">
                <Chip label="OUTBOX PENDING" testid="exec-outbox-pending"
                    tone={ob.pending > 10 ? "bad" : ob.pending > 0 ? "warn" : "good"}
                    value={`${ob.pending ?? 0}${ob.oldest_pending_age_sec != null ? ` · oldest ${ob.oldest_pending_age_sec}s` : ""}`} />
                <Chip label="OUTBOX FAILED" testid="exec-outbox-failed"
                    tone={ob.failed > 0 ? "bad" : "good"} value={ob.failed ?? 0} />
                <Chip label="SUBMISSION SLOTS" testid="exec-slots"
                    tone={slots.free === 0 && slots.total > 0 ? "warn" : "neutral"}
                    value={`${slots.active ?? 0} active / ${slots.total ?? 0}`} />
                <Chip label="UNPROTECTED OPEN" testid="exec-unprotected"
                    tone={(prot.unprotected || 0) + (prot.no_stop || 0) > 0 ? "bad" : "good"}
                    value={`${prot.unprotected ?? 0} pending SL · ${prot.no_stop ?? 0} no stop`} />
            </div>

            {data.unresolved_submissions > 0 && (
                <div className="px-4 pb-3" data-testid="exec-unresolved-banner">
                    <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 font-mono text-[10px] tracking-widest text-[#FF3B30]">
                        {data.unresolved_submissions} BROKER-ACCEPTED ORDER(S) AWAITING POSITION RESOLUTION — reservation held, EA re-resolving
                    </div>
                </div>
            )}

            <div className="px-4 pb-3 flex flex-wrap gap-2" data-testid="exec-sre-row">
                <span className="font-mono text-[10px] tracking-widest px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]" data-testid="exec-latency">
                    LATENCY p50 <span className="text-white">{latency.p50_sec ?? "—"}s</span> · p95 <span className="text-white">{latency.p95_sec ?? "—"}s</span> · p99 <span className="text-white">{latency.p99_sec ?? "—"}s</span> ({latency.n ?? 0})
                </span>
                <span className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${counters.rejects_24h > 0 ? "border-[#FFB000]/40 text-[#FFB000]" : "border-[#1F1F1F] text-[#A1A1AA]"}`} data-testid="exec-rejects">
                    REJECTS 24H <span className="text-white">{counters.rejects_24h ?? 0}</span>
                </span>
                <span className="font-mono text-[10px] tracking-widest px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]" data-testid="exec-replays">
                    REPLAYS 24H <span className="text-white">{counters.replays_24h ?? 0}</span>
                </span>
                <span className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${counters.partial_fills_open > 0 ? "border-[#FFB000]/40 text-[#FFB000]" : "border-[#1F1F1F] text-[#A1A1AA]"}`} data-testid="exec-partials">
                    PARTIAL FILLS OPEN <span className="text-white">{counters.partial_fills_open ?? 0}</span>
                </span>
                <span className="font-mono text-[10px] tracking-widest px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]" data-testid="exec-mongo">
                    MONGO <span className="text-white">{infra.mongo_latency_ms ?? "—"}ms</span>
                </span>
                {(infra.heartbeats || []).map(h => (
                    <span key={h.label} className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${h.fresh ? "border-[#00FF41]/30 text-[#00FF41]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`} data-testid={`exec-hb-${h.label}`}>
                        EA {h.label} <span>{h.age_sec != null ? `${h.age_sec}s` : "never"}</span>
                    </span>
                ))}
            </div>

            {lifecycle.length > 0 && (
                <div className="px-4 pb-3 flex flex-wrap gap-2" data-testid="exec-lifecycle-row">
                    {lifecycle.map(([state, n]) => (
                        <span key={state}
                            className="font-mono text-[10px] tracking-widest px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA]"
                            data-testid={`exec-lc-${state}`}>
                            {state} <span className="text-white">{n}</span>
                        </span>
                    ))}
                </div>
            )}

            {stuck.length > 0 && (
                <div className="px-4 pb-3" data-testid="exec-stuck-list">
                    <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/5 px-3 py-2 space-y-1">
                        <div className="font-mono text-[9px] tracking-widest text-[#FF3B30]">
                            STUCK PENDING DISPATCH ({stuck.length})
                        </div>
                        {stuck.map(s => (
                            <div key={s.trade_id} className="font-mono text-[10px] text-[#A1A1AA]">
                                {s.symbol} · {s.account || "—"} · {s.state} · {Math.round(s.age_sec / 60)}min
                            </div>
                        ))}
                    </div>
                </div>
            )}

            <div className="px-4 pb-4 grid grid-cols-1 md:grid-cols-2 gap-4">
                <div data-testid="exec-workers">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">WORKER LEASES</div>
                    {workers.length === 0 && (
                        <div className="font-mono text-[10px] text-[#52525B]">no worker leases recorded</div>
                    )}
                    {workers.map(w => (
                        <div key={w.name} className="flex items-center gap-2 py-0.5 font-mono text-[10px]">
                            <span className={`inline-block w-1.5 h-1.5 rounded-full ${dotCls(w.alive)}`} />
                            <span className="text-white">{w.name}</span>
                            <span className="text-[#52525B] truncate">{w.holder}</span>
                        </div>
                    ))}
                    {leases.length > 0 && (
                        <div className="mt-2">
                            <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">ACCOUNT LEASES (SCALP)</div>
                            {leases.map(l => (
                                <div key={l.account_id} className="flex items-center gap-2 py-0.5 font-mono text-[10px]">
                                    <span className={`inline-block w-1.5 h-1.5 rounded-full ${dotCls(l.alive)}`} />
                                    <span className="text-white">{l.account}</span>
                                    <span className="text-[#52525B]">epoch {l.lease_epoch}</span>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
                <div data-testid="exec-costs">
                    <div className="font-mono text-[9px] tracking-widest text-[#52525B] mb-1.5">
                        EXPECTED VS REALIZED COST (recent closed bot trades)
                    </div>
                    {costs.length === 0 && (
                        <div className="font-mono text-[10px] text-[#52525B]">no slippage data yet</div>
                    )}
                    {costs.map(c => (
                        <div key={c.symbol} className="flex items-center gap-3 py-0.5 font-mono text-[10px]"
                            data-testid={`exec-cost-${c.symbol}`}>
                            <span className="text-white w-16">{c.symbol}</span>
                            <span className="text-[#A1A1AA]">realized {c.avg_realized_slippage_pips}p avg · {c.trades} trades</span>
                            {c.expected_cost_pips != null && (
                                <span className={c.avg_realized_slippage_pips > c.expected_cost_pips ? "text-[#FFB000]" : "text-[#00FF41]"}>
                                    expected ≤{c.expected_cost_pips}p
                                </span>
                            )}
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}
