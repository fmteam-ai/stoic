import { useEffect, useState, useCallback } from "react";
import api, { formatApiError } from "@/lib/api";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import { MetaBrainPanel } from "@/components/MetaBrainPanel";
import { BrainPhaseBPanel } from "@/components/BrainPhaseBPanel";
import { Brain, ArrowUp, ArrowDown, Pause, Zap as Lightning, Trash2 as Trash, ShieldCheck, Wand2, X, ChevronDown } from "lucide-react";
import { useLiveStream } from "@/lib/useLiveStream";
import { toast } from "sonner";

function ActionPill({ action }) {
    if (action === "BUY") return <span className="font-mono text-[10px] tracking-widest px-2 py-1 bg-[#00FF41]/10 text-[#00FF41] border border-[#00FF41]/30 inline-flex items-center gap-1"><ArrowUp className="w-3 h-3" /> BUY</span>;
    if (action === "SELL") return <span className="font-mono text-[10px] tracking-widest px-2 py-1 bg-[#FF3B30]/10 text-[#FF3B30] border border-[#FF3B30]/30 inline-flex items-center gap-1"><ArrowDown className="w-3 h-3" /> SELL</span>;
    return <span className="font-mono text-[10px] tracking-widest px-2 py-1 bg-[#1F1F1F] text-[#A1A1AA] border border-[#1F1F1F] inline-flex items-center gap-1"><Pause className="w-3 h-3" /> HOLD</span>;
}

function ConfBar({ value, threshold }) {
    const v = Math.min(100, Math.max(0, value));
    const meets = v >= threshold;
    return (
        <div className="w-full">
            <div className="flex justify-between text-[10px] font-mono mb-1">
                <span className="text-[#52525B] tracking-widest">CONFIDENCE</span>
                <span className={meets ? "text-[#00FF41]" : "text-[#FFB000]"}>{v}% / {threshold}% req</span>
            </div>
            <div className="h-1 bg-[#1F1F1F] relative">
                <div className={`absolute top-0 left-0 h-full ${meets ? "bg-[#00FF41]" : "bg-[#FFB000]"}`} style={{ width: `${v}%` }} />
                <div className="absolute top-0 h-full w-px bg-white/40" style={{ left: `${threshold}%` }} />
            </div>
        </div>
    );
}

function ManualTradeModal({ accounts, onClose, onSuccess }) {
    const [form, setForm] = useState({
        account_id: accounts[0]?.id || "",
        symbol: "XAUUSD",
        action: "BUY",
        lot_size: 0.01,
        stop_loss_pct: 1.0,
        take_profit_pct: 2.0,
    });
    const [supportedSymbols, setSupportedSymbols] = useState(["XAUUSD", "BTCUSD"]);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState("");

    useEffect(() => {
        api.get("/market/symbols").then(({ data }) => {
            const syms = Array.isArray(data) ? data.map(d => d.symbol || d).filter(Boolean) : [];
            if (syms.length) setSupportedSymbols(syms);
        }).catch(() => { /* keep defaults */ });
    }, []);

    const submit = async (e) => {
        e.preventDefault();
        setSubmitting(true); setError("");
        try {
            const { data } = await api.post("/trades/manual", form);
            onSuccess(data);
        } catch (e2) {
            setError(formatApiError(e2));
        } finally {
            setSubmitting(false);
        }
    };

    if (accounts.length === 0) {
        return (
            <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4" data-testid="manual-trade-modal">
                <div className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-md w-full p-6 space-y-4">
                    <div className="flex items-center justify-between">
                        <div className="font-display font-bold text-lg text-[#FFD700]">Manual Test Trade</div>
                        <button onClick={onClose} className="text-[#52525B] hover:text-white" data-testid="manual-trade-close"><X className="w-4 h-4" /></button>
                    </div>
                    <div className="text-sm text-[#A1A1AA]">
                        Manual trades are only allowed on <span className="text-[#FFD700]">paper accounts</span>. Add one in the <em>Accounts</em> page first.
                    </div>
                    <button onClick={onClose} className="w-full py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs tracking-widest">CLOSE</button>
                </div>
            </div>
        );
    }

    return (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4" data-testid="manual-trade-modal">
            <form onSubmit={submit} className="bg-[#0A0A0A] border border-[#FFD700]/40 max-w-md w-full p-6 space-y-4">
                <div className="flex items-center justify-between">
                    <div>
                        <div className="font-display font-bold text-lg text-[#FFD700]">Manual Test Trade</div>
                        <div className="text-xs text-[#A1A1AA] mt-0.5">Paper-only · fills instantly at live mid-price</div>
                    </div>
                    <button type="button" onClick={onClose} className="text-[#52525B] hover:text-white" data-testid="manual-trade-close"><X className="w-4 h-4" /></button>
                </div>

                {error && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="manual-trade-error">{error}</div>}

                <div>
                    <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">ACCOUNT</label>
                    <select value={form.account_id} onChange={e => setForm({ ...form, account_id: e.target.value })}
                        required data-testid="manual-trade-account"
                        className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm outline-none">
                        {accounts.map(a => <option key={a.id} value={a.id}>{a.label} · ${a.balance?.toFixed(2)}</option>)}
                    </select>
                </div>

                <div className="grid grid-cols-2 gap-3">
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">SYMBOL</label>
                        <select value={form.symbol} onChange={e => setForm({ ...form, symbol: e.target.value })}
                            data-testid="manual-trade-symbol"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none">
                            {supportedSymbols.map(s => <option key={s} value={s}>{s}</option>)}
                        </select>
                    </div>
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">DIRECTION</label>
                        <div className="grid grid-cols-2 gap-1.5">
                            {["BUY", "SELL"].map(a => (
                                <button key={a} type="button" onClick={() => setForm({ ...form, action: a })}
                                    data-testid={`manual-trade-action-${a.toLowerCase()}`}
                                    className={`py-2 text-xs font-mono tracking-widest border transition-colors ${
                                        form.action === a
                                            ? a === "BUY" ? "border-[#00FF41] bg-[#00FF41]/10 text-[#00FF41]" : "border-[#FF3B30] bg-[#FF3B30]/10 text-[#FF3B30]"
                                            : "border-[#1F1F1F] text-[#A1A1AA]"
                                    }`}>{a}</button>
                            ))}
                        </div>
                    </div>
                </div>

                <div className="grid grid-cols-3 gap-3">
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">LOT SIZE</label>
                        <input type="number" step="0.01" min="0.01" max="100" value={form.lot_size}
                            onChange={e => setForm({ ...form, lot_size: parseFloat(e.target.value) || 0.01 })}
                            data-testid="manual-trade-lot"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">SL %</label>
                        <input type="number" step="0.1" min="0.1" max="20" value={form.stop_loss_pct}
                            onChange={e => setForm({ ...form, stop_loss_pct: parseFloat(e.target.value) || 1 })}
                            data-testid="manual-trade-sl"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                    <div>
                        <label className="font-mono text-[10px] text-[#52525B] tracking-widest block mb-1.5">TP %</label>
                        <input type="number" step="0.1" min="0.1" max="50" value={form.take_profit_pct}
                            onChange={e => setForm({ ...form, take_profit_pct: parseFloat(e.target.value) || 2 })}
                            data-testid="manual-trade-tp"
                            className="w-full bg-[#050505] border border-[#1F1F1F] focus:border-[#FFD700] px-3 py-2 text-sm font-mono outline-none" />
                    </div>
                </div>

                <div className="text-[10px] text-[#52525B] font-mono leading-relaxed border-t border-[#1F1F1F] pt-3">
                    SL/TP are computed as percentage distance from the live mid-price at fill time.
                    Trade auto-closes when SL or TP is hit (settled every ~60s).
                </div>

                <div className="flex gap-2">
                    <button type="button" onClick={onClose}
                        className="flex-1 py-2 border border-[#1F1F1F] hover:border-[#333333] text-xs tracking-widest">CANCEL</button>
                    <button type="submit" disabled={submitting}
                        data-testid="manual-trade-submit"
                        className="flex-1 py-2 bg-[#FFD700] hover:bg-[#FFC700] disabled:opacity-50 text-black font-bold text-xs tracking-widest transition-colors">
                        {submitting ? "PLACING…" : "PLACE TRADE"}
                    </button>
                </div>
            </form>
        </div>
    );
}

function VetoCascade({ s }) {
    const reasoning = s.reasoning || "";
    // Each gate evaluates a fragment of the signal payload to a tri-state:
    //   "pass"  → green tick · gate said OK
    //   "block" → red cross · gate caused or contributed to a HOLD
    //   "skip"  → grey dot · gate didn't run / not applicable
    const stages = [
        { key: "ai", label: "AI", state: ["BUY", "SELL"].includes(s.action) ? "pass" : "skip" },
        { key: "macro", label: "MACRO", state: reasoning.includes("VETO (macro)") ? "block" : "pass" },
        { key: "regime", label: "REGIME", state: reasoning.includes("VETO (regime)") ? "block" : "pass" },
        { key: "entropy", label: "ENTROPY", state: reasoning.includes("VETO (entropy)") ? "block" : "pass" },
        { key: "meta", label: "META", state: reasoning.includes("VETO (meta-labeler)") ? "block" : (s.meta_label ? "pass" : "skip") },
        { key: "mtf", label: "MTF", state: reasoning.includes("VETO (multi-timeframe)") ? "block" : ((s.mtf_gate?.checked) ? "pass" : "skip") },
        { key: "learned", label: "LEARNED", state: reasoning.includes("VETO (learned-meta)") ? "block" : (s.learned_meta ? "pass" : "skip") },
        { key: "aplus", label: "A+", state: reasoning.includes("VETO (A+ confluence)") ? "block" : (s.aplus_confluence?.checks ? "pass" : "skip") },
        { key: "rr", label: "R:R", state: reasoning.includes("VETO (R:R)") ? "block" : (s.rr_ratio != null ? "pass" : "skip") },
        { key: "dxy", label: "DXY", state: reasoning.includes("VETO (DXY gate)") ? "block" : (s.dxy ? "pass" : "skip") },
    ];
    const cls = {
        pass: "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/5",
        block: "border-[#FF3B30]/50 text-[#FF3B30] bg-[#FF3B30]/10",
        skip: "border-[#1F1F1F] text-[#52525B] bg-[#0A0A0A]",
    };
    const symbol = { pass: "✓", block: "✗", skip: "·" };
    const blockedCount = stages.filter(s => s.state === "block").length;
    return (
        <div className="space-y-1.5" data-testid={`veto-cascade-${s.id}`}>
            <div className="flex items-center justify-between font-mono text-[10px] text-[#52525B] tracking-widest">
                <span>VETO CASCADE</span>
                <span className={blockedCount > 0 ? "text-[#FF3B30]" : "text-[#00FF41]"}>
                    {blockedCount > 0 ? `${blockedCount} BLOCKED` : "ALL CLEAR"}
                </span>
            </div>
            <div className="flex items-center gap-1 flex-wrap">
                {stages.map(stage => (
                    <span key={stage.key}
                        data-testid={`veto-cascade-${stage.key}-${stage.state}`}
                        title={`${stage.label}: ${stage.state}`}
                        className={`font-mono text-[10px] tracking-widest px-1.5 py-0.5 border ${cls[stage.state]}`}>
                        <span className="mr-1">{symbol[stage.state]}</span>{stage.label}
                    </span>
                ))}
            </div>
        </div>
    );
}

// Maps a HOLD signal to the FIRST plain-English reason a human can act on.
// Returns null for actionable signals (BUY/SELL) — banner is HOLD-only.
function diagnoseHold(s) {
    const action = s.action;
    const reasoning = s.reasoning || "";
    const conf = s.confidence ?? 0;
    const minConf = s.min_confidence_required ?? 75;

    // VETO patterns — order matters: most actionable / clearest reason wins.
    // The strings on the right of the regex match the labels surfaced in
    // ai_signals.py (e.g. "VETO (DXY gate): ...").
    const vetoMap = [
        // [match string in reasoning, severity, label, fallback explanation]
        ["VETO (macro)",          "block",  "MACRO BLACKOUT",  "High-impact USD news event within blackout window — bot pauses entries."],
        ["VETO (DXY gate)",       "block",  "DXY GATE",        "Dollar Index is trending against this XAU direction — fighting macro."],
        ["VETO (R:R)",            "block",  "POOR R:R",        "Reward-to-risk below the minimum (TP ladder too close to entry vs SL)."],
        ["VETO (A+ confluence)",  "warn",   "NOT A+ SETUP",    "Confluence factors aren't aligned enough to qualify as an A-grade setup."],
        ["VETO (learned-meta)",   "warn",   "LEARNED-META",    "Historical session-specific p_win is below threshold for this kind of setup."],
        ["VETO (MTF gate)",       "warn",   "MTF MISMATCH",    "M15 and H1 trends disagree — would be a counter-trend entry."],
        ["VETO (meta-labeler)",   "warn",   "META MISMATCH",   "Claude and the local LR disagree on this trade — best to wait."],
        ["VETO (entropy)",        "warn",   "CHOP / ENTROPY",  "Recent price action is near-random (entropy > 0.85) — no edge."],
        ["VETO (regime)",         "warn",   "REGIME · CHOP",   "Low-volatility / chop regime needs higher confidence to risk an entry."],
        ["VETO (news)",           "warn",   "DUAL-AI VETO",    "Chart wanted to trade but news sentiment disagreed — forced to HOLD."],
    ];

    for (const [needle, severity, label, fallback] of vetoMap) {
        if (reasoning.includes(needle)) {
            // Try to extract the line after the VETO marker for a more specific reason
            const idx = reasoning.indexOf(needle);
            const rest = reasoning.slice(idx + needle.length).split("\n")[0].replace(/^:\s*/, "").trim();
            return { severity, label, detail: rest || fallback };
        }
    }

    // No veto fired — only HOLDs here are confidence-floor or Claude-said-HOLD.
    if (action === "HOLD") {
        if (conf < minConf) {
            return {
                severity: "info",
                label: "BELOW CONFIDENCE FLOOR",
                detail: `Claude generated this setup but confidence (${conf}%) is under the minimum (${minConf}%). No veto fired — the bar simply isn't high enough.`,
            };
        }
        return {
            severity: "info",
            label: "CLAUDE RETURNED HOLD",
            detail: "All gates passed but Claude itself declined to trade — most often because no clear directional bias exists right now.",
        };
    }
    return null;
}

function HoldReasonBanner({ s }) {
    if (s.action !== "HOLD") return null;
    const dx = diagnoseHold(s);
    if (!dx) return null;

    const palette = {
        block: { bd: "border-[#FF3B30]/30", bg: "bg-[#FF3B30]/10", fg: "text-[#FF3B30]" },
        warn:  { bd: "border-[#FFB000]/30", bg: "bg-[#FFB000]/10", fg: "text-[#FFB000]" },
        info:  { bd: "border-[#1F1F1F]",    bg: "bg-[#0A0A0A]",    fg: "text-[#A1A1AA]" },
    }[dx.severity];

    return (
        <div className={`px-3 py-2 flex items-start gap-2 border ${palette.bd} ${palette.bg}`}
            data-testid={`hold-reason-${s.id}`}>
            <ShieldCheck className={`w-4 h-4 ${palette.fg} shrink-0 mt-0.5`} />
            <div className="text-xs flex-1">
                <div className={`font-mono text-[10px] tracking-widest mb-0.5 ${palette.fg}`}>
                    WHY HOLD · {dx.label}
                </div>
                <div className="text-[#A1A1AA] leading-relaxed">{dx.detail}</div>
            </div>
        </div>
    );
}

// Score a BUY/SELL signal on a 0-100 strength scale. Each indicator contributes
// independently — score is the average of the components present. Concerns
// are surfaced as bullet-like chips so the user knows EXACTLY what's weak.
function diagnoseStrength(s) {
    if (s.action !== "BUY" && s.action !== "SELL") return null;

    const conf = s.confidence ?? 0;
    const minConf = s.min_confidence_required ?? 75;
    const rr = s.rr_ratio;
    const pWin = s.learned_meta?.p_win;
    const pThr = s.learned_meta?.threshold;
    const aplus = s.aplus_confluence;
    const sentScore = Math.abs(s.sentiment?.score ?? 0);
    const dxyAligned = s.dxy_gate?.aligned;

    const components = [];
    const concerns = [];
    const strengths = [];

    // 1. Confidence margin (Claude conviction)
    const confMargin = conf - minConf;
    if (confMargin >= 15) {
        components.push(100); strengths.push(`High Claude conviction (+${confMargin.toFixed(0)}% over floor)`);
    } else if (confMargin >= 5) {
        components.push(70);
    } else {
        components.push(35);
        concerns.push(`Confidence barely clears floor (${conf}% vs ${minConf}%)`);
    }

    // 2. Reward-to-risk
    if (rr != null) {
        if (rr >= 2.5) { components.push(100); strengths.push(`Excellent R:R ${rr.toFixed(2)}:1`); }
        else if (rr >= 1.8) { components.push(70); }
        else { components.push(35); concerns.push(`Marginal R:R ${rr.toFixed(2)}:1`); }
    }

    // 3. Learned-meta probability
    if (pWin != null && pThr != null) {
        const pMargin = pWin - pThr;
        if (pMargin >= 0.10) { components.push(100); strengths.push(`Learned-meta p_win ${(pWin*100).toFixed(0)}% (strong)`); }
        else if (pMargin >= 0.03) { components.push(65); }
        else { components.push(35); concerns.push(`Learned-meta p_win ${(pWin*100).toFixed(0)}% only just above ${(pThr*100).toFixed(0)}% threshold`); }
    }

    // 4. A+ confluence depth
    if (aplus && typeof aplus.checks === "number") {
        if (aplus.checks >= 5) { components.push(100); strengths.push(`${aplus.checks}/5 A+ confluence factors aligned`); }
        else if (aplus.checks >= 4) { components.push(70); }
        else { components.push(40); concerns.push(`Thin A+ confluence (${aplus.checks}/5 factors)`); }
    }

    // 5. News sentiment alignment
    if (sentScore >= 0.4) { components.push(100); strengths.push(`Strong news sentiment alignment`); }
    else if (sentScore >= 0.15) { components.push(70); }
    else { components.push(55); /* neutral sentiment — not a red flag */ }

    // 6. DXY gate (XAU only)
    if (s.symbol === "XAUUSD" && dxyAligned === true) {
        components.push(100); strengths.push("DXY regime reinforces trade direction");
    }

    if (components.length === 0) return null;
    const score = Math.round(components.reduce((a, b) => a + b, 0) / components.length);

    let tier;
    if (score >= 80)      tier = { label: "STRONG",   severity: "strong",   sub: "High-conviction setup — full Kelly sizing recommended" };
    else if (score >= 60) tier = { label: "SOLID",    severity: "solid",    sub: "Clean setup with one or two soft spots" };
    else                  tier = { label: "MARGINAL", severity: "marginal", sub: "Cleared all vetoes but barely — consider half-Kelly or skip" };

    return { score, ...tier, concerns, strengths };
}

function StrengthBanner({ s }) {
    const dx = diagnoseStrength(s);
    if (!dx) return null;

    const palette = {
        strong:   { bd: "border-[#00FF41]/30", bg: "bg-[#00FF41]/10", fg: "text-[#00FF41]" },
        solid:    { bd: "border-[#1F1F1F]",    bg: "bg-[#0A0A0A]",    fg: "text-[#A1A1AA]" },
        marginal: { bd: "border-[#FFB000]/30", bg: "bg-[#FFB000]/10", fg: "text-[#FFB000]" },
    }[dx.severity];

    return (
        <div className={`px-3 py-2 border ${palette.bd} ${palette.bg}`}
            data-testid={`signal-strength-${s.id}`}>
            <div className="flex items-center justify-between gap-2 mb-1">
                <div className="flex items-center gap-2">
                    <ShieldCheck className={`w-4 h-4 ${palette.fg}`} />
                    <span className={`font-mono text-[10px] tracking-widest ${palette.fg}`}>
                        SIGNAL STRENGTH · {dx.label}
                    </span>
                </div>
                <div className={`font-display font-bold text-sm ${palette.fg}`}>{dx.score}/100</div>
            </div>
            <div className="text-[10px] text-[#A1A1AA] leading-relaxed mb-1.5">{dx.sub}</div>
            {/* Score bar */}
            <div className="h-1 bg-[#0A0A0A] border border-[#1F1F1F] mb-2">
                <div className={`h-full ${dx.severity === "strong" ? "bg-[#00FF41]" : dx.severity === "marginal" ? "bg-[#FFB000]" : "bg-[#A1A1AA]"}`}
                    style={{ width: `${dx.score}%` }} />
            </div>
            {dx.concerns.length > 0 && (
                <div className="flex flex-wrap gap-1 mb-1">
                    {dx.concerns.map(c => (
                        <span key={c} className="font-mono text-[9px] text-[#FFB000] bg-[#FFB000]/10 border border-[#FFB000]/30 px-1.5 py-0.5 tracking-wide">
                            ⚠ {c}
                        </span>
                    ))}
                </div>
            )}
            {dx.strengths.length > 0 && (
                <div className="flex flex-wrap gap-1">
                    {dx.strengths.map(c => (
                        <span key={c} className="font-mono text-[9px] text-[#00FF41] bg-[#00FF41]/10 border border-[#00FF41]/30 px-1.5 py-0.5 tracking-wide">
                            ✓ {c}
                        </span>
                    ))}
                </div>
            )}
        </div>
    );
}

function SignalCard({ s, accounts, onExecute, onDelete }) {
    const [accId, setAccId] = useState(accounts[0]?.id || "");
    useEffect(() => { if (!accId && accounts[0]) setAccId(accounts[0].id); }, [accounts, accId]);
    const tradeable = s.tradeable;
    const sentScore = s.sentiment?.score ?? 0;
    const sentLabel = s.sentiment?.label;
    const sentClass = sentScore > 0.2 ? "text-[#00FF41]" : sentScore < -0.2 ? "text-[#FF3B30]" : "text-[#A1A1AA]";

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-5 space-y-3" data-testid={`signal-card-${s.symbol}`}>
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <ActionPill action={s.action} />
                    <span className="font-mono text-sm tracking-widest">{s.symbol}</span>
                    {s.origin === "auto" && (
                        <span className="font-mono text-[10px] tracking-widest px-1.5 py-0.5 bg-[#FFB000]/10 text-[#FFB000] border border-[#FFB000]/30">AUTO</span>
                    )}
                </div>
                <div className="flex items-center gap-2">
                    <span className="font-mono text-[10px] text-[#52525B] tracking-widest">RISK · {s.risk_level?.toUpperCase()}</span>
                    <button onClick={() => onDelete(s.id)} data-testid={`signal-delete-${s.id}`} className="p-1 text-[#52525B] hover:text-[#FF3B30] transition-colors">
                        <Trash className="w-3.5 h-3.5" />
                    </button>
                </div>
            </div>

            <ConfBar value={s.confidence || 0} threshold={s.min_confidence_required || 65} />

            {(s.ev || s.quality) && (
                <div className="flex items-center gap-3 font-mono text-[11px] pt-1" data-testid={`signal-ev-quality-${s.id}`}>
                    {s.ev && (
                        <span className={s.ev.ev_usd > 0 ? "text-[#00FF41]" : s.ev.ev_usd < 0 ? "text-[#FF3B30]" : "text-[#A1A1AA]"}
                              title={`p_win ${(s.ev.p_win * 100).toFixed(0)}% (${s.ev.p_basis || "—"}) · move ${s.ev.expected_move_pips}p − costs ${s.ev.cost_pips}p = ${s.ev.ev_pips}p net`}>
                            EV {s.ev.ev_usd != null ? `$${Number(s.ev.ev_usd).toFixed(2)}` : `${s.ev.ev_pips}p`}
                        </span>
                    )}
                    {s.quality && (
                        <span className={`px-1.5 py-0.5 border ${s.quality.score >= 80 ? "border-[#00FF41]/40 text-[#00FF41]" : s.quality.score >= 55 ? "border-[#FFB000]/40 text-[#FFB000]" : "border-[#FF3B30]/40 text-[#FF3B30]"}`}
                              title={Object.entries(s.quality.breakdown || {}).map(([k, v]) => `${k} +${v}`).join("  ")}>
                            QUALITY {s.quality.score}/100
                        </span>
                    )}
                    {s.ev_gate_block && <span className="text-[#FF3B30]">EV GATE · SKIPPED</span>}
                </div>
            )}

            <VetoCascade s={s} />

            <HoldReasonBanner s={s} />
            <StrengthBanner s={s} />

            {/* Three-Engine Verification Stack — Quant + Semantic + Meta-Labeler */}
            <div className="grid grid-cols-3 gap-2 pt-2" data-testid={`verification-stack-${s.symbol}`}>
                <div className={`border p-2 ${
                    s.regime_execution_mode?.execution_mode === "DYNAMIC_MOMENTUM"
                        ? "border-[#00FF41]/40 bg-[#00FF41]/5"
                        : s.regime_execution_mode?.execution_mode === "DEFENSIVE_SCALP"
                            ? "border-[#FFB000]/40 bg-[#FFB000]/5"
                            : "border-[#1F1F1F]"
                }`}>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">EXEC MODE</div>
                    <div className="font-mono text-[11px] uppercase tracking-tight">
                        {s.regime_execution_mode?.execution_mode?.replace("_", " ") || "—"}
                    </div>
                </div>
                <div className={`border p-2 ${
                    s.noise_filter?.traffic_light === "green"
                        ? "border-[#00FF41]/40 bg-[#00FF41]/5"
                        : s.noise_filter?.traffic_light === "yellow"
                            ? "border-[#FFB000]/40 bg-[#FFB000]/5"
                            : "border-[#FF3B30]/40 bg-[#FF3B30]/5"
                }`}>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">NOISE · ENTROPY</div>
                    <div className="font-mono text-[11px]">
                        {s.noise_filter?.label || "—"} <span className="text-[#52525B]">· {s.noise_filter?.entropy ?? "—"}</span>
                    </div>
                </div>
                <div className={`border p-2 ${
                    s.meta_label?.verdict === "TRUE_SIGNAL"
                        ? "border-[#00FF41]/40 bg-[#00FF41]/5"
                        : s.meta_label?.verdict === "FAKE_OUT"
                            ? "border-[#FF3B30]/40 bg-[#FF3B30]/5"
                            : "border-[#1F1F1F]"
                }`}>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">META-LABELER</div>
                    <div className="font-mono text-[11px]">
                        {s.meta_label?.verdict?.replace("_", " ") || "—"}
                        {s.meta_label?.p_true != null && (
                            <span className="text-[#52525B]"> · p={s.meta_label.p_true}</span>
                        )}
                    </div>
                </div>
            </div>

            {s.veto_applied && s.reasoning?.includes("VETO (entropy)") && (
                <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 flex items-start gap-2">
                    <ShieldCheck className="w-4 h-4 text-[#FF3B30] shrink-0 mt-0.5" />
                    <div className="text-xs">
                        <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest mb-0.5">ENTROPY VETO · NOISE</div>
                        <div className="text-[#A1A1AA]">Random-walk distribution detected — trade blocked.</div>
                    </div>
                </div>
            )}

            {s.veto_applied && s.reasoning?.includes("VETO (meta-labeler)") && (
                <div className="bg-[#FF3B30]/10 border border-[#FF3B30]/30 px-3 py-2 flex items-start gap-2">
                    <ShieldCheck className="w-4 h-4 text-[#FF3B30] shrink-0 mt-0.5" />
                    <div className="text-xs">
                        <div className="font-mono text-[10px] text-[#FF3B30] tracking-widest mb-0.5">META-LABELER VETO · FAKE-OUT</div>
                        <div className="text-[#A1A1AA]">Quant + Semantic engines disagree — signal classified as a likely fake-out.</div>
                    </div>
                </div>
            )}

            <div className="grid grid-cols-3 gap-2 pt-2">
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">ENTRY</div>
                    <div className="font-mono text-sm">{s.entry_price}</div>
                </div>
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">STOP LOSS</div>
                    <div className="font-mono text-sm text-[#FF3B30]">{s.stop_loss}</div>
                </div>
                <div>
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">TAKE PROFIT</div>
                    <div className="font-mono text-sm text-[#00FF41]">{s.take_profit}</div>
                </div>
            </div>

            {/* Kelly sizing + regime + session */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 pt-1">
                <div className="border border-[#1F1F1F] p-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">LOTS · KELLY</div>
                    <div className="font-mono text-sm">{s.lot_size}</div>
                    {s.kelly_f != null && (
                        <div className="font-mono text-[10px] text-[#A1A1AA]">f={s.kelly_f} · {s.effective_risk_pct}% risk</div>
                    )}
                </div>
                <div className="border border-[#1F1F1F] p-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">REGIME</div>
                    <div className="font-mono text-sm" data-testid={`regime-${s.symbol}`}>{s.regime?.regime || "—"}</div>
                </div>
                <div className="border border-[#1F1F1F] p-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">SESSION</div>
                    <div className="font-mono text-sm uppercase">{s.session?.primary?.replace("_", " ") || "—"}</div>
                </div>
                <div className="border border-[#1F1F1F] p-2">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-0.5">STRATEGY HINT</div>
                    <div className="font-mono text-[11px] uppercase">{s.session_bias?.preferred_strategy?.replace("_", " ") || "—"}</div>
                </div>
            </div>

            {sentLabel && (
                <div className="pt-2 border-t border-[#1F1F1F]">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">NEWS SENTIMENT</div>
                    <div className="flex items-center gap-2 text-xs">
                        <span className={`font-mono ${sentClass}`}>{sentLabel.replace("_", " ").toUpperCase()} · {sentScore >= 0 ? "+" : ""}{sentScore}</span>
                        <span className="text-[#52525B] font-mono">· {s.sentiment.article_count} articles</span>
                    </div>
                </div>
            )}

            {s.reasoning && (
                <div className="pt-2 border-t border-[#1F1F1F]">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1.5">AI REASONING</div>
                    <p className="text-xs text-[#A1A1AA] leading-relaxed whitespace-pre-wrap">{s.reasoning}</p>
                </div>
            )}

            {Array.isArray(s.key_factors) && s.key_factors.length > 0 && (
                <ul className="flex flex-wrap gap-1.5">
                    {s.key_factors.map((f) => (
                        <li key={f} className="font-mono text-[10px] text-[#A1A1AA] bg-[#121212] border border-[#1F1F1F] px-2 py-0.5">{f}</li>
                    ))}
                </ul>
            )}

            <div className="pt-3 border-t border-[#1F1F1F] flex items-center gap-2 flex-wrap">
                {tradeable && accounts.length > 0 ? (
                    <>
                        <select value={accId} onChange={e => setAccId(e.target.value)}
                            data-testid={`signal-account-select-${s.symbol}`}
                            className="bg-[#0A0A0A] border border-[#1F1F1F] focus:border-[#00FF41] px-2 py-1.5 text-xs flex-1 min-w-0 outline-none">
                            {accounts.map(a => <option key={a.id} value={a.id}>{a.label} · #{a.account_number}</option>)}
                        </select>
                        <button onClick={() => onExecute(s.id, accId)}
                            disabled={s.consumed}
                            data-testid={`signal-execute-${s.symbol}`}
                            className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-40 disabled:cursor-not-allowed text-black font-medium px-3 py-1.5 text-xs flex items-center gap-1 transition-colors">
                            <Lightning className="w-3 h-3" /> {s.consumed ? "EXECUTED" : "EXECUTE"}
                        </button>
                    </>
                ) : (
                    <div className="font-mono text-[10px] text-[#FFB000] tracking-widest">
                        {!tradeable ? "BELOW CONFIDENCE THRESHOLD" : "NO ACCOUNT CONFIGURED"}
                    </div>
                )}
            </div>
        </div>
    );
}

export default function Signals() {
    const [signals, setSignals] = useState([]);
    const [accounts, setAccounts] = useState([]);
    const [filter, setFilter] = useState("all");  // all | actionable | hold | strong | solid | marginal | blocked
    const [loading, setLoading] = useState(true);
    const [generating, setGenerating] = useState(false);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");
    const [showManual, setShowManual] = useState(false);

    const load = useCallback(async () => {
        try {
            const [s, a] = await Promise.all([api.get("/signals"), api.get("/accounts")]);
            setSignals(s.data);
            setAccounts(a.data);
        } catch (e) { setErr(formatApiError(e)); }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { load(); }, [load]);

    // Live stream: auto-add new signals + toast on auto-trades
    const { lastEvent } = useLiveStream();
    useEffect(() => {
        if (!lastEvent) return;
        if (lastEvent.type === "signal_created") {
            setSignals(prev => [lastEvent.payload, ...prev].slice(0, 50));
        } else if (lastEvent.type === "trade_created" && lastEvent.payload?.origin === "auto") {
            toast.success(`Auto-trade · ${lastEvent.payload.symbol} ${lastEvent.payload.action}`, {
                description: `${lastEvent.payload.lot_size} lots queued for MT5`,
            });
            // Mark signal as consumed
            const sigId = lastEvent.payload.signal_id;
            if (sigId) setSignals(prev => prev.map(s => s.id === sigId ? { ...s, consumed: true } : s));
        }
    }, [lastEvent]);

    const [genAccountId, setGenAccountId] = useState("");

    const handleGenerate = async () => {
        setGenerating(true); setErr(""); setMsg("");
        try {
            // genAccountId="" → uses the user's default profile; otherwise scope to a bot
            const url = genAccountId
                ? `/signals/generate-all?account_id=${encodeURIComponent(genAccountId)}`
                : "/signals/generate-all";
            const { data } = await api.post(url, {});
            const scope = genAccountId
                ? ` for ${(accounts.find(a => a.id === genAccountId) || {}).label || "selected bot"}`
                : "";
            setMsg(`Generated ${data.generated.length} signal(s)${scope}.`);
            await load();
        } catch (e) { setErr(formatApiError(e)); }
        finally { setGenerating(false); }
    };

    const handleExecute = async (signalId, accountId) => {
        setErr(""); setMsg("");
        try {
            await api.post(`/trades/execute/${signalId}`, { account_id: accountId });
            setMsg("Trade queued for execution. Open MT5 EA to fulfill.");
            await load();
        } catch (e) { setErr(formatApiError(e)); }
    };

    const handleDelete = async (id) => {
        try { await api.delete(`/signals/${id}`); await load(); } catch (e) { setErr(formatApiError(e)); }
    };

    return (
        <AppLayout>
            <PageHeader
                title="AI Signals"
                subtitle="Claude-generated trading signals across your configured symbols."
                testid="signals-header"
                action={
                    <div className="flex gap-2 items-center flex-wrap">
                        <ClearSignalsMenu signals={signals} onCleared={load} />
                        {accounts.length > 1 && (
                            <select value={genAccountId}
                                onChange={(e) => setGenAccountId(e.target.value)}
                                data-testid="signals-generate-account"
                                className="bg-[#050505] border border-[#00FF41]/40 focus:border-[#00FF41] text-[10px] font-mono tracking-widest px-2 py-2 outline-none">
                                <option value="">Default bot</option>
                                {accounts.map(a => (
                                    <option key={a.id} value={a.id}>
                                        Bot · {a.label}
                                    </option>
                                ))}
                            </select>
                        )}
                        <button onClick={() => setShowManual(true)}
                            data-testid="manual-trade-open-button"
                            className="flex items-center gap-2 px-3 py-2 border border-[#FFD700]/50 text-[#FFD700] hover:bg-[#FFD700]/10 font-medium text-xs tracking-widest transition-colors duration-150">
                            <Wand2 className="w-3.5 h-3.5" /> MANUAL TRADE
                        </button>
                        <button onClick={handleGenerate} disabled={generating}
                            data-testid="signals-generate-button"
                            className="flex items-center gap-2 px-4 py-2 bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium text-xs tracking-widest transition-colors duration-150">
                            <Brain className="w-4 h-4" /> {generating ? "ANALYSING…" : "GENERATE SIGNALS"}
                        </button>
                    </div>
                }
            />

            {showManual && (
                <ManualTradeModal
                    accounts={accounts.filter(a => (a.mode || "live").toLowerCase() === "paper")}
                    onClose={() => setShowManual(false)}
                    onSuccess={(trade) => {
                        setShowManual(false);
                        setMsg(`Manual ${trade.action} ${trade.symbol} placed @ ${trade.entry_price} on paper account.`);
                        toast.success(`Paper trade opened · ${trade.symbol} ${trade.action}`, { description: `Entry ${trade.entry_price} · ${trade.lot_size} lots` });
                        load();
                    }}
                />
            )}

            <div className="p-4 md:p-8 space-y-4">
                <MetaBrainPanel />
                <BrainPhaseBPanel />
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono" data-testid="signals-error">{err}</div>}
                {msg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-4 py-2 text-xs text-[#00FF41] font-mono">{msg}</div>}

                {signals.length > 0 && <SignalFilterBar signals={signals} filter={filter} setFilter={setFilter} />}

                {loading ? (
                    <div className="font-mono text-xs text-[#52525B] tracking-widest">LOADING SIGNALS…</div>
                ) : signals.length === 0 ? (
                    <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="signals-empty">
                        <Brain className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                        <div className="font-display font-bold text-lg mb-1">No signals yet</div>
                        <div className="text-sm text-[#A1A1AA] mb-4">Click <em>Generate Signals</em> to let Claude analyse your configured markets.</div>
                    </div>
                ) : (
                    <FilteredGrid signals={signals} filter={filter} accounts={accounts} onExecute={handleExecute} onDelete={handleDelete} />
                )}
            </div>
        </AppLayout>
    );
}



function ClearSignalsMenu({ signals, onCleared }) {
    const [open, setOpen] = useState(false);
    const [busy, setBusy] = useState(false);
    const total = signals.length;
    const holdCount = signals.filter(s => s.action === "HOLD").length;

    const clear = async (scope, label, days) => {
        const noun = scope === "hold" ? `${holdCount} HOLD signal(s)`
                   : scope === "non_hold" ? "all BUY/SELL signals"
                   : days ? `signals older than ${days} day(s)`
                   : "ALL signals";
        if (!window.confirm(`Delete ${noun}? This cannot be undone.`)) return;
        setBusy(true);
        try {
            const params = new URLSearchParams();
            if (scope) params.set("scope", scope);
            if (days) params.set("older_than_days", String(days));
            const { data } = await api.delete(`/signals?${params.toString()}`);
            toast.success(`Cleared ${data.deleted} signal${data.deleted === 1 ? "" : "s"} (${label})`);
            setOpen(false);
            await onCleared();
        } catch (e) {
            toast.error(formatApiError(e));
        } finally { setBusy(false); }
    };

    return (
        <div className="relative" data-testid="clear-signals-menu">
            <button onClick={() => setOpen(!open)} disabled={busy || total === 0}
                data-testid="clear-signals-toggle"
                className="flex items-center gap-2 px-3 py-2 border border-[#FF3B30]/40 text-[#FF3B30] hover:bg-[#FF3B30]/10 disabled:opacity-40 disabled:cursor-not-allowed font-medium text-xs tracking-widest transition-colors duration-150">
                <Trash className="w-3.5 h-3.5" /> CLEAR <ChevronDown className={`w-3 h-3 transition-transform ${open ? "rotate-180" : ""}`} />
            </button>
            {open && (
                <div className="absolute right-0 top-full mt-1 z-30 w-72 border border-[#1F1F1F] bg-[#0A0A0A] shadow-2xl"
                    data-testid="clear-signals-dropdown">
                    <div className="px-3 py-2 border-b border-[#1F1F1F] font-mono text-[10px] text-[#52525B] tracking-widest">
                        {total} SIGNAL{total === 1 ? "" : "S"} IN VIEW
                    </div>
                    <ClearOption icon={Pause} label="Clear HOLD signals only" sub={`${holdCount} noise signal${holdCount === 1 ? "" : "s"}`}
                        onClick={() => clear("hold", "HOLDs")} testid="clear-holds" disabled={holdCount === 0} />
                    <ClearOption icon={Trash} label="Clear signals > 7 days old" sub="Keep the recent week"
                        onClick={() => clear(null, "old", 7)} testid="clear-7d" />
                    <ClearOption icon={Trash} label="Clear signals > 1 day old" sub="Keep last 24h only"
                        onClick={() => clear(null, "old", 1)} testid="clear-1d" />
                    <ClearOption icon={X} label="Clear ALL signals" sub="Wipes the entire history" danger
                        onClick={() => clear("all", "all")} testid="clear-all" />
                </div>
            )}
        </div>
    );
}

function ClearOption({ icon: Icon, label, sub, onClick, testid, danger, disabled }) {
    return (
        <button type="button" onClick={onClick} disabled={disabled}
            data-testid={testid}
            className={`w-full flex items-center gap-3 px-3 py-2 text-left border-b border-[#1F1F1F] last:border-b-0 transition-colors disabled:opacity-30 disabled:cursor-not-allowed ${
                danger ? "hover:bg-[#FF3B30]/10" : "hover:bg-[#121212]"
            }`}>
            <Icon className={`w-3.5 h-3.5 ${danger ? "text-[#FF3B30]" : "text-[#A1A1AA]"}`} />
            <div className="flex-1">
                <div className={`text-xs font-medium ${danger ? "text-[#FF3B30]" : "text-white"}`}>{label}</div>
                <div className="font-mono text-[10px] text-[#52525B] tracking-wide mt-0.5">{sub}</div>
            </div>
        </button>
    );
}


// ─── Filter ────────────────────────────────────────────────────────────────
// Bucket a signal into ONE of: strong | solid | marginal | hold-blocked |
// hold-info | hold-other. Used by both the chip counts and the grid filter.
function bucketOf(s) {
    if (s.action === "BUY" || s.action === "SELL") {
        const dx = diagnoseStrength(s);
        if (!dx) return "solid";
        return dx.severity;  // "strong" | "solid" | "marginal"
    }
    // HOLD
    const dx = diagnoseHold(s);
    if (dx?.severity === "block") return "hold-blocked";
    if (dx?.severity === "warn")  return "hold-blocked";  // soft veto — still a block
    return "hold-info";  // confidence-floor / claude-hold
}

function passesFilter(s, filter) {
    const b = bucketOf(s);
    switch (filter) {
        case "all":         return true;
        case "actionable":  return b === "strong" || b === "solid" || b === "marginal";
        case "strong":      return b === "strong";
        case "solid":       return b === "solid";
        case "marginal":    return b === "marginal";
        case "blocked":     return b === "hold-blocked";
        case "hold":        return b === "hold-blocked" || b === "hold-info";
        default:            return true;
    }
}

function SignalFilterBar({ signals, filter, setFilter }) {
    const counts = signals.reduce((acc, s) => {
        const b = bucketOf(s);
        acc[b] = (acc[b] || 0) + 1;
        return acc;
    }, {});
    const actionable = (counts.strong || 0) + (counts.solid || 0) + (counts.marginal || 0);
    const holdAll = (counts["hold-blocked"] || 0) + (counts["hold-info"] || 0);

    const chips = [
        { id: "all",        label: "ALL",         count: signals.length,           color: "neutral" },
        { id: "actionable", label: "ACTIONABLE",  count: actionable,               color: "green" },
        { id: "strong",     label: "STRONG",      count: counts.strong || 0,       color: "green" },
        { id: "solid",      label: "SOLID",       count: counts.solid || 0,        color: "neutral" },
        { id: "marginal",   label: "MARGINAL",    count: counts.marginal || 0,     color: "amber" },
        { id: "blocked",    label: "BLOCKED",     count: counts["hold-blocked"] || 0, color: "red" },
        { id: "hold",       label: "ALL HOLDS",   count: holdAll,                  color: "neutral" },
    ];

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-3" data-testid="signal-filter-bar">
            <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono text-[10px] text-[#52525B] tracking-widest mr-1">FILTER ·</span>
                {chips.map(c => (
                    <FilterChip key={c.id} chip={c} active={filter === c.id} onClick={() => setFilter(c.id)} />
                ))}
            </div>
        </div>
    );
}

function FilterChip({ chip, active, onClick }) {
    const palette = {
        green:   { ring: "border-[#00FF41]/40", fg: "text-[#00FF41]", bg: "bg-[#00FF41]/10" },
        amber:   { ring: "border-[#FFB000]/40", fg: "text-[#FFB000]", bg: "bg-[#FFB000]/10" },
        red:     { ring: "border-[#FF3B30]/40", fg: "text-[#FF3B30]", bg: "bg-[#FF3B30]/10" },
        neutral: { ring: "border-[#1F1F1F]",    fg: "text-[#A1A1AA]", bg: "bg-[#0A0A0A]" },
    }[chip.color];

    const activeCls = active
        ? `${palette.fg} ${palette.bg} ${palette.ring}`
        : "text-[#52525B] bg-transparent border-[#1F1F1F] hover:text-[#A1A1AA] hover:border-[#333333]";

    return (
        <button type="button" onClick={onClick}
            data-testid={`signal-filter-${chip.id}`}
            disabled={chip.count === 0 && chip.id !== "all"}
            className={`font-mono text-[10px] tracking-widest px-2 py-1 border transition-colors disabled:opacity-30 disabled:cursor-not-allowed ${activeCls}`}>
            {chip.label} <span className="ml-1 font-display font-bold">{chip.count}</span>
        </button>
    );
}

function FilteredGrid({ signals, filter, accounts, onExecute, onDelete }) {
    const filtered = signals.filter(s => passesFilter(s, filter));
    if (filtered.length === 0) {
        return (
            <div className="border border-dashed border-[#1F1F1F] p-12 text-center" data-testid="signals-empty-filtered">
                <Brain className="w-10 h-10 text-[#52525B] mx-auto mb-3" />
                <div className="font-display font-bold text-lg mb-1">No signals match this filter</div>
                <div className="text-sm text-[#A1A1AA]">
                    Try a different filter or click <em>Generate Signals</em> for fresh ones.
                </div>
            </div>
        );
    }
    return (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {filtered.map(s => (
                <SignalCard key={s.id} s={s} accounts={accounts} onExecute={onExecute} onDelete={onDelete} />
            ))}
        </div>
    );
}
