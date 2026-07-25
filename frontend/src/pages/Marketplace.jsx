import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { toast } from "sonner";
import { Store, Star, RefreshCw, Download, Check } from "lucide-react";

const fmtPnl = (v) => v == null ? "—" : `${v >= 0 ? "+$" : "-$"}${Math.abs(v).toFixed(2)}`;
const pnlCls = (v) => v > 0 ? "text-[#00FF41]" : v < 0 ? "text-[#FF3B30]" : "text-[#A1A1AA]";

const RISK_CLS = {
    CONSERVATIVE: "text-[#00FF41] border-[#00FF41]/40",
    MODERATE: "text-[#0099FF] border-[#0099FF]/40",
    ACTIVE: "text-[#FFD700] border-[#FFD700]/40",
    AGGRESSIVE: "text-[#FF3B30] border-[#FF3B30]/40",
    ADAPTIVE: "text-[#A78BFA] border-[#A78BFA]/40",
};

function Stars({ n }) {
    if (n == null) return <span className="font-mono text-[9px] text-[#3F3F46]">UNRATED</span>;
    return (
        <span className="flex gap-0.5">
            {[1, 2, 3, 4, 5].map(i => (
                <Star key={i} size={11} className={i <= n ? "text-[#FFD700] fill-[#FFD700]" : "text-[#27272A]"} />
            ))}
        </span>
    );
}

function StrategyCard({ s, accounts, onInstalled }) {
    const [installing, setInstalling] = useState(false);
    const [pickAccount, setPickAccount] = useState(false);
    const p = s.performance;

    const install = async (accountId) => {
        setInstalling(true);
        try {
            await api.post(`/bot/preset/${s.key}${accountId ? `?account_id=${accountId}` : ""}`);
            toast.success(`${s.label} installed${accountId ? "" : " (default scope)"}`);
            setPickAccount(false);
            onInstalled();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setInstalling(false); }
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4 flex flex-col" data-testid={`market-card-${s.key}`}>
            <div className="flex items-start justify-between gap-2">
                <div>
                    <div className="font-display font-bold text-sm text-white">{s.label}</div>
                    <div className="font-mono text-[9px] text-[#52525B] mt-0.5">{s.creator} · {s.engine || "adaptive"}</div>
                </div>
                <Stars n={s.stars} />
            </div>
            <div className="font-mono text-[10px] text-[#71717A] mt-2">{s.tagline}</div>
            <div className="flex flex-wrap gap-1.5 mt-2.5">
                <span className={`font-mono text-[9px] px-1.5 py-0.5 border ${RISK_CLS[s.risk_class] || RISK_CLS.MODERATE}`}>{s.risk_class}</span>
                {s.verified && <span className="font-mono text-[9px] px-1.5 py-0.5 border border-[#00FF41]/40 text-[#00FF41]">VERIFIED</span>}
            </div>
            {p ? (
                <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 mt-3 border-t border-[#141414] pt-2.5">
                    <div className="font-mono text-[9px] text-[#52525B]">P&L (90D)<div className={`text-[11px] ${pnlCls(p.pnl)}`}>{fmtPnl(p.pnl)}</div></div>
                    <div className="font-mono text-[9px] text-[#52525B]">PROFIT FACTOR<div className={`text-[11px] ${(p.profit_factor ?? 0) >= 1.2 ? "text-[#00FF41]" : "text-[#FFD700]"}`}>{p.profit_factor ?? "∞"}</div></div>
                    <div className="font-mono text-[9px] text-[#52525B]">WIN RATE<div className="text-[11px] text-white">{p.win_rate}% ({p.trades})</div></div>
                    <div className="font-mono text-[9px] text-[#52525B]">MAX DRAWDOWN<div className="text-[11px] text-[#FF3B30]">${p.max_drawdown_usd}</div></div>
                </div>
            ) : (
                <div className="font-mono text-[9px] text-[#3F3F46] mt-3 border-t border-[#141414] pt-2.5">
                    No verified trades on this engine yet — performance appears after 3+ closed trades.
                </div>
            )}
            <div className="mt-2.5 space-y-1">
                <div className="font-mono text-[9px] text-[#52525B]">BEST FOR · <span className="text-[#A1A1AA]">{s.regime_fit}</span></div>
                {s.best_broker && (
                    <div className="font-mono text-[9px] text-[#52525B]">BEST BROKER · <span className="text-[#0099FF]">{s.best_broker.label}</span> ({s.best_broker.score})</div>
                )}
                <div className="font-mono text-[9px] text-[#52525B]">RECOMMENDED · <span className="text-[#A1A1AA]">{s.recommended_capital}</span></div>
            </div>
            {s.installed_on?.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2" data-testid={`market-installed-${s.key}`}>
                    {s.installed_on.map((l, i) => (
                        <span key={i} className="flex items-center gap-1 font-mono text-[9px] px-1.5 py-0.5 border border-[#00FF41]/30 text-[#00FF41]">
                            <Check size={9} /> {l}
                        </span>
                    ))}
                </div>
            )}
            <div className="mt-auto pt-3">
                {!pickAccount ? (
                    <button onClick={() => setPickAccount(true)} data-testid={`market-install-${s.key}`}
                        className="w-full flex items-center justify-center gap-1.5 font-mono text-[10px] tracking-widest px-3 py-1.5 border border-[#0099FF]/40 text-[#0099FF] hover:bg-[#0099FF]/10">
                        <Download size={11} /> INSTALL
                    </button>
                ) : (
                    <div className="space-y-1.5" data-testid={`market-pick-${s.key}`}>
                        <div className="font-mono text-[9px] text-[#52525B] tracking-widest">INSTALL ON:</div>
                        {accounts.map((a) => (
                            <button key={a.id} onClick={() => install(a.id)} disabled={installing}
                                data-testid={`market-install-${s.key}-${a.id}`}
                                className="w-full font-mono text-[10px] px-2 py-1 border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#0099FF]/40 hover:text-white text-left disabled:opacity-40">
                                {a.label || a.login || a.id}
                            </button>
                        ))}
                        <button onClick={() => setPickAccount(false)} className="w-full font-mono text-[9px] text-[#52525B] hover:text-white py-0.5">CANCEL</button>
                    </div>
                )}
            </div>
        </div>
    );
}

export default function Marketplace() {
    const [data, setData] = useState(null);
    const [accounts, setAccounts] = useState([]);
    const [err, setErr] = useState("");
    const [loading, setLoading] = useState(true);

    const load = useCallback(async () => {
        setErr("");
        try {
            const [m, a] = await Promise.all([
                api.get("/marketplace/strategies"),
                api.get("/accounts"),
            ]);
            setData(m.data);
            setAccounts(a.data || []);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);
    useEffect(() => { load(); }, [load]);

    return (
        <AppLayout>
            <PageHeader
                title="Strategy Marketplace"
                subtitle="Install a verified strategy on any account — performance is measured from real closed trades, never simulated claims"
                icon={Store}
            />
            <div className="px-4 md:px-8 py-5">
                {err && <div className="font-mono text-xs text-[#FF3B30] mb-3" data-testid="market-error">{err}</div>}
                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] flex items-center gap-2">
                        <RefreshCw size={12} className="animate-spin" /> Loading strategies…
                    </div>
                ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4" data-testid="market-grid">
                        {(data?.strategies || []).map((s) => (
                            <StrategyCard key={s.key} s={s} accounts={accounts} onInstalled={load} />
                        ))}
                    </div>
                )}
            </div>
        </AppLayout>
    );
}
