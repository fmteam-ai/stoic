import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Trophy, RefreshCw, Filter } from "lucide-react";

const ENGINE_LABELS = {
    hf_scalp: "SCALPER",
    hf_scalp_fast: "FAST SCALP",
    range_fade: "MEAN REVERSION",
    breakout_m15: "BREAKOUT HUNTER",
    unattributed: "UNATTRIBUTED",
};

const fmtPnl = (v) => v == null ? "—" : `${v >= 0 ? "+$" : "-$"}${Math.abs(v).toFixed(2)}`;
const pnlCls = (v) => v > 0 ? "text-[#00FF41]" : v < 0 ? "text-[#FF3B30]" : "text-[#A1A1AA]";

function verdict(r) {
    if (r.trades < 3) return { label: "SAMPLE TOO SMALL", cls: "text-[#52525B] border-[#1F1F1F]" };
    if (r.pnl > 0 && (r.profit_factor ?? 99) >= 1.2)
        return { label: "EARNING", cls: "text-[#00FF41] border-[#00FF41]/40" };
    if (r.pnl < 0) return { label: "BLEEDING", cls: "text-[#FF3B30] border-[#FF3B30]/40" };
    return { label: "MARGINAL", cls: "text-[#FFD700] border-[#FFD700]/40" };
}

function EngineRow({ r }) {
    const v = verdict(r);
    const syms = Object.entries(r.symbols || {}).sort((a, b) => b[1] - a[1]);
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid={`engine-row-${r.engine}`}>
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <div className="font-mono text-sm text-white tracking-wider">
                        {ENGINE_LABELS[r.engine] || r.engine.toUpperCase()}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] mt-0.5">
                        {r.version || r.engine} · {r.trades} trades
                    </div>
                </div>
                <span className={`px-2 py-1 border font-mono text-[10px] tracking-widest ${v.cls}`}
                    data-testid={`engine-verdict-${r.engine}`}>{v.label}</span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mt-4">
                <Metric label="W / L" value={<><span className="text-[#00FF41]">{r.wins}W</span><span className="text-[#52525B]"> / </span><span className="text-[#FF3B30]">{r.losses}L</span></>} />
                <Metric label="WIN RATE" value={`${r.win_rate}%`} cls={r.win_rate >= 55 ? "text-[#00FF41]" : r.win_rate >= 40 ? "text-[#FFD700]" : "text-[#FF3B30]"} />
                <Metric label="P&L" value={fmtPnl(r.pnl)} cls={pnlCls(r.pnl)} />
                <Metric label="PROFIT FACTOR" value={r.profit_factor ?? "∞"} cls={(r.profit_factor ?? 99) >= 1.2 ? "text-[#00FF41]" : "text-[#FF3B30]"} />
                <Metric label="AVG WIN / LOSS" value={`$${r.avg_win} / $${r.avg_loss}`} />
                <Metric label="LONG / SHORT" value={<><span className={pnlCls(r.long_pnl)}>{fmtPnl(r.long_pnl)}</span><span className="text-[#52525B]"> · </span><span className={pnlCls(r.short_pnl)}>{fmtPnl(r.short_pnl)}</span></>} />
            </div>
            {syms.length > 1 && (
                <div className="flex flex-wrap gap-2 mt-3">
                    {syms.map(([s, p]) => (
                        <span key={s} className="font-mono text-[10px] px-2 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]">
                            {s} <span className={pnlCls(p)}>{fmtPnl(p)}</span>
                        </span>
                    ))}
                </div>
            )}
        </div>
    );
}

function Metric({ label, value, cls = "text-white" }) {
    return (
        <div>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{label}</div>
            <div className={`font-mono text-sm mt-0.5 ${cls}`}>{value}</div>
        </div>
    );
}

function Funnel({ funnel }) {
    const rejected = funnel.filter((f) => f.status === "rejected");
    const executed = funnel.filter((f) => f.status === "executed")
        .reduce((a, f) => a + f.count, 0);
    const max = Math.max(1, ...rejected.map((f) => f.count));
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="decision-funnel">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">
                DECISION FUNNEL · WHICH GATE DOES THE WORK
            </div>
            <div className="space-y-2">
                {rejected.map((f) => (
                    <div key={f.stage} className="flex items-center gap-2" data-testid={`funnel-${f.stage}`}>
                        <div className="w-44 font-mono text-[10px] text-[#A1A1AA] truncate">{f.stage}</div>
                        <div className="flex-1 h-3 bg-[#111111]">
                            <div className="h-3 bg-[#FF3B30]/60" style={{ width: `${(f.count / max) * 100}%` }} />
                        </div>
                        <div className="w-10 text-right font-mono text-[10px] text-[#A1A1AA]">{f.count}</div>
                    </div>
                ))}
                {!rejected.length && (
                    <div className="font-mono text-xs text-[#52525B]">No rejections recorded in this window yet.</div>
                )}
                <div className="flex items-center gap-2 pt-2 border-t border-[#1F1F1F]" data-testid="funnel-executed">
                    <div className="w-44 font-mono text-[10px] text-[#00FF41]">EXECUTED</div>
                    <div className="flex-1" />
                    <div className="w-10 text-right font-mono text-[10px] text-[#00FF41]">{executed}</div>
                </div>
            </div>
        </div>
    );
}

function AblationPanel({ days }) {
    const [ab, setAb] = useState(null);
    useEffect(() => {
        api.get(`/trades/ablation?days=${days || 90}`).then(({ data }) => setAb(data)).catch(() => {});
    }, [days]);
    const rows = ab?.gates || [];
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="ablation-panel">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                GATE ABLATION · COUNTERFACTUAL VALUE OF EACH VETO
            </div>
            <div className="font-mono text-[10px] text-[#3F3F46] mb-3">
                Vetoed setups replayed against the bars that actually followed — net R &gt; 0 means the gate avoided more loss than it blocked in winners.
            </div>
            {rows.length ? rows.map((g) => (
                <div key={g.gate} className="flex flex-wrap items-center gap-3 py-1.5 border-t border-[#141414]"
                    data-testid={`ablation-${g.gate}`}>
                    <div className="w-44 font-mono text-[10px] text-[#A1A1AA] truncate">{g.gate}</div>
                    <div className="font-mono text-[10px] text-[#52525B]">{g.replayed} replayed</div>
                    <div className="font-mono text-[10px]">
                        <span className="text-[#00FF41]">saved {g.saved_r}R</span>
                        <span className="text-[#52525B]"> · </span>
                        <span className="text-[#FF3B30]">blocked {g.blocked_r}R wins</span>
                    </div>
                    <div className={`ml-auto font-mono text-[10px] px-2 py-0.5 border ${
                        g.verdict === "ADDS VALUE" ? "text-[#00FF41] border-[#00FF41]/40"
                            : g.verdict === "COSTS EDGE" ? "text-[#FF3B30] border-[#FF3B30]/40"
                                : "text-[#FFD700] border-[#FFD700]/40"}`}>
                        {g.verdict} · {g.net_r >= 0 ? "+" : ""}{g.net_r}R
                    </div>
                </div>
            )) : (
                <div className="font-mono text-xs text-[#52525B]" data-testid="ablation-empty">
                    Collecting rejection snapshots — the ledger started recording them recently; this panel fills in as vetoes accrue.
                </div>
            )}
        </div>
    );
}

export default function Scoreboard() {
    const [data, setData] = useState(null);
    const [days, setDays] = useState(30);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");

    const load = useCallback(async (d) => {
        setErr("");
        try {
            const { data: res } = await api.get(`/trades/scoreboard?days=${d}`);
            setData(res);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(days); }, [load, days]);

    return (
        <AppLayout>
            <PageHeader
                title="Strategy Scoreboard"
                subtitle="Per-engine attribution — who earns their spot, and which gate does the work"
                icon={Trophy}
            />
            <div className="space-y-4">
                <div className="flex items-center gap-2" data-testid="scoreboard-period-row">
                    <Filter size={12} className="text-[#52525B]" />
                    {[[7, "7D"], [30, "30D"], [90, "90D"], [0, "ALL"]].map(([d, label]) => (
                        <button key={d} onClick={() => setDays(d)}
                            data-testid={`scoreboard-period-${label}`}
                            className={`px-3 py-1.5 text-xs font-mono tracking-widest border transition-colors ${
                                days === d ? "border-[#00FF41] text-[#00FF41]" : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"}`}>
                            {label}
                        </button>
                    ))}
                    {data && (
                        <span className="ml-auto font-mono text-xs" data-testid="scoreboard-total">
                            <span className="text-[#52525B]">BOT TOTAL · </span>
                            <span className={pnlCls(data.total_pnl)}>{fmtPnl(data.total_pnl)}</span>
                        </span>
                    )}
                </div>
                {err && <div className="font-mono text-xs text-[#FF3B30]" data-testid="scoreboard-error">{err}</div>}
                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] flex items-center gap-2">
                        <RefreshCw size={12} className="animate-spin" /> Loading attribution…
                    </div>
                ) : (
                    <>
                        <div className="space-y-3" data-testid="engine-list">
                            {(data?.engines || []).map((r) => <EngineRow key={r.engine} r={r} />)}
                            {!data?.engines?.length && (
                                <div className="font-mono text-xs text-[#52525B]">No closed bot trades in this window.</div>
                            )}
                        </div>
                        <Funnel funnel={data?.funnel || []} />
                        <AblationPanel days={days} />
                    </>
                )}
            </div>
        </AppLayout>
    );
}
