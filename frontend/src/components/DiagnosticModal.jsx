import { useState, useCallback } from "react";
import api from "@/lib/api";
import { X, Play, Wrench, Copy, CheckCircle2, AlertTriangle, XCircle, Loader2 } from "lucide-react";
import { toast } from "sonner";

const STATUS_STYLE = {
    pass: { color: "#00FF41", Icon: CheckCircle2, label: "PASS" },
    warn: { color: "#FFB000", Icon: AlertTriangle, label: "WARN" },
    fail: { color: "#FF3B30", Icon: XCircle,      label: "FAIL" },
};

function reportToText(report) {
    if (!report) return "";
    const L = [];
    L.push(`STOIC DIAGNOSTIC — ${report.checked_at}`);
    L.push(`OVERALL: ${(report.status || "").toUpperCase()}`);
    L.push("");
    for (const sec of report.sections || []) {
        L.push(`[${(sec.status || "").toUpperCase()}] ${sec.title}`);
        for (const c of sec.checks || []) {
            L.push(`  ${(c.status || "").toUpperCase().padEnd(4)} | ${c.label}: ${c.detail || ""}`);
            if (c.fix_label) L.push(`         FIX: ${c.fix_label}${c.fix_code ? ` [auto:${c.fix_code}]` : ""}`);
        }
        L.push("");
    }
    if (report.auto_fixable_codes?.length) {
        L.push(`Auto-fixable: ${report.auto_fixable_codes.join(", ")}`);
    }
    return L.join("\n");
}

export function DiagnosticModal({ open, onClose }) {
    const [report, setReport] = useState(null);
    const [loading, setLoading] = useState(false);
    const [fixing, setFixing] = useState(false);

    const run = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await api.get("/diagnostic/run");
            setReport(data);
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Diagnostic failed");
        } finally {
            setLoading(false);
        }
    }, []);

    const applyFix = useCallback(async (codes) => {
        if (!codes?.length) return;
        setFixing(true);
        try {
            const { data } = await api.post("/diagnostic/auto-fix", { codes });
            const lines = Object.entries(data.results || {}).map(
                ([code, res]) => `${code}: ${res?.error ? `❌ ${res.error}` : JSON.stringify(res)}`,
            );
            toast.success("Auto-fix applied", { description: lines.join(" · ") });
            await run();   // re-run diagnostic to refresh status
        } catch (e) {
            toast.error(e?.response?.data?.detail || "Auto-fix failed");
        } finally {
            setFixing(false);
        }
    }, [run]);

    const copyText = useCallback(async () => {
        try {
            await navigator.clipboard.writeText(reportToText(report));
            toast.success("Report copied to clipboard");
        } catch {
            toast.error("Copy failed — try selecting manually");
        }
    }, [report]);

    if (!open) return null;

    return (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-start justify-center overflow-y-auto p-4"
             data-testid="diagnostic-modal" onClick={onClose}>
            <div className="bg-[#0A0A0A] border border-[#1F1F1F] max-w-3xl w-full mt-12 mb-12"
                 onClick={(e) => e.stopPropagation()}>
                <div className="flex items-center justify-between px-5 py-4 border-b border-[#1F1F1F]">
                    <div>
                        <div className="font-mono text-[10px] text-[#FFB000] tracking-widest">ADMIN · DIAGNOSTIC</div>
                        <div className="text-sm text-[#E4E4E7] mt-1">Auto-diagnose every bot subsystem</div>
                    </div>
                    <button onClick={onClose} className="text-[#A1A1AA] hover:text-white" data-testid="diagnostic-close">
                        <X className="w-5 h-5" />
                    </button>
                </div>

                <div className="px-5 py-4 flex flex-wrap items-center gap-3 border-b border-[#1F1F1F]">
                    <button onClick={run} disabled={loading}
                            className="px-4 py-2 bg-[#00FF41]/10 border border-[#00FF41]/40 text-[#00FF41] font-mono text-xs tracking-widest hover:bg-[#00FF41]/20 disabled:opacity-40 inline-flex items-center gap-2"
                            data-testid="diagnostic-run">
                        {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
                        {report ? "RE-RUN" : "RUN DIAGNOSTIC"}
                    </button>
                    {report?.auto_fixable_codes?.length > 0 && (
                        <button onClick={() => applyFix(report.auto_fixable_codes)} disabled={fixing}
                                className="px-4 py-2 bg-[#FFB000]/10 border border-[#FFB000]/40 text-[#FFB000] font-mono text-xs tracking-widest hover:bg-[#FFB000]/20 disabled:opacity-40 inline-flex items-center gap-2"
                                data-testid="diagnostic-autofix-all">
                            {fixing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wrench className="w-3.5 h-3.5" />}
                            AUTO-FIX ALL ({report.auto_fixable_codes.length})
                        </button>
                    )}
                    {report && (
                        <button onClick={copyText}
                                className="px-4 py-2 border border-[#1F1F1F] text-[#A1A1AA] font-mono text-xs tracking-widest hover:bg-[#1F1F1F] inline-flex items-center gap-2 ml-auto"
                                data-testid="diagnostic-copy">
                            <Copy className="w-3.5 h-3.5" /> COPY AS TEXT
                        </button>
                    )}
                </div>

                <div className="px-5 py-4 max-h-[60vh] overflow-y-auto">
                    {!report && !loading && (
                        <div className="text-center py-10 text-[#52525B] font-mono text-xs tracking-widest">
                            Press <span className="text-[#00FF41]">RUN DIAGNOSTIC</span> to start
                        </div>
                    )}
                    {loading && !report && (
                        <div className="text-center py-10 text-[#A1A1AA] font-mono text-xs inline-flex items-center justify-center w-full gap-2">
                            <Loader2 className="w-4 h-4 animate-spin" /> Running checks…
                        </div>
                    )}
                    {report && (
                        <div className="space-y-4">
                            {report.sections.map((sec) => (
                                <Section key={sec.id} section={sec} applyFix={applyFix} fixing={fixing} />
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function Section({ section, applyFix, fixing }) {
    const S = STATUS_STYLE[section.status] || STATUS_STYLE.warn;
    return (
        <div className="border border-[#1F1F1F]" data-testid={`diag-section-${section.id}`}>
            <div className="px-4 py-2.5 flex items-center justify-between border-b border-[#1F1F1F] bg-[#0F0F0F]">
                <div className="flex items-center gap-2">
                    <S.Icon className="w-4 h-4" style={{ color: S.color }} />
                    <span className="font-mono text-[11px] tracking-widest" style={{ color: S.color }}>{S.label}</span>
                    <span className="text-sm text-[#E4E4E7] ml-2">{section.title}</span>
                </div>
            </div>
            <div className="divide-y divide-[#161616]">
                {section.checks.map((c, i) => {
                    const CS = STATUS_STYLE[c.status] || STATUS_STYLE.warn;
                    return (
                        <div key={`${c.label}-${i}`} className="px-4 py-2.5 flex items-start gap-3" data-testid={`diag-check-${section.id}-${i}`}>
                            <CS.Icon className="w-3.5 h-3.5 mt-0.5 shrink-0" style={{ color: CS.color }} />
                            <div className="flex-1 min-w-0">
                                <div className="text-xs text-[#E4E4E7]">{c.label}</div>
                                {c.detail && <div className="text-[11px] text-[#A1A1AA] mt-0.5 break-words">{c.detail}</div>}
                                {c.fix_label && !c.fix_code && (
                                    <div className="text-[10px] text-[#FFB000]/80 mt-1 font-mono tracking-wide">
                                        MANUAL · {c.fix_label}
                                    </div>
                                )}
                            </div>
                            {c.fix_code && (
                                <button onClick={() => applyFix([c.fix_code])} disabled={fixing}
                                        className="px-2.5 py-1 border border-[#FFB000]/40 text-[#FFB000] font-mono text-[10px] tracking-widest hover:bg-[#FFB000]/10 disabled:opacity-40"
                                        data-testid={`diag-fix-${c.fix_code}`}>
                                    FIX
                                </button>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
