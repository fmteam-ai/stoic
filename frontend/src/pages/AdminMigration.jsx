import { useState, useRef } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Download, Upload, Database, FileJson, AlertTriangle, Loader2 } from "lucide-react";

/**
 * iter-87 · Admin · Migration Helper
 *
 * One-click export of the admin's accounts + bot_configs + custom presets as
 * a portable JSON blob, plus a one-click upload to a fresh production DB so
 * the MT5 EAs already deployed on the user's VPS keep working without re-pairing.
 *
 * Workflow:
 *   1. On the preview/source env, click EXPORT → JSON file downloaded.
 *   2. Click "Save to Github" → "Deploy" on the platform.
 *   3. On the freshly-deployed prod env, login as admin → /admin/migration → IMPORT.
 *   4. EAs continue to authenticate using the same bridge_tokens — no manual
 *      re-pair needed.
 */
export default function AdminMigration() {
    const [exporting, setExporting] = useState(false);
    const [importing, setImporting] = useState(false);
    const [lastExport, setLastExport] = useState(null);
    const [lastImport, setLastImport] = useState(null);
    const fileInputRef = useRef(null);

    const handleExport = async () => {
        setExporting(true);
        try {
            const { data } = await api.get("/admin/export-state");
            setLastExport({ at: new Date(), counts: data.counts });
            const filename = `stoic-state-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);
            toast.success("State exported", {
                description: `${filename} — ${data.counts.accounts} accounts, ${data.counts.bot_configs} bot configs, ${data.counts.user_presets} presets`,
            });
        } catch (e) {
            toast.error("Export failed", { description: formatApiError(e) });
        } finally {
            setExporting(false);
        }
    };

    const handleFilePicked = async (e) => {
        const file = e.target.files?.[0];
        if (!file) return;
        if (!window.confirm(
            `Import "${file.name}"?\n\n` +
            "This will upsert the contained accounts, bot_configs and user_presets " +
            "into THIS environment, remapping ownership to the current admin. " +
            "Existing rows with the same _id will be UPDATED; new ones will be INSERTED. " +
            "The operation is idempotent — running twice is safe."
        )) {
            e.target.value = "";
            return;
        }
        setImporting(true);
        try {
            const text = await file.text();
            const payload = JSON.parse(text);
            const { data } = await api.post("/admin/import-state", payload);
            setLastImport({ at: new Date(), report: data });
            const cs = data.collections || {};
            toast.success("State imported", {
                description: `accounts +${cs.accounts?.inserted || 0}/~${cs.accounts?.updated || 0}, ` +
                             `bot_configs +${cs.bot_configs?.inserted || 0}/~${cs.bot_configs?.updated || 0}, ` +
                             `presets +${cs.user_presets?.inserted || 0}/~${cs.user_presets?.updated || 0}`,
            });
        } catch (err) {
            toast.error("Import failed", { description: formatApiError(err) || err.message });
        } finally {
            setImporting(false);
            e.target.value = "";
        }
    };

    return (
        <AppLayout>
            <PageHeader
                title="Migration Helper"
                subtitle="Move your state across environments — perfect for shipping preview → production."
                testid="admin-migration-header"
            />
            <div className="p-4 md:p-8 space-y-6" data-testid="admin-migration-page">

                {/* Workflow card */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6">
                    <div className="flex items-center gap-2 text-[#A1A1AA] font-mono text-[10px] tracking-widest mb-4">
                        <Database className="w-3.5 h-3.5" /> SCHEMA V1 · IDEMPOTENT
                    </div>
                    <h2 className="font-display text-2xl font-bold tracking-tight mb-2">
                        Deploy without losing your accounts.
                    </h2>
                    <p className="text-sm text-[#A1A1AA] max-w-3xl">
                        Exports your <strong className="text-white">accounts</strong> (incl. bridge tokens, so the MT5 EAs on your VPS keep working), <strong className="text-white">bot configurations</strong>, and <strong className="text-white">custom presets</strong>. Drop the JSON into the freshly-deployed production environment and your bot resumes where it left off — no manual re-pairing required.
                    </p>
                    <div className="mt-5 grid sm:grid-cols-3 gap-3 text-xs font-mono tracking-widest text-[#71717A]">
                        <div className="border border-[#1F1F1F] bg-[#050505] p-3">
                            <span className="text-[#00FF41]">01</span> &nbsp; EXPORT FROM SOURCE
                        </div>
                        <div className="border border-[#1F1F1F] bg-[#050505] p-3">
                            <span className="text-[#00FF41]">02</span> &nbsp; DEPLOY TO PROD
                        </div>
                        <div className="border border-[#1F1F1F] bg-[#050505] p-3">
                            <span className="text-[#00FF41]">03</span> &nbsp; IMPORT ON PROD
                        </div>
                    </div>
                </div>

                {/* Export */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6">
                    <div className="flex items-start justify-between gap-4">
                        <div className="flex-1">
                            <div className="flex items-center gap-2 text-[#00FF41] font-mono text-[10px] tracking-widest mb-2">
                                <Download className="w-3.5 h-3.5" /> STEP 01 · EXPORT
                            </div>
                            <h3 className="font-display text-xl font-bold mb-1">Download current state</h3>
                            <p className="text-sm text-[#A1A1AA] max-w-2xl">
                                Generates <code className="text-[#00FF41]">stoic-state-&lt;timestamp&gt;.json</code>. Keep this file private — it contains broker bridge tokens.
                            </p>
                        </div>
                        <button
                            onClick={handleExport}
                            disabled={exporting}
                            data-testid="export-state-button"
                            className="shrink-0 flex items-center gap-2 px-4 py-2.5 bg-[#00FF41] hover:bg-[#00E53A] text-black font-medium text-xs tracking-widest transition-colors disabled:opacity-50"
                        >
                            {exporting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                            {exporting ? "EXPORTING…" : "EXPORT STATE"}
                        </button>
                    </div>
                    {lastExport && (
                        <div className="mt-4 pt-4 border-t border-[#1F1F1F] text-xs font-mono text-[#71717A]" data-testid="last-export-summary">
                            LAST EXPORT · {lastExport.at.toLocaleTimeString()} · accounts={lastExport.counts.accounts} · bot_configs={lastExport.counts.bot_configs} · user_presets={lastExport.counts.user_presets}
                        </div>
                    )}
                </div>

                {/* Import */}
                <div className="border border-[#FFB020]/40 bg-[#0A0A0A] p-6">
                    <div className="flex items-start justify-between gap-4">
                        <div className="flex-1">
                            <div className="flex items-center gap-2 text-[#FFB020] font-mono text-[10px] tracking-widest mb-2">
                                <Upload className="w-3.5 h-3.5" /> STEP 03 · IMPORT
                            </div>
                            <h3 className="font-display text-xl font-bold mb-1">Upload state to this environment</h3>
                            <p className="text-sm text-[#A1A1AA] max-w-2xl">
                                Idempotent upsert — re-importing the same file is safe (existing rows are updated by <code>_id</code>, not duplicated). Ownership is remapped to <strong className="text-white">the currently-signed-in admin</strong>.
                            </p>
                            <div className="mt-3 flex items-start gap-2 border border-[#FFB020]/30 bg-[#FFB020]/5 p-3 text-xs text-[#FFB020]">
                                <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                                <span>If you&apos;ve made changes on this env since the export, importing may overwrite them for documents with matching IDs.</span>
                            </div>
                        </div>
                        <div className="shrink-0 flex flex-col gap-2 items-end">
                            <input
                                ref={fileInputRef}
                                type="file"
                                accept="application/json,.json"
                                onChange={handleFilePicked}
                                className="hidden"
                                data-testid="import-file-input"
                            />
                            <button
                                onClick={() => fileInputRef.current?.click()}
                                disabled={importing}
                                data-testid="import-state-button"
                                className="flex items-center gap-2 px-4 py-2.5 border border-[#FFB020] text-[#FFB020] hover:bg-[#FFB020]/10 font-medium text-xs tracking-widest transition-colors disabled:opacity-50"
                            >
                                {importing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileJson className="w-3.5 h-3.5" />}
                                {importing ? "IMPORTING…" : "CHOOSE FILE"}
                            </button>
                        </div>
                    </div>
                    {lastImport && (
                        <div className="mt-4 pt-4 border-t border-[#1F1F1F] text-xs font-mono text-[#71717A] space-y-1" data-testid="last-import-summary">
                            <div>LAST IMPORT · {lastImport.at.toLocaleTimeString()}</div>
                            {Object.entries(lastImport.report?.collections || {}).map(([k, v]) => (
                                <div key={k}>
                                    {k.padEnd(15)} received={v.received} · inserted={v.inserted} · updated={v.updated}
                                </div>
                            ))}
                            {lastImport.report?.remapped_user_id && (
                                <div className="text-[#FFB020]">
                                    remapped user_id from {lastImport.report.remapped_user_id.from} → {lastImport.report.remapped_user_id.to}
                                </div>
                            )}
                        </div>
                    )}
                </div>

                {/* What's included */}
                <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-6">
                    <div className="text-[#A1A1AA] font-mono text-[10px] tracking-widest mb-3">// PAYLOAD CONTENTS</div>
                    <div className="grid sm:grid-cols-2 gap-3 text-sm">
                        <div>
                            <div className="text-[#00FF41] font-mono text-xs tracking-widest mb-2">INCLUDED</div>
                            <ul className="text-[#A1A1AA] space-y-1 text-xs">
                                <li>· Admin user (portable fields — NOT password_hash)</li>
                                <li>· Accounts (incl. bridge_token + encrypted creds)</li>
                                <li>· Bot configs</li>
                                <li>· Custom user presets</li>
                            </ul>
                        </div>
                        <div>
                            <div className="text-[#71717A] font-mono text-xs tracking-widest mb-2">SKIPPED</div>
                            <ul className="text-[#71717A] space-y-1 text-xs">
                                <li>· Trades / signals / agent_activity (replayed live)</li>
                                <li>· Heartbeats (transient)</li>
                                <li>· Audit log (env-specific)</li>
                                <li>· Other users&apos; data</li>
                            </ul>
                        </div>
                    </div>
                </div>
            </div>
        </AppLayout>
    );
}
