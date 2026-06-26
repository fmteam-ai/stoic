import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { BarChart3, TrendingUp, TrendingDown, RefreshCw, Trophy, AlertTriangle, Target, Gauge, Cpu, Clock } from "lucide-react";

function pnlColor(v) {
    if (v > 0) return "text-[#00FF41]";
    if (v < 0) return "text-[#FF3B30]";
    return "text-[#A1A1AA]";
}

function HeadlineTile({ label, value, accent, icon: Icon }) {
    return (
        <div className="p-4 border border-[#1F1F1F] bg-[#0A0A0A]">
            <div className="flex items-center gap-2 mb-2">
                {Icon && <Icon className="w-3.5 h-3.5 text-[#52525B]" />}
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">{label}</span>
            </div>
            <div className={`font-mono font-medium text-lg ${accent || "text-white"}`}>{value}</div>
        </div>
    );
}

function SliceTable({ title, rows, subtitle, testid }) {
    if (!rows || rows.length === 0) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid={testid}>
                <div className="px-4 py-3 border-b border-[#1F1F1F]">
                    <div className="font-display font-bold text-sm tracking-tight">{title}</div>
                    {subtitle && <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">{subtitle}</div>}
                </div>
                <div className="p-4 font-mono text-xs text-[#52525B] tracking-widest">NO DATA</div>
            </div>
        );
    }
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid={testid}>
            <div className="px-4 py-3 border-b border-[#1F1F1F]">
                <div className="font-display font-bold text-sm tracking-tight">{title}</div>
                {subtitle && <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">{subtitle}</div>}
            </div>
            <div className="overflow-x-auto">
                <table className="w-full text-xs">
                    <thead>
                        <tr className="border-b border-[#1F1F1F]">
                            {["SLICE", "N", "WIN %", "TOTAL P&L", "AVG P&L"].map(h => (
                                <th key={h} className="px-3 py-2 text-left font-mono text-[10px] text-[#52525B] tracking-widest whitespace-nowrap">{h}</th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map((r) => (
                            <tr key={r.key} className="border-b border-[#1F1F1F] hover:bg-[#121212]"
                                data-testid={`slice-${testid}-${String(r.key).replace(/\s+/g, "-")}`}>
                                <td className="px-3 py-2 font-mono">{r.key}</td>
                                <td className="px-3 py-2 font-mono text-[#A1A1AA]">{r.count}</td>
                                <td className={`px-3 py-2 font-mono ${r.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FFB000]"}`}>
                                    {r.win_rate}%
                                </td>
                                <td className={`px-3 py-2 font-mono ${pnlColor(r.total_pnl)}`}>
                                    {r.total_pnl >= 0 ? "+" : ""}{r.total_pnl}
                                </td>
                                <td className={`px-3 py-2 font-mono ${pnlColor(r.avg_pnl)}`}>
                                    {r.avg_pnl >= 0 ? "+" : ""}{r.avg_pnl}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

// Per-session edge card — Asia / London / Overlap / NY / Off-hours.
// Surfaces which UTC session window the trader's edge actually lives in
// (win-rate, avg-R, expectancy-R, total P&L) so risk can be weighted toward it.
const SESSION_ACCENT = {
    ASIA:    "border-l-[#FFB000]",
    LONDON:  "border-l-[#00FF41]",
    OVERLAP: "border-l-[#FFD700]",
    NY:      "border-l-[#0099FF]",
    OFF:     "border-l-[#52525B]",
};

function rColor(v) {
    if (v == null) return "text-[#52525B]";
    if (v >= 0.3) return "text-[#00FF41]";
    if (v <= -0.3) return "text-[#FF3B30]";
    return "text-[#FFB000]";
}

function SessionCard({ b, isBestR, isBestPnl, minSample }) {
    const accent = SESSION_ACCENT[b.key] || "border-l-[#52525B]";
    const insufficient = b.count < minSample;
    return (
        <div className={`border border-[#1F1F1F] border-l-4 ${accent} bg-[#0A0A0A] p-4 space-y-2`}
             data-testid={`session-card-${b.key}`}>
            <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                    <div className="font-display font-bold text-base tracking-tight">{b.key}</div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest truncate">{b.label}</div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                    {isBestR && (
                        <span className="px-1.5 py-0.5 border border-[#00FF41]/40 text-[#00FF41] text-[9px] font-mono tracking-widest">
                            BEST AVG-R
                        </span>
                    )}
                    {isBestPnl && (
                        <span className="px-1.5 py-0.5 border border-[#FFD700]/40 text-[#FFD700] text-[9px] font-mono tracking-widest">
                            BEST P&amp;L
                        </span>
                    )}
                </div>
            </div>

            <div className="grid grid-cols-2 gap-2 pt-2 border-t border-[#1F1F1F]">
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">TRADES</div>
                    <div className="font-mono text-sm">{b.count}</div>
                </div>
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">WIN RATE</div>
                    <div className={`font-mono text-sm ${b.count === 0 ? "text-[#52525B]" : b.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FFB000]"}`}>
                        {b.count === 0 ? "—" : `${b.win_rate}%`}
                    </div>
                </div>
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">AVG R</div>
                    <div className={`font-mono text-sm ${rColor(b.avg_r)}`}>
                        {b.avg_r == null ? "—" : `${b.avg_r >= 0 ? "+" : ""}${b.avg_r}R`}
                    </div>
                </div>
                <div>
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">EXPECTANCY</div>
                    <div className={`font-mono text-sm ${rColor(b.expectancy_r)}`}>
                        {b.expectancy_r == null ? "—" : `${b.expectancy_r >= 0 ? "+" : ""}${b.expectancy_r}R`}
                    </div>
                </div>
                <div className="col-span-2">
                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest">TOTAL P&amp;L</div>
                    <div className={`font-mono text-sm ${pnlColor(b.total_pnl)}`}>
                        {b.count === 0 ? "—" : `${b.total_pnl >= 0 ? "+$" : "-$"}${Math.abs(b.total_pnl).toFixed(2)}`}
                    </div>
                </div>
            </div>

            {insufficient && b.count > 0 && (
                <div className="font-mono text-[9px] text-[#52525B] tracking-widest pt-1">
                    NEEDS ≥{minSample} TRADES TO RANK
                </div>
            )}
        </div>
    );
}

function SessionBreakdown({ sessions }) {
    if (!sessions) return null;
    const buckets = sessions.buckets || [];
    const totalTrades = sessions.overall?.count || 0;
    const minSample = sessions.min_sample_for_ranking || 3;

    return (
        <div data-testid="session-breakdown">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3 flex items-center gap-2">
                <Clock className="w-3 h-3" /> SESSION BREAKDOWN · WHERE YOUR EDGE LIVES
            </div>

            {totalTrades === 0 ? (
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 text-xs text-[#A1A1AA] leading-relaxed"
                     data-testid="session-breakdown-empty">
                    No closed trades yet. Once the bot has a few sessions of data, this card will show which UTC window
                    (Asia / London / Overlap / NY / Off-hours) your strategy actually wins in — so you can weight risk
                    toward the session your edge lives in.
                </div>
            ) : (
                <>
                    <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-3" data-testid="session-cards">
                        {buckets.map(b => (
                            <SessionCard
                                key={b.key}
                                b={b}
                                isBestR={sessions.best_session_by_r === b.key}
                                isBestPnl={sessions.best_session_by_pnl === b.key}
                                minSample={minSample}
                            />
                        ))}
                    </div>
                    {(sessions.best_session_by_r || sessions.best_session_by_pnl) && (
                        <div className="mt-3 p-3 border border-[#1F1F1F] bg-[#0A0A0A] text-xs text-[#A1A1AA] leading-relaxed">
                            {sessions.best_session_by_r && (
                                <div>
                                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">INSIGHT · </span>
                                    Highest avg-R is <span className="text-[#00FF41] font-mono">{sessions.best_session_by_r}</span>
                                    {sessions.best_session_by_pnl && sessions.best_session_by_pnl !== sessions.best_session_by_r && (
                                        <> · highest cumulative P&amp;L is <span className="text-[#FFD700] font-mono">{sessions.best_session_by_pnl}</span></>
                                    )}.
                                </div>
                            )}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}


export default function Analytics() {
    const [data, setData] = useState(null);
    const [tune, setTune] = useState(null);
    const [learned, setLearned] = useState(null);
    const [sessions, setSessions] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");

    const load = useCallback(async () => {
        setLoading(true); setErr("");
        try {
            const [attr, autoTune, learned, sess] = await Promise.all([
                api.get("/analytics/attribution"),
                api.get("/analytics/auto-tune").catch(() => ({ data: null })),
                api.get("/analytics/learned-meta").catch(() => ({ data: null })),
                api.get("/analytics/sessions").catch(() => ({ data: null })),
            ]);
            setData(attr.data);
            setTune(autoTune.data);
            setLearned(learned.data);
            setSessions(sess.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    const retrainLearned = async () => {
        try {
            const { data } = await api.post("/analytics/learned-meta/retrain");
            setLearned({ ...(data.trained ? data : {}), trained: data.trained, reason: data.reason });
        } catch (e) {
            setErr(formatApiError(e));
        }
    };
    useEffect(() => { load(); }, [load]);

    const overall = data?.overall;

    // Mine best + worst slices across every dimension for the headline insights
    const allSlices = [];
    if (data) {
        const namedSlices = [
            { name: "symbol", rows: data.by_symbol },
            { name: "action", rows: data.by_action },
            { name: "session", rows: data.by_session },
            { name: "hour", rows: data.by_hour },
            { name: "dow", rows: data.by_day_of_week },
            { name: "confidence", rows: data.by_confidence },
            { name: "risk", rows: data.by_risk_level },
            { name: "regime", rows: data.by_regime },
            { name: "symbol×session", rows: data.by_symbol_session },
            { name: "symbol×confidence", rows: data.by_symbol_confidence },
        ];
        namedSlices.forEach(({ name, rows }) => {
            (rows || []).filter(r => r.count >= 3).forEach(r => {
                allSlices.push({ ...r, dim: name });
            });
        });
    }
    const topSlices = [...allSlices].sort((a, b) => b.total_pnl - a.total_pnl).slice(0, 5);
    const worstSlices = [...allSlices].sort((a, b) => a.total_pnl - b.total_pnl).slice(0, 5);

    const totalTrades = overall?.count ?? 0;
    const hasEnoughData = totalTrades >= 5;

    return (
        <AppLayout>
            <PageHeader
                title="Performance Attribution"
                subtitle="Which slices of your strategy actually make money — and which to stop trading."
                testid="analytics-header"
                action={
                    <button onClick={load} disabled={loading}
                        data-testid="analytics-refresh-button"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                        <RefreshCw className="w-3.5 h-3.5" /> {loading ? "LOADING…" : "REFRESH"}
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="analytics-error">{err}</div>}

                {/* Overall */}
                {overall && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-3">OVERALL PERFORMANCE · {totalTrades} CLOSED TRADES</div>
                        <div className="grid grid-cols-2 md:grid-cols-5 gap-3" data-testid="analytics-overall">
                            <HeadlineTile label="TRADES" value={overall.count} />
                            <HeadlineTile label="WIN RATE" value={`${overall.win_rate}%`} accent={overall.win_rate >= 50 ? "text-[#00FF41]" : "text-[#FFB000]"} />
                            <HeadlineTile label="TOTAL P&L" value={`${overall.total_pnl >= 0 ? "+" : ""}${overall.total_pnl}`} accent={pnlColor(overall.total_pnl)} />
                            <HeadlineTile label="AVG P&L" value={`${overall.avg_pnl >= 0 ? "+" : ""}${overall.avg_pnl}`} accent={pnlColor(overall.avg_pnl)} />
                            <HeadlineTile label="BEST · WORST" value={`+${overall.best} / ${overall.worst}`} />
                        </div>
                    </div>
                )}

                {!hasEnoughData && (
                    <div className="border border-[#FFB000]/30 bg-[#FFB000]/5 p-5 flex items-start gap-3" data-testid="analytics-empty-state">
                        <AlertTriangle className="w-5 h-5 text-[#FFB000] shrink-0 mt-0.5" />
                        <div>
                            <div className="font-display font-bold text-sm text-[#FFB000] mb-1">Not enough data yet</div>
                            <div className="text-sm text-[#A1A1AA] leading-relaxed">
                                Attribution analytics need at least <strong>5 closed trades</strong> to start surfacing patterns.
                                Right now you have {totalTrades}. Once the bot trades for a few days, this page will become genuinely useful —
                                spotting things like &ldquo;XAUUSD BUY at London Open with confidence ≥70% wins 78% of the time&rdquo;.
                            </div>
                        </div>
                    </div>
                )}

                {/* Per-session edge — Asia / London / Overlap / NY / Off-hours */}
                <SessionBreakdown sessions={sessions} />

                {/* Learned Meta Classifier */}
                <div className="border border-[#0099FF]/30 bg-[#0A0A0A]" data-testid="learned-meta-card">
                    <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                        <Cpu className="w-4 h-4 text-[#0099FF]" />
                        <div className="flex-1">
                            <div className="font-display font-bold text-sm tracking-tight">Learned Meta-Classifier</div>
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                                {learned?.trained ? "● TRAINED" : "○ NOT TRAINED"} · local logistic regression · p(win | features)
                            </div>
                        </div>
                        <button onClick={retrainLearned}
                            data-testid="learned-meta-retrain"
                            className="px-3 py-1.5 text-[10px] font-mono tracking-widest border border-[#0099FF]/40 text-[#0099FF] hover:bg-[#0099FF]/10">
                            RETRAIN
                        </button>
                    </div>
                    <div className="p-4">
                        {!learned?.trained && (
                            <div className="text-xs text-[#A1A1AA]">
                                {learned?.reason || "Classifier not yet trained. Needs at least 30 closed trades joined with their signals. Click RETRAIN to attempt training now."}
                            </div>
                        )}
                        {learned?.trained && (
                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 font-mono text-[11px]">
                                <div>
                                    <div className="text-[#52525B] text-[10px] tracking-widest">SAMPLES</div>
                                    <div className="text-white text-lg">{learned.n_samples}</div>
                                </div>
                                <div>
                                    <div className="text-[#52525B] text-[10px] tracking-widest">WIN RATE</div>
                                    <div className="text-[#00FF41] text-lg">{learned.n_wins != null ? `${Math.round(100 * learned.n_wins / learned.n_samples)}%` : "—"}</div>
                                </div>
                                <div>
                                    <div className="text-[#52525B] text-[10px] tracking-widest">TRAIN AUC</div>
                                    <div className={`text-lg ${learned.train_auc >= 0.7 ? "text-[#00FF41]" : learned.train_auc >= 0.6 ? "text-[#FFB000]" : "text-[#FF3B30]"}`}>
                                        {learned.train_auc?.toFixed(3)}
                                    </div>
                                </div>
                                <div>
                                    <div className="text-[#52525B] text-[10px] tracking-widest">VETO THRESHOLD</div>
                                    <div className="text-[#0099FF] text-lg">p &lt; {learned.threshold?.toFixed(2)}</div>
                                </div>
                                {learned.feature_names && (
                                    <div className="col-span-2 md:col-span-4 mt-1 text-[10px] text-[#52525B]">
                                        features: {learned.feature_names.join(", ")}
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                </div>


                {tune && tune.thresholds && tune.thresholds.length > 0 && (
                    <div className="border border-[#0099FF]/30 bg-[#0A0A0A]" data-testid="auto-tune-card">
                        <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                            <Gauge className="w-4 h-4 text-[#0099FF]" />
                            <div className="flex-1">
                                <div className="font-display font-bold text-sm tracking-tight">Auto-Tuned Confidence Thresholds</div>
                                <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                                    {tune.enabled ? "● ACTIVE" : "○ DISABLED"} · risk {tune.risk_level}
                                </div>
                            </div>
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-4">
                            {tune.thresholds.map((t) => (
                                <div key={t.symbol} className="border border-[#1F1F1F] bg-[#050505] p-3" data-testid={`auto-tune-${t.symbol}`}>
                                    <div className="flex items-center justify-between mb-2">
                                        <div className="font-mono text-sm font-medium">{t.symbol}</div>
                                        <div className={`font-mono text-[10px] px-2 py-0.5 border ${
                                            t.source === "auto_tuned"
                                                ? "border-[#0099FF]/40 text-[#0099FF] bg-[#0099FF]/10"
                                                : "border-[#52525B]/40 text-[#52525B]"
                                        }`}>
                                            {t.source === "auto_tuned" ? "AUTO-TUNED" : "PROFILE DEFAULT"}
                                        </div>
                                    </div>
                                    <div className="grid grid-cols-3 gap-2 font-mono text-[11px]">
                                        <div>
                                            <div className="text-[#52525B] text-[10px] tracking-widest">EFFECTIVE</div>
                                            <div className="text-[#0099FF] text-lg">{t.effective_threshold}%</div>
                                        </div>
                                        <div>
                                            <div className="text-[#52525B] text-[10px] tracking-widest">SUGGESTED</div>
                                            <div className="text-white text-lg">{t.suggested_threshold ?? "—"}{t.suggested_threshold ? "%" : ""}</div>
                                        </div>
                                        <div>
                                            <div className="text-[#52525B] text-[10px] tracking-widest">SAMPLES</div>
                                            <div className="text-white text-lg">{t.total_samples}</div>
                                        </div>
                                    </div>
                                    {/* Bucket breakdown */}
                                    <div className="mt-3 grid grid-cols-9 gap-px">
                                        {t.breakdown.map((b) => (
                                            <div key={b.bucket} className="text-center" title={`${b.bucket}+: ${b.count} trades · ${b.win_rate}% win · ${b.total_pnl} P&L`}>
                                                <div className={`h-6 flex items-end justify-center ${
                                                    b.count === 0 ? "bg-[#0A0A0A]"
                                                    : b.win_rate >= 55 ? "bg-[#00FF41]/20"
                                                    : b.win_rate >= 45 ? "bg-[#FFB000]/20"
                                                    : "bg-[#FF3B30]/20"
                                                }`}>
                                                    <div className={`w-full ${
                                                        b.win_rate >= 55 ? "bg-[#00FF41]"
                                                        : b.win_rate >= 45 ? "bg-[#FFB000]"
                                                        : "bg-[#FF3B30]"
                                                    }`} style={{ height: `${Math.min(100, b.win_rate)}%` }} />
                                                </div>
                                                <div className="font-mono text-[9px] text-[#52525B] mt-0.5">{b.bucket}</div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Best & Worst slices */}
                {hasEnoughData && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div className="border border-[#00FF41]/30 bg-[#0A0A0A]" data-testid="best-slices">
                            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                                <Trophy className="w-4 h-4 text-[#00FF41]" />
                                <div className="font-display font-bold text-sm tracking-tight">YOUR EDGE — Top 5 Profitable Slices</div>
                            </div>
                            <div className="divide-y divide-[#1F1F1F]">
                                {topSlices.map((s, i) => (
                                    <div key={`top-${i}-${s.key}`} className="px-4 py-3 flex items-center justify-between">
                                        <div className="min-w-0 flex-1">
                                            <div className="font-mono text-xs truncate">{s.key}</div>
                                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                                                {s.dim} · {s.count} trades · {s.win_rate}% win
                                            </div>
                                        </div>
                                        <div className={`font-mono text-sm font-medium ${pnlColor(s.total_pnl)}`}>
                                            {s.total_pnl >= 0 ? "+" : ""}{s.total_pnl}
                                        </div>
                                    </div>
                                ))}
                                {topSlices.length === 0 && <div className="px-4 py-3 font-mono text-xs text-[#52525B]">No slices with 3+ trades yet</div>}
                            </div>
                        </div>

                        <div className="border border-[#FF3B30]/30 bg-[#0A0A0A]" data-testid="worst-slices">
                            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                                <Target className="w-4 h-4 text-[#FF3B30]" />
                                <div className="font-display font-bold text-sm tracking-tight">STOP DOING — Bottom 5 Losing Slices</div>
                            </div>
                            <div className="divide-y divide-[#1F1F1F]">
                                {worstSlices.map((s, i) => (
                                    <div key={`bot-${i}-${s.key}`} className="px-4 py-3 flex items-center justify-between">
                                        <div className="min-w-0 flex-1">
                                            <div className="font-mono text-xs truncate">{s.key}</div>
                                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                                                {s.dim} · {s.count} trades · {s.win_rate}% win
                                            </div>
                                        </div>
                                        <div className={`font-mono text-sm font-medium ${pnlColor(s.total_pnl)}`}>
                                            {s.total_pnl >= 0 ? "+" : ""}{s.total_pnl}
                                        </div>
                                    </div>
                                ))}
                                {worstSlices.length === 0 && <div className="px-4 py-3 font-mono text-xs text-[#52525B]">No slices with 3+ trades yet</div>}
                            </div>
                        </div>
                    </div>
                )}

                {/* Dimensional breakdowns */}
                {data && (
                    <>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1 pt-4 flex items-center gap-2">
                            <BarChart3 className="w-3 h-3" /> DIMENSIONAL BREAKDOWNS
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <SliceTable title="By Symbol" testid="symbol" rows={data.by_symbol} subtitle="Which instruments are profitable" />
                            <SliceTable title="By Direction" testid="action" rows={data.by_action} subtitle="BUY vs SELL bias" />
                            <SliceTable title="By Session" testid="session" rows={data.by_session} subtitle="UTC trading session windows" />
                            <SliceTable title="By Confidence Bucket" testid="confidence" rows={data.by_confidence} subtitle="AI confidence at signal time" />
                            <SliceTable title="By Risk Profile" testid="risk_level" rows={data.by_risk_level} subtitle="Risk level when trade was taken" />
                            <SliceTable title="By Market Regime" testid="regime" rows={data.by_regime} subtitle="Regime adapter classification" />
                            <SliceTable title="By Day of Week" testid="dow" rows={data.by_day_of_week} />
                            <SliceTable title="By Hour (UTC)" testid="hour" rows={data.by_hour?.slice(0, 10)} subtitle="Top 10 hours by P&L" />
                            <SliceTable title="By Origin" testid="origin" rows={data.by_origin} subtitle="Auto vs manual vs test" />
                            <SliceTable title="By Close Reason" testid="close_reason" rows={data.by_close_reason} subtitle="SL / TP / manual" />
                            <SliceTable title="By Symbol × Session" testid="symbol_session" rows={data.by_symbol_session?.slice(0, 10)} subtitle="Top 10 combinations" />
                            <SliceTable title="By Symbol × Confidence" testid="symbol_conf" rows={data.by_symbol_confidence?.slice(0, 10)} subtitle="Top 10 combinations" />
                            <SliceTable title="By Profit Protection Active" testid="protection" rows={data.by_protection} subtitle="Did BE/PC/TRAIL fire?" />
                            <SliceTable title="By Account Mode" testid="mode" rows={data.by_mode} subtitle="Paper vs live" />
                        </div>
                    </>
                )}
            </div>
        </AppLayout>
    );
}
