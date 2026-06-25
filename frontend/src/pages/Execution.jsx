import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    Zap, Activity, Network, Layers, RefreshCw, Clock,
    TrendingUp, ChevronRight, Send,
} from "lucide-react";

const TIER_COLOR = {
    EXCELLENT: "#00FF41",
    GOOD:      "#84CC16",
    FAIR:      "#FFB000",
    POOR:      "#F97316",
    AVOID:     "#FF3B30",
};

const PULSE_COLOR = {
    ACTIVE:  "#00FF41",
    NORMAL:  "#FFB000",
    THIN:    "#F97316",
    FROZEN:  "#FF3B30",
};

export default function Execution() {
    const [quality, setQuality] = useState(null);
    const [schedules, setSchedules] = useState([]);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");

    // Preview probe
    const [probeSymbol, setProbeSymbol] = useState("XAUUSD");
    const [probeAction, setProbeAction] = useState("BUY");
    const [probeLot, setProbeLot] = useState(0.10);
    const [probeResult, setProbeResult] = useState(null);
    const [probeLoading, setProbeLoading] = useState(false);

    const load = useCallback(async () => {
        setErr("");
        try {
            const [q, s] = await Promise.allSettled([
                api.get("/execution/quality?symbols=XAUUSD,BTCUSD"),
                api.get("/execution/schedules"),
            ]);
            if (q.status === "fulfilled") setQuality(q.value.data);
            if (s.status === "fulfilled") setSchedules(s.value.data.items || []);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);
    useEffect(() => {
        const t = setInterval(load, 15000);
        return () => clearInterval(t);
    }, [load]);

    const runProbe = async () => {
        setProbeLoading(true);
        try {
            const { data } = await api.post("/execution/preview", {
                signal: { symbol: probeSymbol, action: probeAction, lot_size: probeLot },
            });
            setProbeResult(data);
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setProbeLoading(false);
        }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Execution Intelligence"
                subtitle="Smart routing · TWAP/VWAP · liquidity · order-book pulse"
                testid="execution-header"
                action={
                    <button onClick={load} data-testid="execution-refresh"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest">
                        <RefreshCw className="w-3.5 h-3.5" /> REFRESH
                    </button>
                }
            />
            <div className="p-4 md:p-8 space-y-6 max-w-6xl">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING…</div>
                ) : (
                    <>
                        {/* Quality per symbol */}
                        <Section icon={Activity} color="#00FF41" title="Live Execution Quality">
                            {(quality?.items || []).length === 0 ? (
                                <div className="text-xs text-[#52525B]">No symbols to score.</div>
                            ) : (
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3" data-testid="quality-grid">
                                    {(quality?.items || []).map(row => (
                                        <SymbolCard key={row.symbol} row={row} />
                                    ))}
                                </div>
                            )}
                        </Section>

                        {/* Smart router probe */}
                        <Section icon={Network} color="#A855F7" title="Smart Order Router · Preview"
                                 subtitle="Probe what the router would do for a hypothetical trade">
                            <div className="flex gap-2 flex-wrap items-end mb-3">
                                <Field label="SYMBOL">
                                    <select value={probeSymbol}
                                        onChange={e => setProbeSymbol(e.target.value)}
                                        data-testid="probe-symbol"
                                        className="bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs font-mono">
                                        <option>XAUUSD</option>
                                        <option>BTCUSD</option>
                                    </select>
                                </Field>
                                <Field label="SIDE">
                                    <select value={probeAction}
                                        onChange={e => setProbeAction(e.target.value)}
                                        data-testid="probe-action"
                                        className="bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs font-mono">
                                        <option>BUY</option>
                                        <option>SELL</option>
                                    </select>
                                </Field>
                                <Field label="LOT">
                                    <input type="number" step="0.01" value={probeLot}
                                        onChange={e => setProbeLot(parseFloat(e.target.value) || 0)}
                                        data-testid="probe-lot"
                                        className="bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs font-mono w-20"/>
                                </Field>
                                <button onClick={runProbe}
                                    disabled={probeLoading}
                                    data-testid="probe-run"
                                    className="px-4 py-2 text-xs font-mono tracking-widest bg-[#A855F7] hover:bg-[#9333EA] text-white disabled:opacity-40 flex items-center gap-1.5">
                                    <Send className="w-3.5 h-3.5" />
                                    {probeLoading ? "PROBING…" : "PROBE ROUTE"}
                                </button>
                            </div>
                            {probeResult && <RouterDecision result={probeResult} />}
                        </Section>

                        {/* Active TWAP/VWAP schedules */}
                        <Section icon={Layers} color="#06B6D4" title="Active TWAP/VWAP Schedules">
                            {schedules.length === 0 ? (
                                <div className="text-xs text-[#52525B]" data-testid="no-schedules">
                                    No active slice schedules. The router auto-creates them for large lots or fair-liquidity conditions.
                                </div>
                            ) : (
                                <div className="space-y-2" data-testid="schedules-list">
                                    {schedules.map(s => <ScheduleRow key={s.schedule_id} s={s} />)}
                                </div>
                            )}
                        </Section>
                    </>
                )}
            </div>
        </AppLayout>
    );
}

function Section({ icon: Icon, color, title, subtitle, children }) {
    return (
        <section className="border border-[#1F1F1F] bg-[#0A0A0A]">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Icon className="w-4 h-4" style={{ color }} />
                <div>
                    <div className="font-display font-bold text-base tracking-tight">{title}</div>
                    {subtitle && <div className="font-mono text-[10px] text-[#52525B] tracking-widest">{subtitle}</div>}
                </div>
            </div>
            <div className="p-5">{children}</div>
        </section>
    );
}

function Field({ label, children }) {
    return (
        <label className="flex flex-col gap-1">
            <span className="font-mono text-[9px] text-[#52525B] tracking-widest">{label}</span>
            {children}
        </label>
    );
}

function SymbolCard({ row }) {
    const { symbol, liquidity, order_book } = row;
    return (
        <div className="border border-[#1F1F1F] p-4" data-testid={`quality-card-${symbol}`}>
            <div className="flex items-center justify-between mb-3">
                <div className="font-display font-bold text-lg tracking-tight">{symbol}</div>
                <div className="font-mono text-[10px] tracking-widest"
                     style={{ color: TIER_COLOR[liquidity.tier] }}>
                    LIQUIDITY · {liquidity.tier} · {liquidity.score}/100
                </div>
            </div>

            {/* Score bar */}
            <div className="h-1.5 bg-[#1F1F1F] mb-3">
                <div className="h-1.5" style={{
                    width: `${liquidity.score}%`,
                    background: TIER_COLOR[liquidity.tier],
                }}/>
            </div>

            <div className="text-[10px] text-[#A1A1AA] font-mono mb-3 leading-snug">
                {liquidity.reason}
            </div>

            <div className="grid grid-cols-3 gap-2 text-[10px] font-mono pt-2 border-t border-[#1F1F1F]">
                <Stat label="SPREAD"
                      value={liquidity.details.spread_ratio
                          ? `${liquidity.details.spread_ratio.toFixed(2)}×`
                          : "—"} />
                <Stat label="TICKS/MIN"
                      value={liquidity.details.ticks_per_min.toFixed(1)} />
                <Stat label="SESSION"
                      value={liquidity.details.session?.toUpperCase() || "—"} />
            </div>

            <div className="mt-3 pt-2 border-t border-[#1F1F1F] flex items-center justify-between">
                <div className="font-mono text-[10px] tracking-widest"
                     style={{ color: PULSE_COLOR[order_book.book_pulse] }}>
                    📊 BOOK · {order_book.book_pulse} · depth {order_book.depth_score}/100
                </div>
                {!order_book.real_dom_available && (
                    <div className="font-mono text-[9px] text-[#52525B]" title="Full L2 DOM ships with EA v1.28+">
                        L1 PROXY
                    </div>
                )}
            </div>
        </div>
    );
}

function Stat({ label, value }) {
    return (
        <div>
            <div className="text-[9px] text-[#52525B] tracking-widest mb-0.5">{label}</div>
            <div className="text-[#E4E4E7]">{value}</div>
        </div>
    );
}

function RouterDecision({ result }) {
    const { router, liquidity, schedule } = result;
    const venueColor = "#06B6D4";
    const typeColor = router.order_type === "DEFER" ? "#FF3B30"
                    : router.order_type === "LIMIT" ? "#FFB000"
                    : "#00FF41";
    return (
        <div className="border border-[#A855F7]/30 bg-[#A855F7]/5 p-4 space-y-3" data-testid="router-decision">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Tile label="VENUE" value={router.venue} color={venueColor} />
                <Tile label="ORDER TYPE" value={router.order_type} color={typeColor} />
                <Tile label="SLICE" value={router.slice_strategy} color="#06B6D4"
                      sub={router.schedule_minutes ? `${router.schedule_minutes}m` : ""} />
                <Tile label="LIQUIDITY"
                      value={liquidity.tier}
                      color={TIER_COLOR[liquidity.tier]}
                      sub={`${liquidity.score}/100`} />
            </div>
            <div className="text-xs text-[#A1A1AA] font-mono leading-snug" data-testid="router-reason">
                {router.reason}
            </div>
            {schedule && schedule.slices?.length > 0 && (
                <div className="border-t border-[#A855F7]/20 pt-3" data-testid="probe-schedule">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-2">
                        PROPOSED {schedule.strategy} SCHEDULE — {schedule.slices.length} SLICES OVER {schedule.duration_minutes}m
                    </div>
                    <div className="space-y-1">
                        {schedule.slices.map(s => (
                            <div key={s.idx} className="font-mono text-[10px] flex items-center gap-3 text-[#A1A1AA]">
                                <span className="text-[#52525B] w-6">#{s.idx + 1}</span>
                                <span className="text-[#FFB000] w-16">{s.lot}</span>
                                <Clock className="w-3 h-3 text-[#52525B]" />
                                <span>{new Date(s.fire_at).toLocaleTimeString()}</span>
                                <span className="ml-auto text-[#06B6D4]">w {(s.weight * 100).toFixed(1)}%</span>
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}

function Tile({ label, value, color, sub }) {
    return (
        <div className="border border-[#1F1F1F] p-3">
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className="font-mono font-bold text-base" style={{ color }}>{value}</div>
            {sub && <div className="text-[10px] text-[#52525B] font-mono mt-0.5">{sub}</div>}
        </div>
    );
}

function ScheduleRow({ s }) {
    const pct = s.total_slices > 0 ? (s.filled / s.total_slices) * 100 : 0;
    return (
        <div className="border border-[#1F1F1F] p-3" data-testid={`schedule-${s.schedule_id}`}>
            <div className="flex items-center gap-3 flex-wrap mb-2">
                <span className="font-display font-bold text-sm">{s.symbol}</span>
                <span className="font-mono text-[10px] tracking-widest"
                      style={{ color: s.action === "BUY" ? "#00FF41" : "#FF3B30" }}>
                    {s.action}
                </span>
                <span className="font-mono text-[10px] text-[#06B6D4] tracking-widest">{s.strategy}</span>
                <span className="font-mono text-[10px] text-[#A1A1AA]">
                    {s.total_lot} lot · {s.duration_minutes}m
                </span>
                <span className="ml-auto font-mono text-[10px] text-[#52525B]">
                    {s.filled}/{s.total_slices} filled
                </span>
            </div>
            <div className="h-1.5 bg-[#1F1F1F]">
                <div className="h-1.5 bg-[#06B6D4]" style={{ width: `${pct}%` }} />
            </div>
        </div>
    );
}
