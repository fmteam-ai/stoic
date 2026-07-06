import { useEffect, useState, useCallback } from "react";
import { Link, useSearchParams } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import {
    RefreshCw, FlaskConical, AlertTriangle, Brain, TrendingDown, ShieldCheck,
    Activity, ChevronRight, Calendar,
} from "lucide-react";

function MeasureRow({ m, i }) {
    const ev = m.evidence;
    const prio = (m.priority || "medium").toUpperCase();
    const prioColor = prio === "HIGH" ? "#FF3B30" : prio === "LOW" ? "#52525B" : "#FFB000";
    return (
        <div className="border border-[#1F1F1F] bg-black/40 p-3" data-testid={`review-measure-${i}`}>
            <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono text-[10px] tracking-widest px-1.5 py-0.5 border"
                      style={{ borderColor: `${prioColor}66`, color: prioColor }}>{prio}</span>
                <span className="font-display font-bold text-sm">{m.title}</span>
                {ev?.testable && (
                    <span className={`font-mono text-xs ml-auto ${ev.net_effect >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"}`}
                          title={`Shadow-tested over your last ${ev.shadow_days} days of real trades`}>
                        net {ev.net_effect >= 0 ? "+" : "-"}${Math.abs(ev.net_effect).toFixed(2)}
                    </span>
                )}
            </div>
            <div className="text-xs text-[#A1A1AA] leading-relaxed mt-1">{m.rationale}</div>
            {ev?.testable ? (
                <div className="font-mono text-[10px] text-[#52525B] tracking-wider mt-2">
                    SHADOW TEST · {ev.trades_blocked} trades blocked · saves ${ev.losses_avoided.toFixed(0)} in losses · misses ${ev.wins_missed.toFixed(0)} in wins ({ev.shadow_days}d replay)
                </div>
            ) : (
                <div className="font-mono text-[10px] text-[#52525B] tracking-wider mt-2">NOT MECHANICALLY SHADOW-TESTABLE — judgement call</div>
            )}
        </div>
    );
}

function ReviewCard({ r }) {
    const agg = r.aggregates || {};
    return (
        <div className="border border-[#0099FF]/30 bg-[#0A0A0A]" data-testid="loss-review-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2 flex-wrap">
                <Brain className="w-4 h-4 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Auto Loss Review</span>
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest">
                    {new Date(r.created_at).toLocaleString()} · {r.trigger === "auto" ? "AUTOMATIC" : "MANUAL"}
                </span>
                <span className="font-mono text-xs text-[#FF3B30] ml-auto">
                    {agg.losses} losses · -${Math.abs(agg.loss_pnl || 0).toFixed(2)} ({agg.window_days}d)
                </span>
            </div>
            <div className="p-4 space-y-3">
                {r.diagnosis && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">DIAGNOSIS</div>
                        <div className="text-sm text-white leading-relaxed">{r.diagnosis}</div>
                    </div>
                )}
                {r.market_context && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">CURRENT MARKET CONTEXT</div>
                        <div className="text-xs text-[#A1A1AA] leading-relaxed">{r.market_context}</div>
                    </div>
                )}
                {(r.measures || []).length > 0 && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-2">SUGGESTED MEASURES · RANKED BY SHADOW-TESTED NET EFFECT</div>
                        <div className="space-y-2">
                            {r.measures.map((m, i) => <MeasureRow key={i} m={m} i={i} />)}
                        </div>
                    </div>
                )}
                <div className="font-mono text-[10px] text-[#52525B] tracking-wider">
                    MEASURES ARE NEVER AUTO-APPLIED — apply the ones you trust via Bot Config.
                </div>
            </div>
        </div>
    );
}

function pnlColor(v) {
    if (v == null || v === 0) return "text-[#A1A1AA]";
    return v < 0 ? "text-[#FF3B30]" : "text-[#00FF41]";
}

function PatternRow({ p, onSelect, selected }) {
    return (
        <button onClick={() => onSelect(p.pattern_key)}
            data-testid={`pattern-${p.pattern_key.replace(/\|/g, "-")}`}
            className={`w-full text-left px-4 py-3 border-l-4 hover:bg-[#1F1F1F]/30 transition-colors ${
                selected ? "border-l-[#FF3B30] bg-[#FF3B30]/5" : "border-l-transparent"
            }`}>
            <div className="flex items-center gap-2">
                <span className="font-mono text-xs text-white truncate flex-1">{p.pattern_key}</span>
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest shrink-0">
                    {p.count}× · {p.total_pnl >= 0 ? "+" : ""}${p.total_pnl}
                </span>
                <ChevronRight className="w-3.5 h-3.5 text-[#52525B] shrink-0" />
            </div>
            {p.last_narrative && (
                <div className="text-[11px] text-[#A1A1AA] mt-1 line-clamp-2 leading-snug">
                    {p.last_narrative}
                </div>
            )}
        </button>
    );
}

function PostmortemCard({ pm }) {
    const n = pm.narrative || {};
    const d = pm.diff || {};
    const adj = pm.adjustment;
    return (
        <div className="border border-[#FF3B30]/30 bg-[#0A0A0A]" data-testid={`postmortem-${pm.trade_id}`}>
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-start justify-between gap-3 flex-wrap">
                <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-mono text-sm text-white">{pm.symbol} · {pm.action}</span>
                        <span className={`font-mono text-xs ${pnlColor(pm.pnl)}`}>
                            {pm.pnl >= 0 ? "+$" : "-$"}{Math.abs(pm.pnl).toFixed(2)}
                        </span>
                        <span className="font-mono text-[10px] tracking-widest px-1.5 py-0.5 border border-[#FF3B30]/40 text-[#FF3B30]">
                            {pm.trigger === "sl_hit" ? "SL HIT" : "CONSECUTIVE LOSS"}
                        </span>
                        {adj && (
                            <span className="font-mono text-[10px] tracking-widest px-1.5 py-0.5 border border-[#FFB000]/40 text-[#FFB000]"
                                  title={`Auto-tightened min_confidence ${adj.from}→${adj.to}`}>
                                AUTO-TIGHTENED
                            </span>
                        )}
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-1">
                        TRADE {pm.trade_id?.slice(-6)} · {new Date(pm.created_at).toLocaleString()}
                    </div>
                </div>
                <Link to={`/trades?id=${pm.trade_id}`} className="font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-white">
                    OPEN TRADE →
                </Link>
            </div>

            <div className="p-4 space-y-3 text-sm leading-relaxed">
                {n.summary && (
                    <div className="text-white">
                        <span className="font-mono text-[10px] text-[#52525B] tracking-widest mr-2">TL;DR</span>
                        {n.summary}
                    </div>
                )}
                {n.why_it_looked_good && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">WHY IT LOOKED GOOD</div>
                        <div className="text-[#A1A1AA]">{n.why_it_looked_good}</div>
                    </div>
                )}
                {n.what_actually_happened && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">WHAT HAPPENED</div>
                        <div className="text-[#A1A1AA]">{n.what_actually_happened}</div>
                    </div>
                )}
                {n.what_changed && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">WHAT CHANGED</div>
                        <div className="text-[#A1A1AA]">{n.what_changed}</div>
                    </div>
                )}
                {Array.isArray(n.lessons) && n.lessons.length > 0 && (
                    <div>
                        <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">LESSONS</div>
                        <ul className="space-y-1">
                            {n.lessons.map((l, i) => (
                                <li key={i} className="text-[#A1A1AA] flex items-start gap-2">
                                    <span className="text-[#FF3B30] mt-1">→</span><span>{l}</span>
                                </li>
                            ))}
                        </ul>
                    </div>
                )}
                {n.suggested_guardrail && (
                    <div className="p-3 border border-[#FFB000]/30 bg-[#FFB000]/5">
                        <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-1 flex items-center gap-1">
                            <ShieldCheck className="w-3 h-3" /> SUGGESTED GUARDRAIL
                        </div>
                        <div className="text-[#FFB000] text-sm">{n.suggested_guardrail}</div>
                    </div>
                )}
                {/* Quantitative diff */}
                <details className="border-t border-[#1F1F1F] pt-3">
                    <summary className="font-mono text-[10px] text-[#52525B] tracking-widest cursor-pointer hover:text-[#A1A1AA]">
                        QUANTITATIVE DIFF — ENTRY vs NOW
                    </summary>
                    <div className="grid grid-cols-2 md:grid-cols-3 gap-2 mt-3 text-xs">
                        {[
                            ["Regime", d.regime_at_entry],
                            ["Session entry", d.session_at_entry],
                            ["Session now", d.session_now],
                            ["Macro freeze (entry)", d.macro_freeze_at_entry == null ? null : String(d.macro_freeze_at_entry)],
                            ["Macro gate (now)", d.macro_gate_open_now == null ? null : String(d.macro_gate_open_now)],
                            ["Sentiment entry", d.sentiment_at_entry],
                            ["Sentiment now", d.sentiment_now],
                            ["Sentiment flipped", String(d.sentiment_flipped)],
                            ["MTF aligned (entry)", d.mtf_aligned_at_entry == null ? null : String(d.mtf_aligned_at_entry)],
                            ["A+ passed (entry)", d.aplus_passed_at_entry == null ? null : String(d.aplus_passed_at_entry)],
                            ["Confidence (entry)", d.confidence_at_entry],
                            ["DXY entry / now", `${d.dxy_at_entry ?? "—"} / ${d.dxy_now ?? "—"}`],
                            ["VIX (now)", d.vix_now],
                            ["10Y yield (now)", d.y10y_now],
                        ].filter(([_, v]) => v != null && v !== "null").map(([k, v]) => (
                            <div key={k} className="border border-[#1F1F1F] bg-[#0A0A0A] p-2">
                                <div className="font-mono text-[9px] text-[#52525B] tracking-widest">{k.toUpperCase()}</div>
                                <div className="font-mono text-xs text-[#A1A1AA] truncate">{String(v)}</div>
                            </div>
                        ))}
                    </div>
                </details>
            </div>
        </div>
    );
}

export default function LossLab() {
    const [searchParams] = useSearchParams();
    const focusTradeId = searchParams.get("trade");
    const [patterns, setPatterns] = useState([]);
    const [postmortems, setPostmortems] = useState([]);
    const [settings, setSettings] = useState(null);
    const [adjustments, setAdjustments] = useState([]);
    const [selectedKey, setSelectedKey] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState("");
    const [regenerating, setRegenerating] = useState(false);
    const [reviews, setReviews] = useState([]);
    const [runningReview, setRunningReview] = useState(false);

    const runReview = async () => {
        setRunningReview(true);
        try {
            const { data } = await api.post("/postmortem/reviews/run");
            if (data.review) {
                setReviews(prev => [data.review, ...prev]);
            } else {
                setErr(data.message || "No losses to analyse.");
            }
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setRunningReview(false);
        }
    };

    const load = useCallback(async () => {
        setLoading(true); setErr("");
        try {
            const [pats, pms, st, adj, rev] = await Promise.all([
                api.get("/postmortem/patterns"),
                api.get("/postmortem"),
                api.get("/postmortem/settings"),
                api.get("/postmortem/adjustments"),
                api.get("/postmortem/reviews"),
            ]);
            setPatterns(pats.data.items || []);
            setPostmortems(pms.data.items || []);
            setSettings(st.data);
            setAdjustments(adj.data.items || []);
            setReviews(rev.data.reviews || []);
        } catch (e) {
            setErr(formatApiError(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    // If linked from /trades?trade=<id>, scroll the corresponding card into view
    // once data has loaded. Also auto-run regenerate if no pm exists for it yet.
    useEffect(() => {
        if (!focusTradeId || loading) return;
        const existing = postmortems.find(p => p.trade_id === focusTradeId);
        if (existing) {
            const el = document.querySelector(`[data-testid="postmortem-${focusTradeId}"]`);
            if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
        } else if (!regenerating) {
            // No pm yet — kick off generation
            setRegenerating(true);
            api.post(`/postmortem/${focusTradeId}/regenerate`)
                .then(() => load())
                .catch(e => setErr(formatApiError(e)))
                .finally(() => setRegenerating(false));
        }
    }, [focusTradeId, loading, postmortems.length]);

    const toggleAutoTighten = async () => {
        try {
            const next = !settings?.auto_tighten_enabled;
            await api.post("/postmortem/settings", { auto_tighten_enabled: next });
            setSettings(s => ({ ...s, auto_tighten_enabled: next }));
        } catch (e) {
            setErr(formatApiError(e));
        }
    };

    const filteredPms = selectedKey
        ? postmortems.filter(p => p.pattern_key === selectedKey)
        : postmortems;

    return (
        <AppLayout>
            <PageHeader
                title="Loss Lab"
                subtitle="Every losing trade investigated — why it failed and how to stop it happening again."
                testid="loss-lab-header"
                action={
                    <button onClick={load} disabled={loading}
                        data-testid="loss-lab-refresh"
                        className="flex items-center gap-2 px-3 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs font-mono tracking-widest transition-colors">
                        <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
                        {loading ? "LOADING…" : "REFRESH"}
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-6">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono">{err}</div>}

                {regenerating && (
                    <div className="border border-[#0099FF]/30 bg-[#0099FF]/5 px-4 py-3 text-xs text-[#0099FF] font-mono flex items-center gap-2"
                         data-testid="postmortem-regenerating">
                        <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                        INVESTIGATING TRADE {focusTradeId?.slice(-6)} — Claude is analysing market conditions (typically 10-15s)…
                    </div>
                )}

                {/* iter-54 — Auto Loss Review */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 flex items-start gap-3" data-testid="auto-review-card">
                    <Brain className="w-5 h-5 shrink-0 mt-0.5 text-[#0099FF]" />
                    <div className="flex-1">
                        <div className="font-display font-bold text-sm">Auto Loss Review</div>
                        <div className="text-xs text-[#A1A1AA] leading-relaxed mt-1">
                            The bot automatically re-analyses <strong>all losses of the last 7 days</strong> whenever
                            3+ new losses accumulate (max once/24h): Claude diagnoses the root cause against current
                            market data, proposes counter-measures, and each measure is <strong>shadow-tested against
                            your last 14 days of real trades</strong> — showing exactly how much it would have saved
                            vs. how many wins it would have cost. Delivered here + Telegram. Never auto-applied.
                        </div>
                    </div>
                    <button onClick={runReview} disabled={runningReview}
                        data-testid="run-loss-review"
                        className="px-3 py-1.5 border border-[#0099FF]/40 text-[#0099FF] hover:bg-[#0099FF]/10 text-[10px] font-mono tracking-widest transition-colors disabled:opacity-50 flex items-center gap-1.5">
                        <RefreshCw className={`w-3 h-3 ${runningReview ? "animate-spin" : ""}`} />
                        {runningReview ? "ANALYSING…" : "RUN NOW"}
                    </button>
                </div>
                {reviews.length > 0 && <ReviewCard r={reviews[0]} />}

                {/* Auto-tighten toggle */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 flex items-start gap-3"
                     data-testid="auto-tighten-card">
                    <FlaskConical className={`w-5 h-5 shrink-0 mt-0.5 ${settings?.auto_tighten_enabled ? "text-[#00FF41]" : "text-[#52525B]"}`} />
                    <div className="flex-1">
                        <div className="font-display font-bold text-sm">Auto-Tighten Guardrails</div>
                        <div className="text-xs text-[#A1A1AA] leading-relaxed mt-1">
                            When a losing pattern recurs <strong>3+ times in 30 days</strong>, the bot raises
                            <code className="px-1 mx-1 bg-[#1F1F1F] text-[#A1A1AA]">min_confidence_override</code>
                            by 5 (clamped 50–95). Pattern-level 7-day cooldown prevents over-correction.
                            Adjustments are reversible from Bot Config.
                        </div>
                    </div>
                    <button onClick={toggleAutoTighten}
                        data-testid="auto-tighten-toggle"
                        className={`px-3 py-1.5 border text-[10px] font-mono tracking-widest transition-colors ${
                            settings?.auto_tighten_enabled
                                ? "border-[#00FF41]/40 bg-[#00FF41]/10 text-[#00FF41]"
                                : "border-[#52525B] text-[#A1A1AA] hover:border-white hover:text-white"
                        }`}>
                        {settings?.auto_tighten_enabled ? "● ENABLED" : "○ DISABLED"}
                    </button>
                </div>

                {/* Empty state */}
                {patterns.length === 0 && postmortems.length === 0 && (
                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6 text-center" data-testid="loss-lab-empty">
                        <AlertTriangle className="w-8 h-8 text-[#FFB000] mx-auto mb-3" />
                        <div className="font-display font-bold text-base mb-1">No post-mortems yet</div>
                        <div className="text-sm text-[#A1A1AA] leading-relaxed max-w-md mx-auto">
                            The bot auto-investigates every losing trade that hits its stop or follows another loss.
                            Once you have a few, this page will surface the patterns and propose guardrail tweaks.
                        </div>
                    </div>
                )}

                {(patterns.length > 0 || postmortems.length > 0) && (
                    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                        {/* Pattern aggregation */}
                        <div className="lg:col-span-1 space-y-3">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest flex items-center gap-2">
                                <Brain className="w-3 h-3" /> RECURRING PATTERNS · 30-DAY
                            </div>
                            <div className="border border-[#1F1F1F] bg-[#0A0A0A] divide-y divide-[#1F1F1F]"
                                 data-testid="pattern-list">
                                {patterns.length === 0 && (
                                    <div className="px-4 py-3 text-xs text-[#52525B] font-mono">NO PATTERNS YET</div>
                                )}
                                {selectedKey && (
                                    <button onClick={() => setSelectedKey(null)}
                                        className="w-full px-4 py-2 text-left text-[10px] font-mono tracking-widest text-[#A1A1AA] hover:text-white">
                                        ← CLEAR FILTER
                                    </button>
                                )}
                                {patterns.map(p => (
                                    <PatternRow key={p.pattern_key} p={p}
                                        onSelect={setSelectedKey}
                                        selected={p.pattern_key === selectedKey} />
                                ))}
                            </div>

                            {/* Recent auto-tighten audit */}
                            {adjustments.length > 0 && (
                                <>
                                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest flex items-center gap-2 mt-4">
                                        <ShieldCheck className="w-3 h-3" /> AUTO-ADJUSTMENTS
                                    </div>
                                    <div className="border border-[#1F1F1F] bg-[#0A0A0A] divide-y divide-[#1F1F1F]"
                                         data-testid="adjustments-list">
                                        {adjustments.map(a => (
                                            <div key={a.id} className="px-4 py-3">
                                                <div className="font-mono text-xs">{a.pattern_key}</div>
                                                <div className="font-mono text-[10px] text-[#A1A1AA] mt-1">
                                                    {a.field}: <span className="text-[#FFB000]">{a.from} → {a.to}</span>
                                                    {" · "}{a.trigger_count} losses
                                                </div>
                                                <div className="font-mono text-[9px] text-[#52525B] mt-0.5">
                                                    {new Date(a.created_at).toLocaleString()}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </>
                            )}
                        </div>

                        {/* Post-mortem details */}
                        <div className="lg:col-span-2 space-y-4">
                            <div className="font-mono text-[10px] text-[#52525B] tracking-widest flex items-center gap-2">
                                <TrendingDown className="w-3 h-3" />
                                {selectedKey ? `PATTERN · ${selectedKey}` : "ALL POST-MORTEMS"}
                                {" · "} {filteredPms.length} INVESTIGATIONS
                            </div>
                            {filteredPms.length === 0 && (
                                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 text-xs text-[#52525B] font-mono">
                                    No post-mortems for the current filter.
                                </div>
                            )}
                            {filteredPms.map(pm => <PostmortemCard key={pm.id} pm={pm} />)}
                        </div>
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
