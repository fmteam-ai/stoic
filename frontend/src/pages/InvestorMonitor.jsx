import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Eye, Loader2, RefreshCw, Store } from "lucide-react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { NavChart } from "@/components/PammPanels";
import { AllocationsList, MasterKpis, MasterTradesTable, MonitorOnlyBanner, MyShareCard, ProgramStatusStrip, fmtPct, toneOf } from "@/components/InvestorPanels";

const btn = "px-3 py-1.5 text-[10px] font-mono tracking-widest border transition disabled:opacity-40 flex items-center gap-1.5";

function ProgramPicker({ programs, selected, onSelect }) {
    return (
        <div className="flex flex-wrap gap-2" data-testid="investor-program-picker">
            {programs.map(r => {
                const p = r.program;
                const on = p.program_id === selected;
                return (
                    <button key={p.program_id} onClick={() => onSelect(p.program_id)} data-testid={`investor-program-tab-${p.program_id}`}
                        className={`px-3 py-2 border text-left transition ${on ? "border-[#FFB000]/60 bg-[#FFB000]/5" : "border-[#1F1F1F] hover:border-[#333333]"}`}>
                        <div className="font-display font-bold text-xs text-white">{p.name}</div>
                        <div className={`font-mono text-[9px] mt-0.5 ${toneOf(r.performance?.total_return_pct)}`}>{fmtPct(r.performance?.total_return_pct)} · share {r.share?.share_pct != null ? `${r.share.share_pct.toFixed(2)}%` : "—"}</div>
                    </button>
                );
            })}
        </div>
    );
}

function EmptyState() {
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-8 text-center space-y-3" data-testid="investor-empty-state">
            <Eye className="w-6 h-6 text-[#52525B] mx-auto" />
            <div className="font-display font-bold text-white">No mirrored programs yet</div>
            <p className="text-xs text-[#A1A1AA] max-w-md mx-auto">This monitor lights up once you hold a PAMM investor account linked to a program, or a marketplace join request is approved by the manager.</p>
            <div className="flex justify-center gap-2 flex-wrap">
                <Link to="/marketplace" className={`${btn} border-[#00BFFF]/40 text-[#00BFFF] hover:bg-[#00BFFF]/10`} data-testid="investor-empty-marketplace-link"><Store className="w-3 h-3" /> BROWSE MARKETPLACE</Link>
                <Link to="/accounts" className={`${btn} border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]`} data-testid="investor-empty-accounts-link">LINK INVESTOR ACCOUNT</Link>
            </div>
        </div>
    );
}

export default function InvestorMonitor() {
    const [params, setParams] = useSearchParams();
    const [programs, setPrograms] = useState(null);
    const [selected, setSelected] = useState(params.get("program") || "");
    const [view, setView] = useState(null);
    const [loadingView, setLoadingView] = useState(false);

    const loadPrograms = useCallback(async () => {
        try {
            const r = await api.get("/pamm/investor/programs");
            const rows = r.data.programs || [];
            setPrograms(rows);
            if (rows.length && !rows.some(x => x.program.program_id === selected)) setSelected(rows[0].program.program_id);
        } catch (e) { toast.error(formatApiError(e)); setPrograms([]); }
    }, [selected]);

    const loadView = useCallback(async (pid) => {
        if (!pid) return;
        setLoadingView(true);
        try { setView((await api.get(`/pamm/investor/programs/${pid}`)).data); }
        catch (e) { toast.error(formatApiError(e)); setView(null); }
        finally { setLoadingView(false); }
    }, []);

    useEffect(() => { loadPrograms(); }, []);
    useEffect(() => {
        if (!selected) return;
        loadView(selected);
        setParams(prev => { const n = new URLSearchParams(prev); n.set("program", selected); return n; }, { replace: true });
    }, [selected, loadView, setParams]);

    const p = view?.program;
    return (
        <AppLayout>
            <PageHeader title="Investor Monitor" subtitle="Master program performance and your mirrored share — read-only by design." testid="investor-monitor-header"
                action={<button onClick={() => { loadPrograms(); loadView(selected); }} className={`${btn} border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]`} data-testid="investor-refresh-button"><RefreshCw className="w-3 h-3" /> REFRESH</button>} />
            <div className="p-4 md:p-8 space-y-4" data-testid="investor-monitor-page">
                <MonitorOnlyBanner reason={view?.execution_authority?.reason} />
                {programs === null ? (
                    <div className="flex items-center gap-2 font-mono text-xs text-[#52525B]" data-testid="investor-loading"><Loader2 className="w-3 h-3 animate-spin" /> Loading mirrored programs…</div>
                ) : !programs.length ? <EmptyState /> : (<>
                    <ProgramPicker programs={programs} selected={selected} onSelect={setSelected} />
                    {loadingView && !view && <div className="font-mono text-xs text-[#52525B]">Loading program…</div>}
                    {view && p && (
                        <div className="space-y-4" data-testid="investor-program-view">
                            <ProgramStatusStrip program={p} tradingAllowed={view.trading_allowed} blockReason={view.trading_block_reason} />
                            <MasterKpis program={p} performance={view.performance} />
                            <div className="grid lg:grid-cols-3 gap-4">
                                <div className="lg:col-span-2 space-y-4">
                                    <NavChart nav={[...(view.nav || [])].reverse()} />
                                    <MasterTradesTable trades={view.master_trades?.recent_closed} openPositions={view.master_trades?.open_positions} currency={p.currency} />
                                </div>
                                <div className="space-y-4">
                                    <MyShareCard share={view.share} program={p} linkedAccounts={view.linked_accounts} />
                                    <AllocationsList allocations={view.my_allocations} currency={p.currency} />
                                </div>
                            </div>
                        </div>
                    )}
                </>)}
            </div>
        </AppLayout>
    );
}
