import { Clock, Database, FlaskConical, ShieldCheck, ShieldAlert, Activity } from "lucide-react";

const KIND = {
    broker_reconciled: { label: "BROKER-RECONCILED", cls: "border-[#00FF41]/50 text-[#00FF41]", Icon: ShieldCheck },
    indicative: { label: "INDICATIVE", cls: "border-[#FFB000]/50 text-[#FFB000]", Icon: Activity },
    derived: { label: "DERIVED · LEDGER", cls: "border-[#00BFFF]/50 text-[#00BFFF]", Icon: Database },
    simulated: { label: "SIMULATED", cls: "border-[#EC4899]/50 text-[#EC4899]", Icon: FlaskConical },
};

const age = (s) => (s == null ? "—" : s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`);

// Shared chart provenance strip (audit r28 P2-05) — renders the backend `provenance` contract.
export const ChartProvenance = ({ p, testid = "chart-provenance", loading = false }) => {
    if (loading) return null;
    if (!p || p.contract_version !== 1 || !KIND[p.source_kind]) {
        // audit r29 P2-05 — never hide a missing/invalid contract
        return (
            <div data-testid={`${testid}-missing`} className="inline-flex items-center gap-1 px-1.5 py-0.5 border border-[#FF3B30]/60 text-[#FF3B30] font-mono text-[9px] tracking-widest mt-1">
                <ShieldAlert className="w-3 h-3" /> PROVENANCE MISSING — series origin unverified
            </div>
        );
    }
    const k = KIND[p.source_kind] || KIND.derived;
    const Icon = k.Icon;
    const fallback = p.cache_status && !["live", "upstream", "memory_cache"].includes(p.cache_status);
    const title = [
        `provider: ${p.provider}`, `as of: ${p.as_of || "n/a"} (${p.timezone})`, `freshness: ${age(p.freshness_s)}`,
        `points: ${p.points}`, `missing intervals: ${p.missing_intervals_count}`, `cache: ${p.cache_status}`,
        `environment: ${p.environment}`, p.ledger_id ? `ledger: ${p.ledger_id}` : null,
        p.reconciliation_id ? `reconciliation: ${p.reconciliation_id}` : null, p.note,
    ].filter(Boolean).join("\n");
    return (
        <div data-testid={testid} title={title}
            className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[9px] tracking-widest text-[#52525B] mt-1">
            <span data-testid={`${testid}-kind`} className={`inline-flex items-center gap-1 px-1.5 py-0.5 border ${k.cls}`}><Icon className="w-3 h-3" /> {k.label}</span>
            <span>{p.provider}</span>
            <span className="inline-flex items-center gap-1"><Clock className="w-3 h-3" /> {p.as_of ? `${p.as_of.slice(0, 16).replace("T", " ")} ${p.timezone}` : "no data"}</span>
            <span data-testid={`${testid}-freshness`} className={p.stale ? "text-[#FF3B30]" : ""}>{p.stale ? "STALE · " : ""}{age(p.freshness_s)} old</span>
            {p.missing_intervals_count > 0 && <span data-testid={`${testid}-gaps`} className="text-[#FFB000]">{p.missing_intervals_count} gap{p.missing_intervals_count > 1 ? "s" : ""}</span>}
            {fallback && <span data-testid={`${testid}-fallback`} className="text-[#FFB000]">{String(p.cache_status).toUpperCase()}</span>}
            <span>{String(p.environment || "").toUpperCase()}</span>
            {(p.ledger_id || p.reconciliation_id) && <span className="truncate max-w-[180px]">#{p.ledger_id || p.reconciliation_id}</span>}
        </div>
    );
};
