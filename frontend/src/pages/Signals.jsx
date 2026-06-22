import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { Brain, ArrowUp, ArrowDown, Pause, Zap as Lightning, Trash2 as Trash, ShieldCheck } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";
import { toast } from "sonner";

function ActionPill({ action }) {
    if (action === "BUY") return <span className="font-mono text-[10px] tracking-widest px-2 py-1 bg-[#00FF41]/10 text-[#00FF41] border border-[#00FF41]/30 inline-flex items-center gap-1"><ArrowUp className="w-3 h-3" /> BUY</span>;
    if (action === "SELL") return <span className="font-mono text-[10px] tracking-widest px-2 py-1 bg-[#FF3B30]/10 text-[#FF3B30] border border-[#FF3B30]/30 inline-flex items-center gap-1"><ArrowDown className="w-3 h-3" /> SELL</span>;
    return <span className="font-mono text-[10px] tracking-widest px-2 py-1 bg-[#1F1F1F] text-[#A1A1AA] border border-[#1F1F1F] inline-flex items-center gap-1"><Pause className="w-3 h-3" /> HOLD</span>;
}

function ConfBar({ value, threshold }) {
    const v = Math.min(100, Math.max(0, value));
    const meets = v >= threshold;
    return (
        <div className="w-full">
            <div className="flex justify-between text-[10px] font-mono mb-1">
                <span className="text-[#52525B] tracking-widest">CONFIDENCE</span>
                <span className={meets ? "text-[#00FF41]" : "text-[#FFB000]"}>{v}% / {threshold}% req</span>
            </div>
            <div className="h-1 bg-[#1F1F1F] relative">
                <div className={`absolute top-0 left-0 h-full ${meets ? "bg-[#00FF41]" : "bg-[#FFB000]"}`} style={{ width: `${v}%` }} />
                <div className="absolute top-0 h-full w-px bg-white/40" style={{ left: `${threshold}%` }} />
            </div>
        </div>
    );
}

function SignalCard({ s, accounts, onExecute, onDelete }) {
    const [accId, setAccId] = useState(accounts[0]?.id || "");
    useEffect(() => { if (!accId && accounts[0]) setAccId(accounts[0].id); }, [accounts, accId]);
    const tradeable = s.tradeable;
    const sentScore = s.sentiment?.score ?? 0;
    const sentLabel = s.sentiment?.label;
    const sentClass = sentScore > 0.2 ? "text-[#00FF41]" : sentScore < -0.2 ? "text-[#FF3B30]" : "text-[#A1A1AA]";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 space-y-3" data-testid={`signal-card-${s.symbol}`}>
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <ActionPill action={s.action} />
                    <span className="font-mono text-sm tracking-widest">{s.symbol}</span>
                    {s.origin === "auto" && (
                        <span className="font-mono text-[10px] tracking-widest px-1.5 py-0.5 bg-[#FFB000]/10 text-[#FFB000] border border-[#FFB000]/30">AUTO</span>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">RISK · {s.risk_level?.toUpperCase()}</span>
                    <button onClick={() => onDelete(s.id)} data-testid={`signal-delete-${s.id}`} className="p-1 text-[#52525B] hover:text-[#FF3B30] transition-colors">
                        <Trash className="w-3.5 h-3.5" />
                    </button>
                </div>
            </div>

            <ConfBar value={s.confidence || 0} threshold={s.min_confidence_required || 65} />

            {s.veto_applied && (
                <div className="bg-[#FFB000]/10 border border-[#FFB000]/30 px-3 py-2 flex items-start gap-2">
                    <ShieldCheck className="w-4 h-4 text-[#FFB000] shrink-0 mt-0.5" />
                    <div className="text-xs">
                        <div className="font-mono text-[10px] text-[#FFB000] tracking-widest mb-0.5">DUAL-AI VETO APPLIED</div>
                        <div className="text-[#A1A1AA]">Chart said {s.chart_action} but news sentiment disagreed — forced to HOLD.</div>
                    </div>
                </div>
            )}

            <div className="grid grid-cols-3 gap-2 pt-2">
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">ENTRY</div>
                    <div className="font-mono text-sm">{s.entry_price}</div>
                </div>
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">STOP LOSS</div>
                    <div className="font-mono text-sm text-[#FF3B30]">{s.stop_loss}</div>
                </div>
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">TAKE PROFIT</div>
                    <div className="font-mono text-sm text-[#00FF41]">{s.take_profit}</div>
                </div>
            </div>

            {sentLabel && (
                <div className="pt-2 border-t border-[#1F1F1F]">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">NEWS SENTIMENT</div>
                    <div className="flex items-center gap-2 text-xs">
                        <span className={`font-mono ${sentClass}`}>{sentLabel.replace("_", " ").toUpperCase()} · {sentScore >= 0 ? "+" : ""}{sentScore}</span>
                        <span className="text-[#52525B] font-mono">· {s.sentiment.article_count} articles</span>
                    </div>
                </div>
            )}

            {s.reasoning && (
                <div className="pt-2 border-t border-[#1F1F1F]">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1.5">AI REASONING</div>
                    <p className="text-xs text-[#A1A1AA] leading-relaxed whitespace-pre-wrap">{s.reasoning}</p>
                </div>
            )}

            {Array.isArray(s.key_factors) && s.key_factors.length > 0 && (
                <ul className="flex flex-wrap gap-1.5">
                    {s.key_factors.map((f, i) => (
                        <li key={i} className="font-mono text-[10px] text-[#A1A1AA] bg-[#121212] border border-[#1F1F1F] px-2 py-0.5">{f}</li>
                    ))}
                </ul>
            )}

            <div className="pt-3 border-t border-[#1F1F1F] flex items-center gap-2 flex-wrap">
                {tradeable && accounts.length > 0 ? (
                    <>
                        <select value={accId} onChange={e => setAccId(e.target.value)}
                            data-testid={`signal-account-select-${s.symbol}`}
                            className="bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] px-2 py-1.5 text-xs flex-1 min-w-0 outline-none">
                            {accounts.map(a => <option key={a.id} value={a.id}>{a.label} · #{a.account_number}</option>)}
                        </select>
                        <button onClick={() => onExecute(s.id, accId)}
                            disabled={s.consumed}
                            data-testid={`signal-execute-${s.symbol}`}
                            className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-40 disabled:cursor-not-allowed text-black font-medium px-3 py-1.5 text-xs flex items-center gap-1 transition-colors">
                            <Lightning className="w-3 h-3" /> {s.consumed ? "EXECUTED" : "EXECUTE"}
                        </button>
                    </>
                ) : (
                    <div className="font-mono text-[10px] text-[#FFB000] tracking-widest">
                        {!tradeable ? "BELOW CONFIDENCE THRESHOLD" : "NO ACCOUNT CONFIGURED"}
                    </div>
                )}
            </div>
        </div>
    );
}

export default function Signals() {
    const [signals, setSignals] = useState([]);
    const [accounts, setAccounts] = useState([]);
    const [loading, setLoading] = useState(true);
    const [generating, setGenerating] = useState(false);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");

    const load = useCallback(async () => {
        try {
            const [s, a] = await Promise.all([api.get("/signals"), api.get("/accounts")]);
            setSignals(s.data);
            setAccounts(a.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    // Live stream: auto-add new signals + toast on auto-trades
    const { lastEvent } = useLiveStream();
    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "signal_created") {
            setSignals(prev => [lastEvent.payload, ...prev].slice(0, 50));
        } else if (lastEvent.type === "trade_created" && lastEvent.payload?.origin === "auto") {
            toast.success(`Auto-trade · ${lastEvent.payload.symbol} ${lastEvent.payload.action}`, {
                description: `${lastEvent.payload.lot_size} lots queued for MT5`,
            });
            // Mark signal as consumed
            const sigId = lastEvent.payload.signal_id;
            if (sigId) setSignals(prev => prev.map(s => s.id === sigId ? { ...s, consumed: true } : s));
        }
    }, [lastEvent]);

    const handleGenerate = async () => {
        setGenerating(true); setErr(""); setMsg("");
        try {
            const { data } = await api.post("/signals/generate-all", {});
            setMsg(`Generated ${data.generated.length} signal(s).`);
            await load();
        } catch (e) { setErr(formatApiError(e)); }
        finally { setGenerating(false); }
    };

    const handleExecute = async (signalId, accountId) => {
        setErr(""); setMsg("");
        try {
            await api.post(`/trades/execute/${signalId}`, { account_id: accountId });
            setMsg("Trade queued for execution. Open MT5 EA to fulfill.");
            await load();
        } catch (e) { setErr(formatApiError(e)); }
    };

    const handleDelete = async (id) => {
        try { await api.delete(`/signals/${id}`); await load(); } catch (e) { setErr(formatApiError(e)); }
    };

    return (
        <AppLayout>
            <PageHeader
                title="AI Signals"
                subtitle="Claude-generated trading signals across your configured symbols."
                testid="signals-header"
                action={
                    <button onClick={handleGenerate} disabled={generating}
                        data-testid="signals-generate-button"
                        className="flex items-center gap-2 px-4 py-2 bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium text-xs tracking-widest transition-colors duration-150">
                        <Brain className="w-4 h-4" /> {generating ? "ANALYSING…" : "GENERATE SIGNALS"}
                    </button>
                }
            />

            <div className="p-4 md:p-8 space-y-4">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="signals-error">{err}</div>}
                {msg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-4 py-2 text-xs text-[#00FF41] font-mono">{msg}</div>}

                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING SIGNALS…</div>
                ) : signals.length === 0 ? (
                    <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="signals-empty">
                        <Brain className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                        <div className="font-display font-bold text-lg mb-1">No signals yet</div>
                        <div className="text-sm text-[#A1A1AA] mb-4">Click <em>Generate Signals</em> to let Claude analyse your configured markets.</div>
                    </div>
                ) : (
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                        {signals.map(s => (
                            <SignalCard key={s.id} s={s} accounts={accounts} onExecute={handleExecute} onDelete={handleDelete} />
                        ))}
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
