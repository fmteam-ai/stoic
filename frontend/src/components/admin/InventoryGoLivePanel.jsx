import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Loader2, Trash2, ShieldCheck, ClipboardCheck } from "lucide-react";

const Row = ({ label, children, testId }) => (
    <div data-testid={testId} className="flex items-start justify-between gap-4 py-2 border-b border-white/5 text-sm">
        <span className="font-mono text-xs text-white/50 uppercase tracking-wider pt-0.5">{label}</span>
        <span className="text-right">{children}</span>
    </div>
);

export const InventoryGoLivePanel = () => {
    const [inv, setInv] = useState(null);
    const [pend, setPend] = useState(null);
    const [busy, setBusy] = useState("");
    const [exp, setExp] = useState({ accounts: "", enabled: "", bots: "" });
    const [accountIds, setAccountIds] = useState("");          // A15-1 — production requires the approved account ids
    const [policies, setPolicies] = useState([]);              // A15-1 — signed policy migrations from release/policy_migrations/
    const [policyFile, setPolicyFile] = useState("");
    const policy = policies.find((p) => p.file === policyFile) || null;
    const choosePolicy = (file) => {
        setPolicyFile(file);
        const pol = policies.find((p) => p.file === file);
        if (pol) { setExp({ accounts: String(pol.accounts), enabled: String(pol.enabled), bots: String(pol.bots) }); setAccountIds(pol.account_ids.join(",")); }
    };

    const load = useCallback(async () => {
        try {
            const [a, b, c] = await Promise.all([api.get("/authority/inventory"), api.get("/authority/inventory/pending"),
                                                 api.get("/authority/inventory/policies").catch(() => ({ data: { policies: [] } }))]);
            setInv(a.data); setPend(b.data); setPolicies(c.data?.policies || []);
            const cur = b.data?.expectation || {};
            setExp((e) => ({
                accounts: e.accounts === "" && cur.accounts != null ? String(cur.accounts) : e.accounts,
                enabled: e.enabled === "" && cur.enabled != null ? String(cur.enabled) : e.enabled,
                bots: e.bots === "" && cur.bots != null ? String(cur.bots) : e.bots,
            }));
        } catch (err) { toast.error(formatApiError(err)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const [lastError, setLastError] = useState("");
    const run = async (key, fn, okMsg) => {
        setBusy(key); setLastError("");
        try { await fn(); toast.success(okMsg); await load(); }
        catch (err) {
            const d = err?.response?.data?.detail;
            const msg = d?.errors ? `${d.code}: ${d.errors.join("; ")}` : formatApiError(err);
            setLastError(msg); toast.error(msg);
        }
        finally { setBusy(""); }
    };

    if (!inv || !pend) return <div className="p-4 text-white/40 text-sm font-mono"><Loader2 className="inline h-4 w-4 animate-spin mr-2" />loading inventory…</div>;
    const c = inv.counts || {};
    const violations = inv.violations || [];
    const singleAdmin = pend.approval_mode === "single_admin";
    const secondAdminMissing = !singleAdmin && (pend.admin_count || 0) < 2;
    const selfBlocked = (proposedBy) => !singleAdmin && proposedBy === pend.me;
    const approverLabel = singleAdmin ? "same admin · fresh step-up" : "2nd admin";
    const fill = () => setExp({ accounts: String(c.configured ?? 0), enabled: String(c.live_enabled ?? 0), bots: String(c.bots_enabled ?? 0) });

    return (
        <section data-testid="inventory-golive-panel" className="rounded-xl border border-white/10 bg-[#0B0F14] p-5 space-y-5">
            <header className="flex items-center justify-between">
                <h3 className="text-base md:text-lg font-semibold flex items-center gap-2"><ClipboardCheck className="h-5 w-5 text-[#FFB000]" />Inventory &amp; Go-Live Gate</h3>
                <span data-testid="inventory-gate-status" className={`font-mono text-xs px-2 py-1 rounded ${inv.ok ? "bg-emerald-500/15 text-emerald-300" : "bg-[#FF3B30]/15 text-[#FF3B30]"}`}>{inv.ok ? "GATE PASS" : `GATE FAIL · ${violations.length}`}</span>
            </header>

            <div className="grid md:grid-cols-2 gap-6">
                <div>
                    <Row label="accounts (cfg / live / enabled)" testId="inventory-count-accounts">{c.configured} / {c.live_configured} / {c.live_enabled}</Row>
                    <Row label="bots enabled (raw / orphan)" testId="inventory-count-bots">{c.bots_enabled} ({c.bots_configured_raw} / {c.bots_null_account})</Row>
                    <Row label="expected A/E/B" testId="inventory-expected">{c.expected_accounts ?? "—"} / {c.expected_enabled ?? "—"} / {c.expected_bots ?? "—"}</Row>
                    <Row label="inventory hash" testId="inventory-hash"><code className="text-xs">{(inv.inventory_hash || "").slice(0, 16)}</code>{inv.approved_hash ? " · approved" : " · NOT approved"}</Row>
                    <Row label="admins" testId="inventory-admin-count">{pend.admin_count}{secondAdminMissing && <span className="text-[#FFB000] ml-2">second admin required for approvals</span>}{singleAdmin && <span className="text-[#FFB000] ml-2">single-operator mode</span>}</Row>
                </div>
                <div>
                    <div className="font-mono text-xs text-white/50 uppercase tracking-wider mb-2">violations</div>
                    {violations.length === 0 ? <div className="text-emerald-300 text-sm">none</div> : (
                        <ul data-testid="inventory-violations" className="space-y-1 text-sm text-[#FF3B30]">{violations.map((v, i) => <li key={`${i}-${v}`}>· {v}</li>)}</ul>
                    )}
                </div>
            </div>

            {pend.orphan_bots?.length > 0 && (
                <div data-testid="inventory-orphans" className="rounded-lg border border-[#FF3B30]/30 p-3 space-y-2">
                    <div className="font-mono text-xs text-[#FF3B30] uppercase">bot configs with no account ({pend.orphan_total ?? pend.orphan_bots.length}{pend.orphan_total > pend.orphan_bots.length ? `, showing ${pend.orphan_bots.length}` : ""}) — delete to clear the defect</div>
                    {pend.orphan_bots.map((b) => (
                        <div key={b.id} className="flex items-center justify-between text-sm">
                            <span className="font-mono text-xs">{b.id} · {b.symbol || b.name || "—"} · owner {b.user_id || "?"} · {b.active ? "ACTIVE" : "inactive"}</span>
                            <Button size="sm" variant="ghost" data-testid={`orphan-bot-delete-${b.id}`} disabled={!!busy} onClick={() => run(b.id, () => api.delete(`/authority/inventory/orphan-bots/${b.id}`), "orphan bot config deleted")}>
                                <Trash2 className="h-4 w-4 text-[#FF3B30]" />
                            </Button>
                        </div>
                    ))}
                </div>
            )}

            <div className="grid md:grid-cols-2 gap-6">
                <div className="space-y-2">
                    <div className="font-mono text-xs text-white/50 uppercase tracking-wider">1 · declare expectation (accounts / enabled / bots)</div>
                    <div className="flex gap-2">
                        {["accounts", "enabled", "bots"].map((k) => (
                            <Input key={k} data-testid={`expectation-${k}`} inputMode="numeric" placeholder={k} value={exp[k]} onChange={(e) => setExp({ ...exp, [k]: e.target.value.replace(/\D/g, "") })} className="w-24" />
                        ))}
                        <Button variant="ghost" size="sm" data-testid="expectation-fill-current" onClick={fill}>use current</Button>
                    </div>
                    <Input data-testid="expectation-account-ids" placeholder="approved account ids, comma-separated (required in production)"
                        value={accountIds} onChange={(e) => setAccountIds(e.target.value)} className="font-mono text-xs" />
                    {policies.length > 0 && (
                        <select data-testid="expectation-policy" value={policyFile} onChange={(e) => choosePolicy(e.target.value)}
                            className="w-full bg-[#0A0A0A] border border-[#1F1F1F] text-xs font-mono text-white p-2">
                            <option value="">no signed policy (6/3/3 live default)</option>
                            {policies.map((p) => (
                                <option key={p.file} value={p.file} disabled={!p.matches_host}>
                                    {p.policy_version} · {p.accounts}/{p.enabled}/{p.bots} · {p.demo_only ? "DEMO-only" : "LIVE"}{p.matches_host ? "" : " · other host"}
                                </option>
                            ))}
                        </select>
                    )}
                    {policy && <div data-testid="expectation-policy-info" className="text-[11px] font-mono text-[#71717A]">signed by {policy.issuer} · expires {String(policy.expires_at).slice(0, 10)} · {policy.demo_only ? "every listed account must be an attested DEMO account" : "real-money policy"}</div>}
                    {pend.expectation_pending ? (
                        <div data-testid="expectation-pending" className="text-xs text-[#FFB000] font-mono">pending {pend.expectation_pending.accounts}/{pend.expectation_pending.enabled}/{pend.expectation_pending.bots} proposed by {pend.expectation_pending.proposed_by} — a DIFFERENT admin must approve</div>
                    ) : null}
                    {exp.bots !== "" && exp.enabled !== "" && exp.bots !== exp.enabled && (
                        <div data-testid="expectation-precheck" className="text-xs text-[#FFB000] font-mono">policy: bots must equal enabled (one bot per enabled account)</div>
                    )}
                    {lastError && <div data-testid="golive-last-error" className="text-xs text-[#FF3B30] font-mono">{lastError}</div>}
                    <div className="flex gap-2">
                        <Button size="sm" data-testid="expectation-propose" disabled={!!busy || !exp.accounts || !exp.enabled || !exp.bots}
                            onClick={() => run("prop", () => api.post("/authority/inventory/expectation", { accounts: +exp.accounts, enabled: +exp.enabled, bots: +exp.bots,
                                account_ids: accountIds.split(",").map((x) => x.trim()).filter(Boolean), ...(policy ? { policy_migration: policy.migration } : {}) }), singleAdmin ? "expectation proposed — approve with a fresh step-up" : "expectation proposed — second admin must approve")}>
                            {busy === "prop" ? <Loader2 className="h-4 w-4 animate-spin" /> : "Propose"}
                        </Button>
                        <Button size="sm" variant="outline" data-testid="expectation-approve" disabled={!!busy || !pend.expectation_pending || selfBlocked(pend.expectation_pending?.proposed_by)}
                            onClick={() => run("appr", () => api.post("/authority/inventory/expectation/approve"), "expectation approved")}>
                            <ShieldCheck className="h-4 w-4 mr-1" />Approve ({approverLabel})
                        </Button>
                    </div>
                </div>
                <div className="space-y-2">
                    <div className="font-mono text-xs text-white/50 uppercase tracking-wider">2 · approve current inventory hash</div>
                    {pend.hash_pending ? (
                        <div data-testid="hash-pending" className="text-xs text-[#FFB000] font-mono">hash {pend.hash_pending.inventory_hash?.slice(0, 16)} proposed by {pend.hash_pending.proposed_by} — {singleAdmin ? "confirm with a fresh step-up (single-operator mode)" : "a DIFFERENT admin must confirm"}</div>
                    ) : <div className="text-xs text-white/40">requires zero violations above</div>}
                    <div className="flex gap-2">
                        <Button size="sm" data-testid="hash-propose" disabled={!!busy || violations.length > 0}
                            onClick={() => run("hp", () => api.post("/authority/inventory/approve", { note: "go-live approval" }), singleAdmin ? "inventory hash proposed — confirm it" : "inventory hash proposed — second admin must confirm")}>
                            {busy === "hp" ? <Loader2 className="h-4 w-4 animate-spin" /> : "Propose approval"}
                        </Button>
                        <Button size="sm" variant="outline" data-testid="hash-confirm" disabled={!!busy || !pend.hash_pending || selfBlocked(pend.hash_pending?.proposed_by)}
                            onClick={() => run("hc", () => api.post("/authority/inventory/approve/confirm"), "inventory approved — gate clears on next decision")}>
                            <ShieldCheck className="h-4 w-4 mr-1" />Confirm ({approverLabel})
                        </Button>
                    </div>
                </div>
            </div>
            {secondAdminMissing && (
                <div data-testid="second-admin-hint" className="text-xs font-mono text-white/50">
                    Second admin: register the account normally, then on the server run <code className="text-white/80">docker compose exec -T backend python ops/promote_admin.py you2@example.com</code>
                    <span className="block mt-1">No second person? Set <code className="text-white/80">INVENTORY_APPROVAL_MODE=single_admin</code> in backend/.env and restart — approvals then need only a fresh step-up and are stamped single_admin in the audit chain.</span>
                </div>
            )}
            {singleAdmin && (
                <div data-testid="single-admin-mode" className="text-xs font-mono text-[#FFB000]">
                    single-operator mode (INVENTORY_APPROVAL_MODE=single_admin): no 4-eyes — the proposing admin may approve after a fresh step-up; every approval is stamped in the audit chain
                </div>
            )}
        </section>
    );
};
