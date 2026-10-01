import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { BrokerHealthWidget, ChangeRequestsPanel, EventsFeed, FlattenFailedBanner, InvestorsPanel, JoinRequestsPanel, KpiTile, NavChart, OpStateControl, PositionTruthWidget, RiskPanel, SweepChip, VerdictTester } from "@/components/PammPanels";
import { PammStrategyPanel } from "@/components/PammStrategyPanel";
import { PammCertificationPanel } from "@/components/PammCertificationPanel";
import { Globe, Loader2, OctagonAlert, Pause, Play, Plus, RefreshCw } from "lucide-react";

const btn = "px-3 py-1.5 text-[10px] font-mono tracking-widest border transition disabled:opacity-40 flex items-center gap-1.5";

function ProgramBar({ programs, selected, onSelect, onCreate, busy }) {
    const [name, setName] = useState("");
    return (
        <div className="flex flex-wrap items-center gap-2" data-testid="pamm-program-bar">
            <select value={selected || ""} onChange={e => onSelect(e.target.value)}
                data-testid="pamm-program-select"
                className="bg-[#0A0A0A] border border-[#1F1F1F] px-3 py-2 text-sm text-white font-mono min-w-[220px]">
                {!programs.length && <option value="">No programs yet</option>}
                {programs.map(p => <option key={p.program_id} value={p.program_id}>{p.name}</option>)}
            </select>
            <input placeholder="New program name" value={name} onChange={e => setName(e.target.value)}
                data-testid="pamm-new-program-input"
                className="bg-[#050505] border border-[#1F1F1F] px-3 py-2 text-xs text-white w-48" />
            <button onClick={() => { onCreate(name); setName(""); }} disabled={busy || !name.trim()}
                data-testid="pamm-create-program-button"
                className={`${btn} border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10`}>
                <Plus className="w-3 h-3" /> CREATE
            </button>
        </div>
    );
}

function Controls({ program, allowed, reason, act, busy, publish }) {
    const paused = program.trading === "paused";
    return (
        <div className="flex flex-wrap items-center gap-2" data-testid="pamm-controls">
            <span className={`px-2 py-1 font-mono text-[10px] tracking-widest border ${allowed
                ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10"
                : "border-[#FF3B30]/40 text-[#FF3B30] bg-[#FF3B30]/10"}`}
                data-testid="pamm-trading-status">
                {allowed ? "TRADING ALLOWED" : `BLOCKED: ${reason}`}
            </span>
            {paused ? (
                <button onClick={() => act("resume")} disabled={busy} data-testid="pamm-resume-button"
                    className={`${btn} border-[#00FF41]/40 text-[#00FF41] hover:bg-[#00FF41]/10`}><Play className="w-3 h-3" /> RESUME</button>
            ) : (
                <button onClick={() => act("pause")} disabled={busy} data-testid="pamm-pause-button"
                    className={`${btn} border-[#FFB000]/40 text-[#FFB000] hover:bg-[#FFB000]/10`}><Pause className="w-3 h-3" /> PAUSE</button>
            )}
            {program.emergency_stop ? (
                <button onClick={() => act("clear-emergency-stop")} disabled={busy} data-testid="pamm-clear-estop-button"
                    className={`${btn} border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10`}>CLEAR E-STOP</button>
            ) : (
                <button onClick={() => act("emergency-stop", { reason: "manual from dashboard" })} disabled={busy} data-testid="pamm-estop-button"
                    className={`${btn} border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10`}><OctagonAlert className="w-3 h-3" /> E-STOP</button>
            )}
            <button onClick={() => act("reconcile")} disabled={busy} data-testid="pamm-reconcile-button"
                className={`${btn} border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] hover:text-white`}>
                {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />} RECONCILE
            </button>
            <button onClick={publish} disabled={busy} data-testid="pamm-publish-button"
                className={`${btn} ${program.published
                    ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10"
                    : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#52525B] hover:text-white"}`}>
                <Globe className="w-3 h-3" /> {program.published ? "PUBLISHED" : "PUBLISH TO MARKETPLACE"}
            </button>
        </div>
    );
}

export default function ManagedStrategy() {
    const { user } = useAuth();
    const [programs, setPrograms] = useState([]);
    const [selected, setSelected] = useState(null);
    const [detail, setDetail] = useState(null);
    const [nav, setNav] = useState([]);
    const [navProvenance, setNavProvenance] = useState(null);
    const [allocations, setAllocations] = useState([]);
    const [riskStatus, setRiskStatus] = useState(null);
    const [limits, setLimits] = useState(null);
    const [health, setHealth] = useState([]);
    const [events, setEvents] = useState([]);
    const [joinRequests, setJoinRequests] = useState([]);
    const [changeRequests, setChangeRequests] = useState([]);
    const [sweep, setSweep] = useState(null);
    const [busy, setBusy] = useState(false);
    const [pinging, setPinging] = useState(false);

    const loadPrograms = useCallback(async () => {
        try {
            const r = await api.get("/pamm/programs");
            setPrograms(r.data.programs || []);
            setSelected(s => s || r.data.programs?.[0]?.program_id || null);
        } catch (e) { toast.error(formatApiError(e)); }
    }, []);

    const loadDetail = useCallback(async (pid) => {
        if (!pid) return;
        try {
            const [d, n, a, rs, rl, ev, jr, cr] = await Promise.all([
                api.get(`/pamm/programs/${pid}`),
                api.get(`/pamm/programs/${pid}/nav`),
                api.get(`/pamm/programs/${pid}/allocations`),
                api.get(`/pamm/programs/${pid}/risk-status`),
                api.get(`/pamm/programs/${pid}/risk-limits`),
                api.get(`/pamm/events?program_id=${pid}`),
                api.get(`/pamm/programs/${pid}/join-requests`),
                api.get(`/pamm/programs/${pid}/change-requests`),
            ]);
            setDetail(d.data); setNav(n.data.nav || []); setNavProvenance(n.data.provenance || null);
            setAllocations(a.data.allocations || []);
            setRiskStatus(rs.data); setLimits(rl.data.risk_limits);
            setEvents(ev.data.events || []);
            setJoinRequests(jr.data.requests || []);
            setChangeRequests(cr.data.requests || []);
        } catch (e) { toast.error(formatApiError(e)); }
    }, []);

    const loadHealth = useCallback(async () => {
        try {
            const [h, sw] = await Promise.all([
                api.get("/pamm/health"), api.get("/pamm/sweep-status")]);
            setHealth(h.data.partners || []);
            setSweep(sw.data);
        } catch { /* non-fatal */ }
    }, []);

    useEffect(() => { loadPrograms(); loadHealth(); }, [loadPrograms, loadHealth]);
    useEffect(() => { loadDetail(selected); }, [selected, loadDetail]);

    const createProgram = async (name) => {
        setBusy(true);
        try {
            const r = await api.post("/pamm/programs", { name });
            toast.success("Program provisioned on broker");
            await loadPrograms();
            setSelected(r.data.program_id);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const act = async (action, payload = {}) => {
        setBusy(true);
        try {
            await api.post(`/pamm/programs/${selected}/${action}`, payload);
            toast.success(action.replace(/-/g, " ").toUpperCase());
            await loadDetail(selected);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const publish = async () => {
        setBusy(true);
        try {
            await api.post(`/pamm/programs/${selected}/publish`,
                { publish: !p.published });
            toast.success(p.published ? "Removed from marketplace" : "Published to marketplace");
            await loadDetail(selected);
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const ping = async () => {
        setPinging(true);
        try { await api.post("/pamm/health/check"); await loadHealth(); }
        catch (e) { toast.error(formatApiError(e)); }
        finally { setPinging(false); }
    };

    const p = detail?.program;
    const perf = detail?.performance || {};
    const fmt = v => (v === null || v === undefined) ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

    return (
        <AppLayout>
            <div className="space-y-5" data-testid="managed-strategy-page">
                <PageHeader title="Managed Strategy"
                    subtitle="Broker-hosted PAMM — the broker owns money, STOIC owns strategy, risk veto and reporting."
                    testid="managed-strategy-header" />
                <ProgramBar programs={programs} selected={selected} onSelect={setSelected}
                    onCreate={createProgram} busy={busy} />
                {!p && <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-10 text-center font-mono text-xs text-[#52525B]" data-testid="pamm-empty-state">
                    Create your first managed program above — it will be provisioned on the sandbox broker.
                </div>}
                {p && (<>
                    <FlattenFailedBanner incident={p.flatten_failed} />
                    <div className="flex flex-wrap items-center gap-2">
                        <Controls program={p} allowed={detail.trading_allowed} reason={detail.trading_block_reason} act={act} busy={busy} publish={publish} />
                        <OpStateControl program={p} onChanged={() => loadDetail(selected)} />
                        <SweepChip sweep={sweep} />
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
                        <KpiTile title="AUM" value={fmt(p.aum)} sub={p.currency} testid="kpi-aum" />
                        <KpiTile title="NAV" value={fmt(p.last_nav?.nav)} sub={p.last_nav?.at?.slice(0, 16).replace("T", " ")} testid="kpi-nav" />
                        <KpiTile title="TOTAL RETURN" value={`${fmt(perf.total_return_pct)}%`}
                            tone={(perf.total_return_pct || 0) >= 0 ? "text-[#00FF41]" : "text-[#FF3B30]"} testid="kpi-return" />
                        <KpiTile title="MAX DRAWDOWN" value={`${fmt(perf.max_drawdown_pct)}%`} tone="text-[#FFB000]" testid="kpi-drawdown" />
                        <KpiTile title="INVESTORS" value={p.investor_count ?? 0} testid="kpi-investors" />
                        <KpiTile title="MANAGER FEE" value={`${fmt(p.manager_fee_pct)}%`} sub="performance" testid="kpi-fee" />
                    </div>
                    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                        <div className="lg:col-span-2 space-y-4">
                            <NavChart nav={nav} provenance={navProvenance} />
                            <VerdictTester programId={selected} />
                            <RiskPanel programId={selected} riskStatus={riskStatus} limits={limits}
                                breach={p.risk_breach || riskStatus?.risk_breach}
                                onChanged={() => loadDetail(selected)} isAdmin={user?.role === "admin"} />
                        </div>
                        <div className="space-y-4">
                            <PammStrategyPanel programId={selected} onChanged={() => loadDetail(selected)} />
                            <PammCertificationPanel programId={selected} isAdmin={user?.role === "admin"} />
                            <PositionTruthWidget programId={selected} isAdmin={user?.role === "admin"} />
                            <ChangeRequestsPanel requests={changeRequests} meId={user?.id} onChanged={() => loadDetail(selected)} />
                            <BrokerHealthWidget partners={health} onPing={ping} pinging={pinging} />
                            <JoinRequestsPanel programId={selected} requests={joinRequests} onChanged={() => loadDetail(selected)} />
                            <InvestorsPanel programId={selected} allocations={allocations} onChanged={() => loadDetail(selected)} />
                            <EventsFeed events={events} />
                        </div>
                    </div>
                </>)}
            </div>
        </AppLayout>
    );
}
