/* AdaptiveModePanel — dashboard card surfacing the iter-74 Win-Rate
   Adaptive Mode subsystem (profit_taking_mode, max_tp_pips, rolling
   adaptive risk, regime-aware auto-preset).

   For each account it shows:
     · profit-taking mode (with quick switcher)
     · TP cap pips per symbol
     · live adaptive risk multiplier + rolling win rate
     · auto-preset state + which preset it *would* pick right now

   All toggles call PUT /api/bot/config?account_id=… so the changes
   land on the per-account bot_config (default cfg is left alone). */
import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { toast } from "sonner";
import { Sparkles, ToggleRight, ToggleLeft, Activity, Target } from "lucide-react";

const MODE_LABEL = {
    expected_value: "EXPECTED VALUE",
    win_rate: "WIN RATE",
    trend_follow: "TREND FOLLOW",
};
const MODE_ACCENT = {
    expected_value: "text-[#A1A1AA] border-[#1F1F1F]",
    win_rate: "text-[#10F2C5] border-[#10F2C5]/40",
    trend_follow: "text-[#FFD700] border-[#FFD700]/40",
};
const MODE_CYCLE = ["expected_value", "win_rate", "trend_follow"];

function MultiplierBar({ mult }) {
    // 0.5× → 0% fill ; 1.0× → 50% ; 1.3× → 100%
    const pct = Math.max(0, Math.min(100, ((mult - 0.5) / 0.8) * 100));
    const color = mult >= 1.15 ? "bg-[#00FF41]"
        : mult >= 1.0 ? "bg-[#FFD700]"
        : mult >= 0.7 ? "bg-[#FFB000]"
        : "bg-[#FF3B30]";
    return (
        <div className="relative h-2 bg-[#1F1F1F] overflow-hidden">
            <div className={`absolute inset-y-0 left-0 transition-all duration-700 ${color}`} style={{ width: `${pct}%` }} />
            {/* neutral marker */}
            <div className="absolute inset-y-0 w-px bg-[#52525B]/60" style={{ left: "62.5%" }} title="1.0× neutral" />
        </div>
    );
}

function Toggle({ on, onClick, testid }) {
    const Icon = on ? ToggleRight : ToggleLeft;
    return (
        <button
            onClick={onClick}
            data-testid={testid}
            className={`flex items-center gap-1.5 px-2 py-1 border font-mono text-[10px] tracking-widest transition-colors
                ${on ? "text-[#00FF41] border-[#00FF41]/40 hover:bg-[#00FF41]/10"
                     : "text-[#52525B] border-[#1F1F1F] hover:border-[#333333]"}`}
        >
            <Icon className="w-3.5 h-3.5" />
            {on ? "ON" : "OFF"}
        </button>
    );
}

function AdaptiveAccountRow({ account, status, onPatch }) {
    const ptMode = status.profit_taking_mode || "expected_value";
    const ar = status.adaptive_risk || {};
    const ap = status.auto_preset || {};
    const caps = status.max_tp_pips_per_symbol || {};
    const capEntries = Object.entries(caps).filter(([, v]) => Number(v) > 0);

    // iter-77 · Live Smart Cap classification, mirroring backend
    // adaptive_mode.apply_profit_taking_mode():
    //   - win_rate + EXPLICIT cap → "EXPLICIT" badge (always applies)
    //   - win_rate + no explicit + CHOPPY regime → "SMART CAP · 60p/100p" (active)
    //   - win_rate + no explicit + TRENDING regime → "SMART CAP · OFF" (runners stretch)
    //   - other modes → "UNCAPPED"
    const CHOPPY = ["DEFENSIVE_SCALP", "CAUTIOUS_WAIT", "TRANSITIONAL", "RANGING"];
    const TIGHTENED = ["DEFENSIVE_SCALP", "CAUTIOUS_WAIT"];  // 0.6× tightening
    const TRENDING = ["TRENDING", "AGGRESSIVE"];
    const regime = ap.regime_execution_mode;
    let capLabel, capColor, capActive;
    if (capEntries.length > 0) {
        capLabel = "EXPLICIT · " + capEntries.map(([s, v]) => `${s} ≤${Math.round(v)}p`).join(" · ");
        capColor = "text-[#FFD700]";
        capActive = true;
    } else if (ptMode === "win_rate") {
        if (TIGHTENED.includes(regime)) {
            capLabel = `SMART CAP · 60p (${regime})`;
            capColor = "text-[#10F2C5]";
            capActive = true;
        } else if (CHOPPY.includes(regime)) {
            capLabel = `SMART CAP · 100p (${regime})`;
            capColor = "text-[#10F2C5]";
            capActive = true;
        } else if (TRENDING.includes(regime)) {
            capLabel = `SMART CAP · OFF (${regime} → runners free)`;
            capColor = "text-[#00FF41]";
            capActive = false;
        } else {
            capLabel = "SMART CAP · OFF (no regime context)";
            capColor = "text-[#A1A1AA]/80";
            capActive = false;
        }
    } else {
        capLabel = "UNCAPPED";
        capColor = "text-[#52525B]/80";
        capActive = false;
    }

    const cycleMode = () => {
        const next = MODE_CYCLE[(MODE_CYCLE.indexOf(ptMode) + 1) % MODE_CYCLE.length];
        // iter-77 · Pure Smart Cap by default — no longer force-set an
        // explicit 100p cap when flipping to win_rate. Smart Cap will
        // handle the cap dynamically based on regime. User can still
        // set an explicit cap via the manual override.
        onPatch({ profit_taking_mode: next }, `Profit-taking mode → ${MODE_LABEL[next]}`);
    };

    return (
        <div className="border border-[#1F1F1F] bg-[#0F0F0F] p-4 space-y-3"
             data-testid={`adaptive-card-${account.id}`}>
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                        {(account.broker || "—").toUpperCase()} · {account.account_number || "—"}
                    </div>
                    <div className="font-display font-bold text-base tracking-tight truncate">
                        {account.label}
                    </div>
                </div>
                <button
                    onClick={cycleMode}
                    data-testid={`adaptive-mode-toggle-${account.id}`}
                    className={`shrink-0 font-mono text-[10px] tracking-widest px-2.5 py-1 border transition-colors
                        ${MODE_ACCENT[ptMode]} hover:bg-[#1F1F1F]`}
                    title="Click to cycle profit-taking mode"
                >
                    {MODE_LABEL[ptMode]}
                </button>
            </div>

            {/* TP cap row — iter-77 Smart Cap aware */}
            <div className="flex items-center justify-between gap-3 font-mono text-[10px] tracking-widest"
                 data-testid={`smart-cap-${account.id}`}>
                <span className="text-[#52525B] flex items-center gap-1.5">
                    <Target className={`w-3 h-3 ${capActive ? "" : "opacity-40"}`} /> TP CAP
                </span>
                <span className={capColor}>{capLabel}</span>
            </div>

            {/* Adaptive risk row */}
            <div className="space-y-1.5" data-testid={`adaptive-risk-${account.id}`}>
                <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                    <span className="text-[#52525B] flex items-center gap-1.5">
                        <Activity className="w-3 h-3" /> ADAPTIVE RISK
                    </span>
                    <Toggle
                        on={ar.enabled}
                        onClick={() => onPatch(
                            { adaptive_risk_enabled: !ar.enabled },
                            `Adaptive risk ${!ar.enabled ? "enabled" : "disabled"}`,
                        )}
                        testid={`adaptive-risk-toggle-${account.id}`}
                    />
                </div>
                <MultiplierBar mult={Number(ar.multiplier) || 1.0} />
                <div className="flex items-center justify-between font-mono text-[9px] text-[#52525B] tracking-widest">
                    <span>
                        {ar.win_rate_pct != null
                            ? `WIN RATE ${ar.win_rate_pct}% · ${ar.samples ?? 0} samples`
                            : `${ar.samples ?? 0} closed — need ≥5 for adaptation`}
                    </span>
                    <span className={Number(ar.multiplier) >= 1.0 ? "text-[#00FF41]" : "text-[#FFB000]"}>
                        {(Number(ar.multiplier) || 1.0).toFixed(2)}×
                    </span>
                </div>
            </div>

            {/* Auto-preset row */}
            <div className="space-y-1" data-testid={`auto-preset-${account.id}`}>
                <div className="flex items-center justify-between font-mono text-[10px] tracking-widest">
                    <span className="text-[#52525B] flex items-center gap-1.5">
                        <Sparkles className="w-3 h-3" /> AUTO-PRESET
                    </span>
                    <Toggle
                        on={ap.enabled}
                        onClick={() => onPatch(
                            { auto_preset_enabled: !ap.enabled },
                            `Auto-preset ${!ap.enabled ? "enabled" : "disabled"}`,
                        )}
                        testid={`auto-preset-toggle-${account.id}`}
                    />
                </div>
                <div className="flex items-center justify-between font-mono text-[9px] text-[#52525B] tracking-widest">
                    <span>
                        {ap.regime_execution_mode || "—"}
                        {ap.regime && <> · <span className="text-[#A1A1AA]/80">{ap.regime}</span></>}
                    </span>
                    <span className={ap.enabled ? "text-[#00FF41]" : "text-[#A1A1AA]/80"}>
                        {ap.enabled ? "→" : "would →"} {(ap.would_select || "balanced").toUpperCase()}
                    </span>
                </div>
            </div>
        </div>
    );
}

export default function AdaptiveModePanel() {
    const [accounts, setAccounts] = useState([]);
    const [statusByAcct, setStatusByAcct] = useState({});
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);

    const fetchAll = useCallback(async () => {
        try {
            // Only MT5 accounts (skip crypto bridges — adaptive mode is XAU/BTC focused)
            const acctRes = await api.get("/accounts");
            const mt5 = (acctRes.data || []).filter(
                a => a && !a.kind && (a.status === "connected" || a.mode === "paper"),
            );
            setAccounts(mt5);
            const statuses = await Promise.all(
                mt5.map(a =>
                    api.get(`/bot/adaptive-status?account_id=${encodeURIComponent(a.id)}`)
                       .then(r => [a.id, r.data])
                       .catch(() => [a.id, null]),
                ),
            );
            const map = {};
            for (const [id, s] of statuses) { if (s) map[id] = s; }
            setStatusByAcct(map);
            setErr(null);
        } catch (e) {
            setErr(e?.response?.data?.detail || e.message || "Failed to load adaptive status");
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchAll();
        const id = setInterval(fetchAll, 30_000);
        return () => clearInterval(id);
    }, [fetchAll]);

    const patchAccount = useCallback(async (accountId, body, successMsg) => {
        try {
            await api.put(`/bot/config?account_id=${encodeURIComponent(accountId)}`, body);
            toast.success(successMsg);
            fetchAll();
        } catch (e) {
            toast.error(e?.response?.data?.detail || e.message || "Update failed");
        }
    }, [fetchAll]);

    if (loading) {
        return (
            <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6" data-testid="adaptive-panel-loading">
                <div className="font-mono text-[10px] text-[#52525B] tracking-widest animate-pulse">
                    LOADING ADAPTIVE MODE…
                </div>
            </div>
        );
    }
    if (err) return null;
    if (!accounts.length) return null;

    const adaptiveActive = accounts.filter(a => {
        const s = statusByAcct[a.id] || {};
        return s.profit_taking_mode === "win_rate"
            || s.adaptive_risk?.enabled
            || s.auto_preset?.enabled;
    }).length;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="adaptive-mode-panel">
            <div className="px-5 py-4 border-b border-[#1F1F1F] flex items-center gap-3">
                <Sparkles className={`w-4 h-4 ${adaptiveActive ? "text-[#10F2C5]" : "text-[#52525B]"}`} />
                <div className="flex-1">
                    <div className="font-mono text-xs tracking-widest">
                        WIN-RATE ADAPTIVE MODE · iter-74
                    </div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mt-0.5">
                        {adaptiveActive > 0
                            ? `${adaptiveActive}/${accounts.length} accounts running adaptive overlays`
                            : "All accounts on classic Plan A — click any mode badge to swap profile"}
                    </div>
                </div>
            </div>
            <div className={`p-5 grid gap-4 ${accounts.length > 1 ? "md:grid-cols-2 xl:grid-cols-3" : ""}`}>
                {accounts.map(a => {
                    const s = statusByAcct[a.id];
                    if (!s) return null;
                    return (
                        <AdaptiveAccountRow
                            key={a.id}
                            account={a}
                            status={s}
                            onPatch={(body, msg) => patchAccount(a.id, body, msg)}
                        />
                    );
                })}
            </div>
        </div>
    );
}
