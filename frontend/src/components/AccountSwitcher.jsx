import { useEffect, useRef, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { Layers, ChevronDown, Settings2, Power } from "lucide-react";
import { toast } from "sonner";

/** iter-137 · Global account switcher — lives in the quick-actions bar.
 *  Lists every account with live equity + connection dot, per-account
 *  trading kill-switch, and jumps straight to that account's bot config. */
export function AccountSwitcher() {
    const [data, setData] = useState(null);
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState({});
    const ref = useRef(null);
    const navigate = useNavigate();

    const load = useCallback(async () => {
        try { const { data: d } = await api.get("/accounts/overview"); setData(d); } catch { /* silent */ }
    }, []);

    useEffect(() => {
        load();
        const t = setInterval(load, 30_000);
        return () => clearInterval(t);
    }, [load]);

    useEffect(() => {
        const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
        document.addEventListener("mousedown", onDoc);
        return () => document.removeEventListener("mousedown", onDoc);
    }, []);

    const toggleTrading = async (e, acct) => {
        e.stopPropagation();
        setBusy(prev => ({ ...prev, [acct.id]: true }));
        try {
            await api.patch(`/accounts/${acct.id}`, { trading_enabled: !acct.trading_enabled });
            toast.success(acct.trading_enabled
                ? `Trading DISABLED on ${acct.label}`
                : `Trading ENABLED on ${acct.label}`);
            await load();
        } catch (err) {
            toast.error(formatApiError(err));
        } finally {
            setBusy(prev => ({ ...prev, [acct.id]: false }));
        }
    };

    if (!data || (data.totals?.accounts ?? 0) === 0) return null;
    const t = data.totals;

    return (
        <div ref={ref} className="relative hidden md:block" data-testid="account-switcher">
            <button onClick={() => setOpen(o => !o)}
                data-testid="account-switcher-button"
                title="Multi-account manager — equity, connection & trading state per account"
                className="flex items-center gap-1.5 font-mono text-[10px] tracking-widest px-2.5 py-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:border-[#00BFFF]/40 hover:text-[#00BFFF] transition-colors">
                <Layers className="w-3 h-3" />
                <span data-testid="acct-switcher-counts"
                    title={`${t.trading_enabled}/${t.accounts} accounts enabled · ${t.connected} EA${t.connected === 1 ? "" : "s"} connected (fresh heartbeat)`}>
                    {t.trading_enabled}/{t.accounts} ON · {t.connected} EA
                </span>
                <ChevronDown className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} />
            </button>

            {open && (
                <div className="absolute right-0 top-full mt-1.5 w-[340px] bg-[#0A0A0A] border border-[#1F1F1F] shadow-2xl z-50"
                    data-testid="account-switcher-panel">
                    <div className="px-4 py-2.5 border-b border-[#1F1F1F] flex items-center justify-between">
                        <span className="font-mono text-[9px] text-[#52525B] tracking-widest">TOTAL EQUITY</span>
                        <span className="font-display font-bold text-sm tabular-nums">
                            ${t.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                        </span>
                    </div>
                    <div className="max-h-72 overflow-y-auto">
                        {data.accounts.map(a => (
                            <div key={a.id}
                                onClick={() => { setOpen(false); navigate(`/bot-config?account=${a.id}`); }}
                                data-testid={`switcher-account-${a.account_number}`}
                                className="px-4 py-2.5 border-b border-[#1F1F1F] last:border-b-0 flex items-center gap-3 cursor-pointer hover:bg-[#111111] transition-colors">
                                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${a.connected ? "bg-[#00FF41]" : "bg-[#52525B]"}`} />
                                <div className="min-w-0 flex-1">
                                    <div className="flex items-center gap-1.5">
                                        <span className="font-display font-bold text-xs truncate">{a.label}</span>
                                        {a.group && (
                                            <span className="font-mono text-[8px] tracking-widest px-1 py-px border border-[#00BFFF]/40 text-[#00BFFF] shrink-0">
                                                {a.group.toUpperCase()}
                                            </span>
                                        )}
                                    </div>
                                    <div className="font-mono text-[9px] text-[#52525B] tracking-widest truncate">
                                        {a.broker || (a.mode === "paper" ? "PAPER" : "")} · #{a.account_number}
                                    </div>
                                </div>
                                <div className="text-right shrink-0">
                                    <div className="font-display font-bold text-xs tabular-nums">
                                        {a.equity == null ? "—" : `$${a.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
                                    </div>
                                    <div className="font-mono text-[9px] tabular-nums"
                                        style={{ color: a.pnl_today > 0 ? "#00FF41" : a.pnl_today < 0 ? "#FF3B30" : "#52525B" }}>
                                        {a.pnl_today >= 0 ? "+" : "−"}${Math.abs(a.pnl_today).toFixed(0)} TODAY
                                    </div>
                                </div>
                                <button onClick={(e) => toggleTrading(e, a)} disabled={!!busy[a.id]}
                                    data-testid={`switcher-toggle-${a.account_number}`}
                                    title={a.trading_enabled ? "Trading ON — click to disable this account" : "Trading OFF — click to enable"}
                                    className={`p-1.5 border shrink-0 transition-colors disabled:opacity-40 ${
                                        a.trading_enabled
                                            ? "border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10"
                                            : "border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10"
                                    }`}>
                                    <Power className="w-3 h-3" />
                                </button>
                            </div>
                        ))}
                    </div>
                    <button onClick={() => { setOpen(false); navigate("/accounts"); }}
                        data-testid="switcher-manage-accounts"
                        className="w-full px-4 py-2 flex items-center justify-center gap-1.5 font-mono text-[9px] tracking-widest text-[#A1A1AA] hover:text-[#00FF41] border-t border-[#1F1F1F] transition-colors">
                        <Settings2 className="w-3 h-3" /> MANAGE ACCOUNTS
                    </button>
                </div>
            )}
        </div>
    );
}
