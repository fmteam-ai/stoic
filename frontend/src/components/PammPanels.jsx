import { useState } from "react";
import { toast } from "sonner";
import api, { formatApiError } from "@/lib/api";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Activity, AlertTriangle, Loader2, Newspaper, Plus, RefreshCw, ShieldCheck, Wifi } from "lucide-react";

const box = "border border-[#1F1F1F] bg-[#0A0A0A]";
const label = "font-mono text-[10px] tracking-widest text-[#52525B]";
const btn = "px-3 py-1.5 text-[10px] font-mono tracking-widest border transition disabled:opacity-40";

export function KpiTile({ title, value, sub, tone = "text-white", testid }) {
    return (
        <div className={`${box} p-4`} data-testid={testid}>
            <div className={label}>{title}</div>
            <div className={`text-xl font-display font-bold mt-1 ${tone}`}>{value}</div>
            {sub && <div className="text-[10px] font-mono text-[#52525B] mt-0.5">{sub}</div>}
        </div>
    );
}

export function NavChart({ nav }) {
    const data = [...(nav || [])].reverse().map(n => ({ at: (n.at || "").slice(5, 16).replace("T", " "), nav: n.nav }));
    return (
        <div className={`${box} p-4`} data-testid="pamm-nav-chart">
            <div className={`${label} mb-2`}>NAV — BROKER AUTHORITATIVE</div>
            {data.length < 2 ? (
                <div className="text-xs font-mono text-[#52525B] py-8 text-center">Not enough NAV snapshots yet — run a reconcile to pull broker NAV.</div>
            ) : (
                <ResponsiveContainer width="100%" height={220}>
                    <AreaChart data={data}>
                        <defs>
                            <linearGradient id="navFill" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stopColor="#00FF41" stopOpacity={0.25} />
                                <stop offset="100%" stopColor="#00FF41" stopOpacity={0} />
                            </linearGradient>
                        </defs>
                        <XAxis dataKey="at" tick={{ fontSize: 9, fill: "#52525B", fontFamily: "monospace" }} />
                        <YAxis domain={["auto", "auto"]} tick={{ fontSize: 9, fill: "#52525B", fontFamily: "monospace" }} width={70} />
                        <Tooltip contentStyle={{ background: "#0A0A0A", border: "1px solid #1F1F1F", fontSize: 11, fontFamily: "monospace" }} />
                        <Area type="monotone" dataKey="nav" stroke="#00FF41" strokeWidth={1.5} fill="url(#navFill)" />
                    </AreaChart>
                </ResponsiveContainer>
            )}
        </div>
    );
}

const LIMIT_LABELS = {
    daily_loss_pct: "Daily loss cap %", weekly_loss_pct: "Weekly loss cap %",
    monthly_loss_pct: "Monthly loss cap %", max_drawdown_pct: "Max drawdown %",
    max_exposure_pct: "Max exposure %", max_correlated_positions: "Max correlated positions",
};

function LimitRow({ check, cfg, onCfg }) {
    return (
        <div className="grid grid-cols-12 items-center gap-2 px-3 py-2 border-b border-[#141414] text-xs" data-testid={`risk-limit-row-${check.limit}`}>
            <div className="col-span-4 text-[#A1A1AA]">{LIMIT_LABELS[check.limit] || check.limit}</div>
            <div className={`col-span-2 font-mono ${check.breached ? "text-[#FF3B30]" : "text-white"}`} data-testid={`risk-value-${check.limit}`}>
                {check.value === null || check.value === undefined ? "—" : check.value}
            </div>
            <div className="col-span-2">
                <input type="number" step="0.5" value={cfg.threshold ?? ""}
                    data-testid={`risk-threshold-${check.limit}`}
                    onChange={e => onCfg({ threshold: e.target.value })}
                    className="w-full bg-[#050505] border border-[#1F1F1F] px-2 py-1 font-mono text-xs text-white" />
            </div>
            <div className="col-span-2">
                <select value={cfg.action} onChange={e => onCfg({ action: e.target.value })}
                    data-testid={`risk-action-${check.limit}`}
                    className="w-full bg-[#050505] border border-[#1F1F1F] px-1 py-1 font-mono text-[10px] text-[#A1A1AA]">
                    <option value="halt">HALT</option>
                    <option value="flatten">FLATTEN</option>
                </select>
            </div>
            <div className="col-span-2 flex justify-end">
                <button onClick={() => onCfg({ enabled: !cfg.enabled })}
                    data-testid={`risk-toggle-${check.limit}`}
                    className={`${btn} ${cfg.enabled ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#1F1F1F] text-[#52525B]"}`}>
                    {cfg.enabled ? "ON" : "OFF"}
                </button>
            </div>
        </div>
    );
}

export function RiskPanel({ programId, riskStatus, limits, breach, onChanged, isAdmin }) {
    const [draft, setDraft] = useState(null);
    const [busy, setBusy] = useState(false);
    const cfgFor = k => (draft?.[k]) || limits?.[k] || {};

    const save = async () => {
        if (!draft) return;
        setBusy(true);
        try {
            await api.put(`/pamm/programs/${programId}/risk-limits`, draft);
            toast.success("Risk limits saved");
            setDraft(null);
            onChanged();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    const runCheck = async () => {
        setBusy(true);
        try {
            const r = await api.post(`/pamm/programs/${programId}/risk-check`);
            if (r.data.action_taken) toast.warning(`Risk breach — ${r.data.action_taken.toUpperCase()} executed`);
            else toast.success("Risk check passed — no breach");
            onChanged();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    const clearBreach = async () => {
        setBusy(true);
        try {
            await api.post(`/pamm/programs/${programId}/clear-risk-breach`);
            toast.success("Risk breach cleared");
            onChanged();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    const news = riskStatus?.news;

    return (
        <div className={box} data-testid="pamm-risk-panel">
            <div className="flex items-center justify-between px-4 py-3 border-b border-[#1F1F1F]">
                <div className="flex items-center gap-2">
                    <ShieldCheck className="w-4 h-4 text-[#00FF41]" />
                    <span className={label}>RISK ENGINE — MASTER ACCOUNT GATE</span>
                </div>
                <div className="flex gap-2">
                    {draft && <button onClick={save} disabled={busy} data-testid="risk-save-button" className={`${btn} border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10`}>SAVE LIMITS</button>}
                    <button onClick={runCheck} disabled={busy} data-testid="risk-run-check-button" className={`${btn} border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] hover:text-white`}>
                        {busy ? <Loader2 className="w-3 h-3 animate-spin inline" /> : "RUN RISK CHECK"}
                    </button>
                </div>
            </div>
            {breach && (
                <div className="mx-4 mt-3 border border-[#FF3B30]/40 bg-[#FF3B30]/10 px-3 py-2 flex items-center justify-between" data-testid="risk-breach-banner">
                    <div className="text-xs text-[#FF3B30] font-mono flex items-center gap-2">
                        <AlertTriangle className="w-3.5 h-3.5" />
                        BREACH: {(breach.limits || []).join(", ")} → {String(breach.action).toUpperCase()}{breach.flattened ? ` (${breach.flattened} closed)` : ""}
                    </div>
                    {isAdmin && <button onClick={clearBreach} disabled={busy} data-testid="risk-clear-breach-button" className={`${btn} border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10`}>CLEAR BREACH</button>}
                </div>
            )}
            <div className="grid grid-cols-12 gap-2 px-3 py-2 border-b border-[#141414]">
                {["LIMIT", "CURRENT", "THRESHOLD", "ON BREACH", ""].map((h, i) => (
                    <div key={h + i} className={`${label} ${[4, 2, 2, 2, 2][i] === 4 ? "col-span-4" : "col-span-2"} ${i === 4 ? "text-right" : ""}`}>{h}</div>
                ))}
            </div>
            {(riskStatus?.checks || []).map(c => (
                <LimitRow key={c.limit} check={c} cfg={cfgFor(c.limit)}
                    onCfg={patch => setDraft(d => ({ ...(d || limits), [c.limit]: { ...cfgFor(c.limit), ...patch } }))} />
            ))}
            <div className="px-4 py-3 flex items-start gap-2" data-testid="risk-news-filter">
                <Newspaper className="w-3.5 h-3.5 text-[#FFB000] mt-0.5 shrink-0" />
                <div className="text-xs font-mono">
                    {news?.active ? (
                        <span className="text-[#FFB000]">NEWS BLACKOUT ACTIVE — {news.event?.title} ({news.event?.country})</span>
                    ) : news?.enabled ? (
                        <span className="text-[#52525B]">
                            News filter armed ({cfgFor("news_filter").blackout_before_min}m before / {cfgFor("news_filter").blackout_after_min}m after high-impact).
                            {news?.upcoming?.length ? ` Next: ${news.upcoming[0].title} (${news.upcoming[0].country}) in ${news.upcoming[0].starts_in_min}m` : " No high-impact events in the next 24h."}
                            {news?.feed_source === "unavailable" && " — feed unavailable"}
                        </span>
                    ) : <span className="text-[#52525B]">News filter disabled.</span>}
                </div>
            </div>
        </div>
    );
}

export function BrokerHealthWidget({ partners, onPing, pinging }) {
    return (
        <div className={box} data-testid="pamm-broker-health">
            <div className="flex items-center justify-between px-4 py-3 border-b border-[#1F1F1F]">
                <div className="flex items-center gap-2">
                    <Wifi className="w-4 h-4 text-[#00FF41]" />
                    <span className={label}>BROKER HEALTH</span>
                </div>
                <button onClick={onPing} disabled={pinging} data-testid="broker-health-ping-button"
                    className={`${btn} border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] hover:text-white flex items-center gap-1.5`}>
                    {pinging ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} PING
                </button>
            </div>
            {(partners || []).map(p => {
                const h = p.health || {};
                const tone = h.status === "healthy" ? "text-[#00FF41]" : h.status === "degraded" ? "text-[#FFB000]" : "text-[#FF3B30]";
                return (
                    <div key={p.partner_id} className="px-4 py-3 border-b border-[#141414] last:border-b-0" data-testid={`broker-health-${p.partner_id}`}>
                        <div className="flex items-center justify-between">
                            <div className="text-sm text-white">{p.name} <span className="text-[10px] font-mono text-[#52525B]">({p.adapter})</span></div>
                            <div className={`font-mono text-lg font-bold ${tone}`} data-testid={`broker-health-score-${p.partner_id}`}>
                                {h.score !== undefined ? h.score : "—"}
                            </div>
                        </div>
                        <div className="flex items-center justify-between mt-1">
                            <span className={`font-mono text-[10px] tracking-widest ${tone}`}>{(h.status || "unchecked").toUpperCase()}</span>
                            <span className="font-mono text-[10px] text-[#52525B]">{h.latency_ms !== undefined ? `${h.latency_ms}ms` : ""}</span>
                        </div>
                        <div className="flex gap-0.5 mt-2 items-end h-5">
                            {[...(p.history || [])].reverse().map((c, i) => (
                                <div key={i} title={`${c.latency_ms}ms ${c.ok ? "OK" : "FAIL"}`}
                                    className={`w-2 ${c.ok ? "bg-[#00FF41]/60" : "bg-[#FF3B30]"}`}
                                    style={{ height: `${Math.max(15, Math.min(100, (c.latency_ms || 0) / 10))}%` }} />
                            ))}
                            {!(p.history || []).length && <span className="font-mono text-[10px] text-[#52525B]">No heartbeats recorded — hit PING.</span>}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

export function InvestorsPanel({ programId, allocations, onChanged }) {
    const [form, setForm] = useState({ name: "", email: "", amount: "" });
    const [busy, setBusy] = useState(false);
    const add = async () => {
        setBusy(true);
        try {
            await api.post(`/pamm/programs/${programId}/investors`, { ...form, amount: parseFloat(form.amount) });
            toast.success("Investor allocated (broker-side)");
            setForm({ name: "", email: "", amount: "" });
            onChanged();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };
    return (
        <div className={box} data-testid="pamm-investors-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F]"><span className={label}>INVESTORS · ALLOCATIONS (BROKER MIRROR)</span></div>
            <div className="px-4 py-3 flex gap-2 border-b border-[#141414]">
                <input placeholder="Name" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                    data-testid="investor-name-input" className="flex-1 bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs text-white" />
                <input placeholder="Email" value={form.email} onChange={e => setForm(f => ({ ...f, email: e.target.value }))}
                    data-testid="investor-email-input" className="flex-1 bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs text-white" />
                <input placeholder="Amount" type="number" value={form.amount} onChange={e => setForm(f => ({ ...f, amount: e.target.value }))}
                    data-testid="investor-amount-input" className="w-24 bg-[#050505] border border-[#1F1F1F] px-2 py-1.5 text-xs text-white" />
                <button onClick={add} disabled={busy || !form.amount} data-testid="investor-add-button"
                    className={`${btn} border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10 flex items-center gap-1`}>
                    <Plus className="w-3 h-3" /> ADD
                </button>
            </div>
            <div className="max-h-56 overflow-y-auto">
                {(allocations || []).map(a => (
                    <div key={a.broker_allocation_id} className="px-4 py-2 border-b border-[#141414] last:border-b-0 flex justify-between text-xs font-mono">
                        <span className="text-[#A1A1AA]">{a.investor_id}</span>
                        <span className="text-white">{Number(a.amount).toLocaleString()}</span>
                        <span className="text-[#52525B]">{(a.at || "").slice(0, 16).replace("T", " ")}</span>
                    </div>
                ))}
                {!(allocations || []).length && <div className="px-4 py-6 text-center text-xs font-mono text-[#52525B]">No allocations yet.</div>}
            </div>
        </div>
    );
}

export function JoinRequestsPanel({ programId, requests, onChanged }) {
    const [busy, setBusy] = useState(null);
    const decide = async (rid, decision) => {
        setBusy(rid);
        try {
            await api.post(`/pamm/join-requests/${rid}/${decision}`);
            toast.success(decision === "approve" ? "Approved — allocation created on broker" : "Request rejected");
            onChanged();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(null); }
    };
    const pending = (requests || []).filter(r => r.status === "pending");
    const decided = (requests || []).filter(r => r.status !== "pending");
    return (
        <div className={box} data-testid="pamm-join-requests-panel">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center justify-between">
                <span className={label}>JOIN REQUESTS</span>
                {pending.length > 0 && <span className="font-mono text-[10px] px-1.5 py-0.5 border border-[#FFB000]/40 text-[#FFB000]" data-testid="join-requests-pending-count">{pending.length} PENDING</span>}
            </div>
            {pending.map(r => (
                <div key={r.request_id} className="px-4 py-2.5 border-b border-[#141414]" data-testid={`join-request-${r.request_id}`}>
                    <div className="flex justify-between text-xs">
                        <span className="text-white truncate">{r.email}</span>
                        <span className="font-mono text-[#00FF41]">{Number(r.amount).toLocaleString()}</span>
                    </div>
                    {r.note && <div className="font-mono text-[10px] text-[#52525B] mt-0.5 truncate">"{r.note}"</div>}
                    <div className="flex gap-2 mt-2">
                        <button onClick={() => decide(r.request_id, "approve")} disabled={busy === r.request_id}
                            data-testid={`join-approve-${r.request_id}`}
                            className={`${btn} flex-1 border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10`}>APPROVE</button>
                        <button onClick={() => decide(r.request_id, "reject")} disabled={busy === r.request_id}
                            data-testid={`join-reject-${r.request_id}`}
                            className={`${btn} flex-1 border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10`}>REJECT</button>
                    </div>
                </div>
            ))}
            {!pending.length && <div className="px-4 py-4 text-center text-xs font-mono text-[#52525B]">No pending requests.</div>}
            {decided.slice(0, 5).map(r => (
                <div key={r.request_id} className="px-4 py-1.5 border-t border-[#141414] flex justify-between text-[10px] font-mono">
                    <span className="text-[#52525B] truncate">{r.email}</span>
                    <span className={r.status === "approved" ? "text-[#00FF41]" : "text-[#FF3B30]"}>{r.status.toUpperCase()}</span>
                </div>
            ))}
        </div>
    );
}

export function SweepChip({ sweep }) {
    if (!sweep?.at) return (
        <span className="px-2 py-1 font-mono text-[10px] tracking-widest border border-[#1F1F1F] text-[#52525B]" data-testid="pamm-sweep-chip">
            AUTO-SWEEP: WAITING FOR FIRST RUN
        </span>
    );
    const ageS = Math.max(0, Math.round((Date.now() - new Date(sweep.at).getTime()) / 1000));
    const fresh = ageS < 180;
    return (
        <span className={`px-2 py-1 font-mono text-[10px] tracking-widest border ${fresh ? "border-[#00FF41]/40 text-[#00FF41]" : "border-[#FFB000]/40 text-[#FFB000]"}`}
            data-testid="pamm-sweep-chip"
            title={`Programs checked: ${sweep.programs_checked} · breaches enforced: ${sweep.breaches?.length || 0}`}>
            AUTO-SWEEP {ageS}s AGO · {sweep.programs_checked} CHECKED
        </span>
    );
}

export function EventsFeed({ events }) {
    return (
        <div className={box} data-testid="pamm-events-feed">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Activity className="w-4 h-4 text-[#00FF41]" />
                <span className={label}>EVENT STREAM</span>
            </div>
            <div className="max-h-72 overflow-y-auto">
                {(events || []).map(e => (
                    <div key={e.event_id} className="px-4 py-2 border-b border-[#141414] last:border-b-0">
                        <div className="flex justify-between text-xs">
                            <span className="font-mono text-white">{e.type}</span>
                            <span className="font-mono text-[10px] text-[#52525B]">{(e.at || "").slice(5, 19).replace("T", " ")}</span>
                        </div>
                        <div className="font-mono text-[10px] text-[#52525B] truncate">{JSON.stringify(e.data)}</div>
                    </div>
                ))}
                {!(events || []).length && <div className="px-4 py-6 text-center text-xs font-mono text-[#52525B]">No events yet.</div>}
            </div>
        </div>
    );
}
