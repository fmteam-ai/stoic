import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { toast } from "sonner";
import {
    Shield, TrendingDown, AlertTriangle, RefreshCw, Layers,
    Activity, ArrowDown, Gauge,
} from "lucide-react";

const SECTOR_LABEL = {
    crypto: "Crypto",
    commodity: "Commodity",
    equity_index: "Equity Index",
    fx_major: "FX Major",
    fx_minor: "FX Minor",
    other: "Other",
};

export default function Portfolio() {
    const [snap, setSnap] = useState(null);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [err, setErr] = useState("");
    const [deleveraging, setDeleveraging] = useState(false);

    const load = useCallback(async () => {
        setErr("");
        setRefreshing(true);
        try {
            const { data } = await api.get("/portfolio/snapshot");
            setSnap(data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); setRefreshing(false); }
    }, []);

    useEffect(() => { load(); }, [load]);
    useEffect(() => {
        const t = setInterval(load, 30000);
        return () => clearInterval(t);
    }, [load]);

    const triggerDeleverage = async () => {
        if (!snap?.actions?.length) return;
        if (!window.confirm(`Close ${snap.actions.length} position(s) to bring portfolio within limits?`)) return;
        setDeleveraging(true);
        try {
            const { data } = await api.post("/portfolio/deleverage", {
                confirm: true, actions: snap.actions,
            });
            toast.success(`Deleveraged · ${data.closed} closed, ${data.skipped} skipped`);
            await load();
        } catch (e) {
            toast.error("Deleverage failed", { description: formatApiError(e) });
        } finally { setDeleveraging(false); }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Portfolio Risk Manager"
                subtitle="Sector caps · VaR · drawdown · correlation · automatic deleveraging"
                testid="portfolio-header"
                action={
                    <button onClick={load} disabled={refreshing} data-testid="portfolio-refresh"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#00FF41]/40 text-xs font-mono tracking-widest disabled:opacity-60">
                        <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin text-[#00FF41]" : ""}`} />
                        {refreshing ? "REFRESHING…" : "REFRESH"}
                    </button>
                }
            />
            <div className="p-4 md:p-8 space-y-6 max-w-6xl">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING SNAPSHOT…</div>
                ) : !snap ? (
                    <div className="text-sm text-[#A1A1AA]">No portfolio data — open a position first.</div>
                ) : (
                    <>
                        {/* Triggers banner */}
                        {snap.needs_deleveraging && (
                            <div className="border border-[#FF3B30]/40 bg-[#FF3B30]/10 p-4"
                                 data-testid="deleverage-banner">
                                <div className="flex items-center gap-2 mb-2">
                                    <AlertTriangle className="w-5 h-5 text-[#FF3B30]" />
                                    <span className="font-mono text-[10px] text-[#FF3B30] tracking-widest">
                                        AUTO-DELEVERAGING RECOMMENDED · {snap.triggers.join(" · ").toUpperCase()}
                                    </span>
                                </div>
                                <div className="text-sm text-[#E4E4E7] mb-3">
                                    {snap.actions.length} position(s) need closing to bring the portfolio back within limits.
                                </div>
                                <button onClick={triggerDeleverage}
                                    disabled={deleveraging}
                                    data-testid="trigger-deleverage"
                                    className="px-4 py-2 text-xs font-mono tracking-widest bg-[#FF3B30] hover:bg-[#E5342B] disabled:opacity-40 text-white flex items-center gap-1.5">
                                    <ArrowDown className="w-3.5 h-3.5" />
                                    {deleveraging ? "DELEVERAGING…" : "EXECUTE DELEVERAGING"}
                                </button>
                            </div>
                        )}

                        {/* Top KPIs */}
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            <Kpi label="EQUITY" value={`$${snap.equity.toFixed(2)}`} />
                            <Kpi label="HWM" value={`$${snap.drawdown.hwm.toFixed(2)}`} sub="Peak" />
                            <Kpi label="DRAWDOWN"
                                 value={`${snap.drawdown.dd_pct.toFixed(2)}%`}
                                 color={snap.drawdown.hard_breach ? "#FF3B30" : snap.drawdown.soft_breach ? "#FFB000" : "#00FF41"}
                                 sub={`$${snap.drawdown.dd_usd.toFixed(2)} from peak`}
                                 testid="kpi-drawdown" />
                            <Kpi label="VAR 95%"
                                 value={`$${snap.var.var_95_usd.toFixed(2)}`}
                                 color={snap.var.var_95_pct_equity > (snap.limits.var_cap_pct || 5) ? "#FF3B30" : "#06B6D4"}
                                 sub={`${snap.var.var_95_pct_equity.toFixed(2)}% of equity (cap ${snap.limits.var_cap_pct}%)`}
                                 testid="kpi-var" />
                        </div>

                        {/* iter-51 · CVaR (Expected Shortfall) — the AVERAGE loss
                            beyond VaR. Always ≥ VaR. Drives the dynamic-budget
                            trim on new trade entries. */}
                        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                            <Kpi label="CVAR 95%"
                                 value={`$${(snap.var.cvar_95_usd ?? 0).toFixed(2)}`}
                                 color={(snap.var.cvar_95_pct_equity ?? 0) > 2.0 ? "#FF3B30"
                                         : (snap.var.cvar_95_pct_equity ?? 0) > 1.0 ? "#FFB000" : "#A855F7"}
                                 sub={`${(snap.var.cvar_95_pct_equity ?? 0).toFixed(2)}% of equity · target ≤2%/day`}
                                 testid="kpi-cvar-95" />
                            <Kpi label="CVAR 99%"
                                 value={`$${(snap.var.cvar_99_usd ?? 0).toFixed(2)}`}
                                 color={(snap.var.cvar_99_pct_equity ?? 0) > 3.0 ? "#FF3B30"
                                         : (snap.var.cvar_99_pct_equity ?? 0) > 1.5 ? "#FFB000" : "#A855F7"}
                                 sub={`${(snap.var.cvar_99_pct_equity ?? 0).toFixed(2)}% of equity · tail stress`}
                                 testid="kpi-cvar-99" />
                            <Kpi label="PORT. σ"
                                 value={`${(snap.var.portfolio_sigma_pct ?? 0).toFixed(2)}%`}
                                 color="#06B6D4"
                                 sub="1-day daily volatility (correlation-weighted)"
                                 testid="kpi-port-sigma" />
                            <Kpi label="POSITIONS"
                                 value={`${snap.var.positions?.length ?? 0}`}
                                 sub={`${snap.var.horizon_days ?? 1}-day horizon`}
                                 testid="kpi-positions" />
                        </div>

                        {/* Drawdown thresholds */}
                        <Section icon={TrendingDown} color="#FF3B30" title="Max-Drawdown Controls">
                            <div className="grid grid-cols-2 gap-3 text-sm">
                                <div>
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">SOFT THRESHOLD</div>
                                    <div className="text-[#FFB000] font-mono">{snap.drawdown.thresholds.soft_pct}% — warn only</div>
                                </div>
                                <div>
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">HARD THRESHOLD</div>
                                    <div className="text-[#FF3B30] font-mono">{snap.drawdown.thresholds.hard_pct}% — auto-deleverage 50%</div>
                                </div>
                            </div>
                            {snap.drawdown.hwm_at && (
                                <div className="text-[10px] text-[#52525B] font-mono mt-2">
                                    HWM set at {snap.drawdown.hwm_at}
                                </div>
                            )}
                        </Section>

                        {/* Sectors */}
                        <Section icon={Layers} color="#FFB000" title="Sector Exposure">
                            {Object.keys(snap.sectors).length === 0 ? (
                                <div className="text-xs text-[#52525B] font-mono">No open positions.</div>
                            ) : (
                                <div className="space-y-2" data-testid="sectors-list">
                                    {Object.entries(snap.sectors).map(([sec, data]) => {
                                        const pct = data.pct_equity;
                                        const cap = data.cap_pct;
                                        const ratio = Math.min(100, (pct / cap) * 100);
                                        const over = data.over_cap;
                                        return (
                                            <div key={sec} data-testid={`sector-${sec}`}>
                                                <div className="flex justify-between font-mono text-xs mb-1">
                                                    <span className="text-[#E4E4E7]">{SECTOR_LABEL[sec] || sec}</span>
                                                    <span className={over ? "text-[#FF3B30]" : "text-[#A1A1AA]"}>
                                                        {pct.toFixed(1)}% / {cap}%{over && " ⚠"}
                                                    </span>
                                                </div>
                                                <div className="h-1.5 bg-[#1F1F1F]">
                                                    <div className="h-1.5"
                                                         style={{ width: `${ratio}%`,
                                                                  background: over ? "#FF3B30" : "#FFB000" }} />
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </Section>

                        {/* Combined risk bucket */}
                        <Section icon={Activity} color="#06B6D4" title="Cross-Asset Correlation Bucket"
                                 subtitle={`crypto + commodity + equity_index ≤ ${snap.combined_risk_bucket.cap_pct}% when avg-corr ≥ ${snap.combined_risk_bucket.corr_threshold}`}>
                            <div className="grid grid-cols-3 gap-3 text-sm">
                                <Stat label="EXPOSURE" value={`${snap.combined_risk_bucket.notional_pct_equity.toFixed(1)}%`}
                                      color={snap.combined_risk_bucket.breach ? "#FF3B30" : "#A1A1AA"} />
                                <Stat label="AVG CORR" value={snap.combined_risk_bucket.avg_corr.toFixed(2)}
                                      color={snap.combined_risk_bucket.avg_corr >= 0.7 ? "#FFB000" : "#A1A1AA"} />
                                <Stat label="STATUS" value={snap.combined_risk_bucket.breach ? "BREACH" : "OK"}
                                      color={snap.combined_risk_bucket.breach ? "#FF3B30" : "#00FF41"} />
                            </div>
                            {snap.combined_risk_bucket.symbols.length > 0 && (
                                <div className="text-[10px] text-[#52525B] font-mono mt-2" data-testid="bucket-symbols">
                                    Includes: {snap.combined_risk_bucket.symbols.join(", ")}
                                </div>
                            )}
                        </Section>

                        {/* Correlation matrix */}
                        {Object.keys(snap.var.correlation_matrix || {}).length > 1 && (
                            <Section icon={Gauge} color="#A855F7" title="Pairwise Correlation Matrix">
                                <CorrelationGrid matrix={snap.var.correlation_matrix} />
                            </Section>
                        )}

                        {/* Positions VaR breakdown */}
                        {snap.var.positions?.length > 0 && (
                            <Section icon={Shield} color="#00FF41" title="VaR — Per Position">
                                <div className="space-y-1" data-testid="var-positions">
                                    {snap.var.positions.map((p) => (
                                        <div key={p.symbol + p.notional}
                                             className="font-mono text-[11px] flex items-center gap-3">
                                            <span className="text-[#FFB000] w-20">{p.symbol}</span>
                                            <span className="text-[#A1A1AA] w-14">lot {p.lot}</span>
                                            <span className="text-[#A1A1AA] flex-1">${p.notional.toFixed(0)} notional</span>
                                            <span className="text-[#A855F7]">ATR% {(p.atr_pct * 100).toFixed(2)}</span>
                                            <span className="text-[#06B6D4]">w {(p.weight * 100).toFixed(1)}%</span>
                                        </div>
                                    ))}
                                </div>
                                {snap.var.notes?.length > 0 && (
                                    <div className="mt-3 space-y-1">
                                        {snap.var.notes.map(n => (
                                            <div key={n} className="text-[10px] text-[#52525B] font-mono">· {n}</div>
                                        ))}
                                    </div>
                                )}
                            </Section>
                        )}
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

function Kpi({ label, value, color = "#E4E4E7", sub, testid }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid={testid}>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className="font-mono font-bold text-xl" style={{ color }}>{value}</div>
            {sub && <div className="text-[10px] text-[#52525B] font-mono mt-1">{sub}</div>}
        </div>
    );
}

function Stat({ label, value, color = "#A1A1AA" }) {
    return (
        <div>
            <div className="font-mono text-[9px] text-[#52525B] tracking-widest mb-1">{label}</div>
            <div className="font-mono font-bold text-base" style={{ color }}>{value}</div>
        </div>
    );
}

function CorrelationGrid({ matrix }) {
    const syms = Object.keys(matrix).sort();
    if (syms.length < 2) return <div className="text-xs text-[#52525B]">Need ≥2 symbols.</div>;
    const cellColor = (v) => {
        const a = Math.abs(v);
        if (a >= 0.7) return v > 0 ? "#FF3B30" : "#06B6D4";
        if (a >= 0.4) return v > 0 ? "#FFB000" : "#A855F7";
        return "#52525B";
    };
    return (
        <table className="font-mono text-[10px]" data-testid="correlation-matrix">
            <thead>
                <tr>
                    <th className="px-2 py-1"></th>
                    {syms.map(s => <th key={s} className="px-2 py-1 text-[#FFB000] tracking-widest">{s}</th>)}
                </tr>
            </thead>
            <tbody>
                {syms.map(s1 => (
                    <tr key={s1}>
                        <td className="px-2 py-1 text-[#FFB000] tracking-widest">{s1}</td>
                        {syms.map(s2 => {
                            const v = matrix[s1]?.[s2] ?? 0;
                            return (
                                <td key={s2} className="px-2 py-1 text-center"
                                    style={{ color: cellColor(v), background: `${cellColor(v)}11` }}>
                                    {v.toFixed(2)}
                                </td>
                            );
                        })}
                    </tr>
                ))}
            </tbody>
        </table>
    );
}
