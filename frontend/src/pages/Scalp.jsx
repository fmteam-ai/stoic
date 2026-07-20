import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Zap, RefreshCw, ShieldAlert } from "lucide-react";
import { toast } from "sonner";

const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));

function Chip({ ok, label, testid }) {
    return (
        <span data-testid={testid}
              className={`px-2 py-0.5 text-xs border ${ok
                  ? "text-[#00FF41] border-[#00FF41]/40"
                  : "text-[#FF3B30] border-[#FF3B30]/40"}`}>
            {label}
        </span>
    );
}

function Metric({ label, value, testid }) {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3" data-testid={testid}>
            <div className="text-[10px] uppercase tracking-widest text-[#52525B]">{label}</div>
            <div className="text-lg text-[#E4E4E7] font-mono mt-1">{value ?? "—"}</div>
        </div>
    );
}

export default function Scalp() {
    const [accounts, setAccounts] = useState([]);
    const [accountId, setAccountId] = useState("");
    const [runners, setRunners] = useState([]);
    const [metrics, setMetrics] = useState(null);
    const [decisions, setDecisions] = useState([]);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const [st, mt, dc] = await Promise.all([
                api.get("/scalp/status"),
                api.get("/scalp/metrics?symbol=EURUSD"),
                api.get("/scalp/decisions?limit=25&symbol=EURUSD"),
            ]);
            setRunners(st.data.runners || []);
            setMetrics(mt.data);
            setDecisions(dc.data.decisions || []);
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        api.get("/accounts").then((r) => {
            setAccounts(r.data || []);
            if (r.data?.length) setAccountId((cur) => cur || r.data[0].id);
        }).catch(() => {});
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 15000);
        return () => clearInterval(t);
    }, [load]);

    const acctName = (id) => {
        const ac = accounts.find((x) => x.id === id);
        return ac ? (ac.label || ac.account_number) : (id || "").slice(-6);
    };

    const setConfig = async (enabled, mode) => {
        if (!accountId) return toast.error("Select an account first");
        if (mode === "demo_live"
            && !window.confirm("DEMO LIVE mode sends REAL orders to the broker on this account. Continue?"))
            return;
        setSaving(true);
        try {
            await api.post("/scalp/config", {
                account_id: accountId, symbol: "EURUSD",
                enabled, mode, confirm_live: mode === "demo_live",
            });
            toast.success(enabled
                ? `Scalp ${mode.toUpperCase()} enabled on ${acctName(accountId)}`
                : `Scalp disabled on ${acctName(accountId)}`);
            load();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally {
            setSaving(false);
        }
    };

    const a = metrics?.alpha || {};
    return (
        <AppLayout>
            <PageHeader
                icon={Zap}
                title="Scalp Fast Path"
                subtitle="EURUSD micro-pullback continuation · tick-driven · shadow-first"
                actions={
                    <button onClick={load} data-testid="scalp-refresh-btn"
                            className="p-2 border border-[#1F1F1F] text-[#A1A1AA] hover:text-white">
                        <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
                    </button>
                }
            />

            {/* Config */}
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 mb-6" data-testid="scalp-config-card">
                <div className="flex flex-wrap items-center gap-3">
                    <select value={accountId} onChange={(e) => setAccountId(e.target.value)}
                            data-testid="scalp-account-select"
                            className="bg-[#0A0A0A] border border-[#1F1F1F] text-[#E4E4E7] text-sm px-3 py-2">
                        {accounts.map((ac) => (
                            <option key={ac.id} value={ac.id}>{ac.label || ac.account_number}</option>
                        ))}
                    </select>
                    <button disabled={saving} onClick={() => setConfig(true, "shadow")}
                            data-testid="scalp-enable-shadow-btn"
                            className="px-4 py-2 text-sm border border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10">
                        ENABLE SHADOW
                    </button>
                    <button disabled={saving} onClick={() => setConfig(true, "demo_live")}
                            data-testid="scalp-enable-demolive-btn"
                            className="px-4 py-2 text-sm border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10">
                        <ShieldAlert size={12} className="inline mr-1" />DEMO LIVE
                    </button>
                    <button disabled={saving} onClick={() => setConfig(false, "shadow")}
                            data-testid="scalp-disable-btn"
                            className="px-4 py-2 text-sm border border-[#1F1F1F] text-[#A1A1AA] hover:text-white">
                        DISABLE
                    </button>
                </div>
                <p className="text-xs text-[#52525B] mt-3">
                    Requires EA v1.48 attached with <span className="font-mono text-[#A1A1AA]">TickStreamEnabled=true, TickStreamSymbol=EURUSD</span>.
                    Shadow mode runs the full pipeline and logs decisions without sending orders.
                </p>
            </div>

            {/* Runners */}
            <div className="grid gap-4 mb-6">
                {runners.length === 0 && (
                    <div className="text-sm text-[#52525B]" data-testid="scalp-no-runners">
                        No tick stream received yet — attach EA v1.48 with tick streaming enabled.
                    </div>
                )}
                {runners.map((r) => (
                    <div key={`${r.account_id}:${r.symbol}`}
                         className="border border-[#1F1F1F] bg-[#0A0A0A] p-4"
                         data-testid={`scalp-runner-${r.symbol}`}>
                        <div className="flex flex-wrap items-center gap-2 mb-3">
                            <span className="text-[#E4E4E7] font-mono">{r.symbol}</span>
                            <span className="text-xs text-[#A1A1AA] border border-[#1F1F1F] px-2 py-0.5"
                                  data-testid="scalp-runner-account">
                                {acctName(r.account_id)}
                            </span>
                            <Chip ok={r.enabled} label={r.enabled ? `ON · ${r.mode.toUpperCase()}` : "OFF"}
                                  testid="scalp-runner-enabled" />
                            <Chip ok={r.health?.status === "OK"} label={`HEALTH ${r.health?.status}`}
                                  testid="scalp-runner-health" />
                            <Chip ok={r.permissions?.long_enabled || r.permissions?.short_enabled}
                                  label={`REGIME ${r.permissions?.regime || "?"}`}
                                  testid="scalp-runner-regime" />
                        </div>
                        <div className="grid grid-cols-2 md:grid-cols-6 gap-2 text-xs text-[#A1A1AA]">
                            <div>Ticks <span className="text-[#E4E4E7] font-mono">{r.counters?.ticks}</span></div>
                            <div>Candidates <span className="text-[#E4E4E7] font-mono">{r.counters?.candidates}</span></div>
                            <div>Shadow <span className="text-[#E4E4E7] font-mono">{r.counters?.shadow_trades}</span></div>
                            <div>Live <span className="text-[#E4E4E7] font-mono">{r.counters?.live_trades}</span></div>
                            <div>Spread <span className="text-[#E4E4E7] font-mono">{fmt(r.spread_pips, 2)}p</span></div>
                            <div>Quote age <span className="text-[#E4E4E7] font-mono">{(r.quote_age_ms == null || r.quote_age_ms >= 86400000) ? "—" : `${r.quote_age_ms}ms`}</span></div>
                        </div>
                        {(r.health?.reasons?.length > 0 || r.permissions?.reasons?.length > 0) && (
                            <div className="text-xs text-[#FF9F0A] mt-2" data-testid="scalp-runner-reasons">
                                {[...(r.health?.reasons || []), ...(r.permissions?.reasons || [])].join(" · ")}
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {/* Metrics */}
            <h2 className="text-sm text-[#A1A1AA] uppercase tracking-widest mb-2">Shadow Performance (labeled outcomes)</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2 mb-6">
                <Metric label="Samples" value={metrics?.n} testid="scalp-metric-n" />
                <Metric label="Target-first %" value={a.target_before_stop_rate != null ? `${(a.target_before_stop_rate * 100).toFixed(1)}%` : "—"} testid="scalp-metric-tbs" />
                <Metric label="Net exp (pips)" value={fmt(a.net_expectancy_pips, 3)} testid="scalp-metric-netexp" />
                <Metric label="Profit factor" value={fmt(a.profit_factor)} testid="scalp-metric-pf" />
                <Metric label="Avg win / loss" value={a.avg_winner_pips != null ? `${fmt(a.avg_winner_pips)} / ${fmt(a.avg_loser_pips)}` : "—"} testid="scalp-metric-winloss" />
                <Metric label="Timeout rate" value={a.timeout_rate != null ? `${(a.timeout_rate * 100).toFixed(0)}%` : "—"} testid="scalp-metric-timeout" />
                <Metric label="Model OOS AUC" value={metrics?.model ? `${fmt(metrics.model.oos_auc, 3)}${metrics.model.usable ? " ✓" : " (shelved)"}` : "not trained"} testid="scalp-metric-auc" />
            </div>

            {/* Decisions */}
            <h2 className="text-sm text-[#A1A1AA] uppercase tracking-widest mb-2">Recent Decisions</h2>
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] overflow-x-auto" data-testid="scalp-decisions-table">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="text-[#52525B] uppercase tracking-wider border-b border-[#1F1F1F]">
                            <th className="text-left p-2">Time</th>
                            <th className="text-left p-2">Dir</th>
                            <th className="text-left p-2">Verdict</th>
                            <th className="text-right p-2">Edge (p)</th>
                            <th className="text-right p-2">p(target)</th>
                            <th className="text-right p-2">Cost (p)</th>
                            <th className="text-left p-2">Outcome</th>
                            <th className="text-right p-2">Net (p)</th>
                            <th className="text-left p-2">Reject reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        {decisions.map((d) => (
                            <tr key={d.id} className="border-b border-[#141414] text-[#A1A1AA]"
                                data-testid={`scalp-decision-row-${d.id}`}>
                                <td className="p-2 font-mono">{new Date(d.ts_ms).toLocaleTimeString()}</td>
                                <td className={`p-2 font-mono ${d.direction === "BUY" ? "text-[#00FF41]" : "text-[#FF3B30]"}`}>{d.direction}</td>
                                <td className="p-2">{d.verdict}</td>
                                <td className="p-2 text-right font-mono">{fmt(d.net_edge_pips, 2)}</td>
                                <td className="p-2 text-right font-mono">{fmt(d.forecast?.p_target_before_stop, 2)}</td>
                                <td className="p-2 text-right font-mono">{fmt(d.cost_pips, 2)}</td>
                                <td className="p-2">{d.outcome?.result || "pending"}</td>
                                <td className={`p-2 text-right font-mono ${(d.outcome?.net_pips || 0) > 0 ? "text-[#00FF41]" : (d.outcome?.net_pips || 0) < 0 ? "text-[#FF3B30]" : ""}`}>
                                    {fmt(d.outcome?.net_pips, 1)}
                                </td>
                                <td className="p-2 text-[#52525B]">
                                    {d.verdict === "rejected"
                                        ? (d.gates?.edge?.reason || d.gates?.risk?.reason
                                           || d.gates?.final?.reason
                                           || (d.gates?.permission?.reasons || []).join(",") || "—")
                                        : "—"}
                                </td>
                            </tr>
                        ))}
                        {decisions.length === 0 && (
                            <tr><td colSpan={9} className="p-4 text-center text-[#52525B]">No decisions yet</td></tr>
                        )}
                    </tbody>
                </table>
            </div>
        </AppLayout>
    );
}
