import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { ShieldCheck, ShieldAlert, Siren } from "lucide-react";

const LEVEL = {
    safe: { cls: "border-[#00FF41]/30 bg-[#00FF41]/5 text-[#00FF41]", Icon: ShieldCheck, label: "TRADING SAFE" },
    warn: { cls: "border-[#FFB000]/40 bg-[#FFB000]/10 text-[#FFB000]", Icon: ShieldAlert, label: "ATTENTION" },
    critical: { cls: "border-[#FF3B30]/50 bg-[#FF3B30]/10 text-[#FF3B30]", Icon: Siren, label: "CAPITAL AT RISK" },
};

const Pill = ({ label, value, bad, testid }) => (
    <span data-testid={testid}
        className={`font-mono text-[10px] tracking-widest px-2 py-0.5 border ${
            bad ? "border-current" : "border-[#1F1F1F] text-[#A1A1AA]"}`}>
        {label} <span className={bad ? "font-bold" : "text-white"}>{value}</span>
    </span>
);

export function TradingSafetyBanner() {
    const [data, setData] = useState(null);

    const load = useCallback(async () => {
        try {
            const { data: d } = await api.get("/bot/safety-status");
            setData(d);
        } catch { /* banner is best-effort */ }
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 30000);
        return () => clearInterval(id);
    }, [load]);

    if (!data) return null;
    const meta = LEVEL[data.level] || LEVEL.warn;
    const { Icon } = meta;

    return (
        <div className={`border px-4 py-2.5 flex items-center gap-3 flex-wrap ${meta.cls}`}
            data-testid="trading-safety-banner" data-level={data.level}>
            <Icon className="w-4 h-4 shrink-0" />
            <span className="font-display font-bold text-xs tracking-widest">{meta.label}</span>
            <div className="flex items-center gap-2 flex-wrap">
                <Pill testid="safety-mode" label="MODE"
                    value={data.deployment_mode || "—"}
                    bad={["LIVE", "MIXED"].includes(data.deployment_mode)} />
                <Pill testid="safety-capital-risk" label="AT RISK"
                    value={`$${Number(data.capital_at_risk || 0).toFixed(0)}`}
                    bad={Number(data.capital_at_risk || 0) > 0} />
                <Pill testid="safety-recon" label="RECON"
                    value={data.reconciliation_delay_sec == null
                        ? "—" : `${data.reconciliation_delay_sec}s`}
                    bad={data.reconciliation_delay_sec > 300} />
                <Pill testid="safety-panic" label="PANIC"
                    value={data.panic_active ? "ACTIVE" : "CLEAR"}
                    bad={!!data.panic_active} />
                <Pill testid="safety-live" label="LIVE"
                    value={data.live_accounts?.length || 0}
                    bad={data.live_accounts?.length > 0} />
                <Pill testid="safety-unprotected" label="UNPROTECTED"
                    value={data.unprotected_open}
                    bad={data.unprotected_open > 0} />
                <Pill testid="safety-unresolved" label="UNRESOLVED"
                    value={data.unresolved_submissions}
                    bad={data.unresolved_submissions > 0} />
                <Pill testid="safety-stale" label="STALE FEEDS"
                    value={data.stale_feeds?.length || 0}
                    bad={data.stale_feeds?.length > 0} />
                <Pill testid="safety-tripped" label="TRIPPED"
                    value={data.tripped?.length || 0}
                    bad={data.tripped?.length > 0} />
                <Pill testid="safety-daily" label="DAILY DD"
                    value={`${data.daily_worst_consumed_pct}%`}
                    bad={data.daily_worst_consumed_pct >= 70} />
            </div>
        </div>
    );
}
