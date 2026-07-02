import { useEffect, useState, useCallback, useMemo } from "react";
import api from "@/lib/api";
import { Activity, Pause, AlertTriangle, Ban, CheckCircle2, Clock, ChevronDown, ChevronUp } from "lucide-react";

// Threshold at which we switch from the full expanded list to a dropdown+preview.
// Below this, all rows render inline; above, users pick one to inspect + can
// toggle "show all" if they explicitly want the wall-of-rows.
const DROPDOWN_THRESHOLD = 10;

// Color / icon per pulse level. Matches the rest of the STOIC palette.
const LEVEL_STYLE = {
    info:  { border: "border-[#1F1F1F]",       bg: "bg-[#0A0A0A]",      fg: "text-[#A1A1AA]", chip: "text-[#A1A1AA] border-[#1F1F1F]", Icon: Pause },
    warn:  { border: "border-[#FFB000]/30",    bg: "bg-[#FFB000]/5",    fg: "text-[#FFB000]", chip: "text-[#FFB000] border-[#FFB000]/40", Icon: AlertTriangle },
    block: { border: "border-[#FF3B30]/30",    bg: "bg-[#FF3B30]/5",    fg: "text-[#FF3B30]", chip: "text-[#FF3B30] border-[#FF3B30]/40", Icon: Ban },
};
const ACTION_STYLE = {
    EXEC:    { fg: "text-[#00FF41]", Icon: CheckCircle2 },
    BUY:     { fg: "text-[#00FF41]", Icon: CheckCircle2 },
    SELL:    { fg: "text-[#00FF41]", Icon: CheckCircle2 },
    HOLD:    { fg: "text-[#A1A1AA]", Icon: Pause },
    SKIP:    { fg: "text-[#A1A1AA]", Icon: Pause },
    BLOCKED: { fg: "text-[#FF3B30]", Icon: Ban },
};

function ago(seconds) {
    if (seconds == null) return "—";
    if (seconds < 60) return `${seconds}s ago`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m ago`;
}

function untilStr(iso) {
    if (!iso) return null;
    const ms = new Date(iso).getTime() - Date.now();
    if (ms <= 0) return "now";
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

function PulseRow({ item }) {
    const p = item.pulse;
    const lvl = LEVEL_STYLE[(p?.level) || "info"] || LEVEL_STYLE.info;
    const actStyle = ACTION_STYLE[(p?.action) || "HOLD"] || ACTION_STYLE.HOLD;
    const ActIcon = actStyle.Icon;
    const isShadow = item.paper_shadow_mode && !item.active;
    const noPulse = !p;
    const next = p?.next_eligible_at ? untilStr(p.next_eligible_at) : null;

    return (
        <div className={`flex items-start gap-3 px-4 py-3 border ${lvl.border} ${lvl.bg}`}
             data-testid={`bot-pulse-row-${item.config_id}`}>
            {/* Left: bot label + state chip */}
            <div className="shrink-0 w-44 lg:w-56">
                <div className="text-xs font-mono tracking-widest text-white truncate">
                    {item.label}
                </div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                    {item.active && !isShadow && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 border border-[#00FF41]/40 text-[#00FF41] text-[10px] font-mono tracking-widest">
                            <span className="w-1.5 h-1.5 bg-[#00FF41] rounded-full animate-pulse" /> LIVE
                        </span>
                    )}
                    {isShadow && (
                        <span className="inline-flex items-center px-1.5 py-0.5 border border-[#FFB000]/40 text-[#FFB000] text-[10px] font-mono tracking-widest">
                            SHADOW
                        </span>
                    )}
                    {!item.active && !isShadow && (
                        <span className="inline-flex items-center px-1.5 py-0.5 border border-[#1F1F1F] text-[#52525B] text-[10px] font-mono tracking-widest">
                            OFF
                        </span>
                    )}
                    {item.symbols.length > 0 && (
                        <span className="inline-flex items-center px-1.5 py-0.5 border border-[#1F1F1F] text-[#A1A1AA] text-[10px] font-mono tracking-widest">
                            {item.symbols.join(" · ")}
                        </span>
                    )}
                    {item.strategy_label && (
                        <span
                            title={`Strategy preset: ${item.strategy_label}`}
                            data-testid={`bot-pulse-strategy-${item.config_id}`}
                            className="inline-flex items-center px-1.5 py-0.5 border border-[#0099FF]/40 text-[#0099FF] text-[10px] font-mono tracking-widest">
                            {item.strategy_label.toUpperCase()}
                        </span>
                    )}
                </div>
            </div>

            {/* Middle: latest verdict + human reason */}
            <div className="flex-1 min-w-0">
                {noPulse ? (
                    <div className="text-xs text-[#52525B]">
                        {item.active
                            ? "Waiting for first cycle… (bot loop runs every 60s)"
                            : "Bot is off — enable it on Bot Config to start scanning."}
                    </div>
                ) : (
                    <>
                        <div className="flex items-center gap-2 flex-wrap">
                            <span className={`inline-flex items-center gap-1 ${actStyle.fg} text-xs font-mono tracking-widest`}>
                                <ActIcon className="w-3 h-3" /> {p.action}
                            </span>
                            {p.symbol && (
                                <span className="text-[10px] font-mono tracking-widest text-[#52525B]">
                                    · {p.symbol}
                                </span>
                            )}
                            <span className="text-[10px] font-mono tracking-widest text-[#52525B] ml-auto">
                                {ago(item.stale_seconds)}
                            </span>
                        </div>
                        <div className={`mt-1 text-xs leading-relaxed ${lvl.fg}`}>
                            {p.reason}
                        </div>
                        {next && (
                            <div className="mt-1 flex items-center gap-1 text-[10px] font-mono tracking-widest text-[#52525B]">
                                <Clock className="w-3 h-3" /> NEXT IN {next.toUpperCase()}
                            </div>
                        )}
                    </>
                )}
            </div>
        </div>
    );
}

export default function BotPulsePanel() {
    const [data, setData] = useState(null);
    const [open, setOpen] = useState(true);
    const [err, setErr] = useState(null);
    // In dropdown mode (>10 accounts), which config_id is selected for
    // preview. "__all__" means user explicitly toggled the wall-of-rows view.
    const [selectedId, setSelectedId] = useState("");

    const load = useCallback(async () => {
        try {
            const r = await api.get("/bot/pulse");
            setData(r.data);
            setErr(null);
        } catch (e) {
            setErr(e?.response?.data?.detail || "Failed to load bot pulse");
        }
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 30000);
        return () => clearInterval(id);
    }, [load]);

    // Rank items by severity/freshness — we use the ordering both to pick the
    // banner "top" item AND to default-select the loudest row in dropdown mode.
    const rankedItems = useMemo(() => {
        const items = (data?.items) || [];
        const rank = { block: 3, warn: 2, info: 1, undefined: 0 };
        return [...items].sort((a, b) => {
            const ra = rank[(a.pulse?.level)] || 0;
            const rb = rank[(b.pulse?.level)] || 0;
            if (rb !== ra) return rb - ra;
            return (a.stale_seconds ?? 9999) - (b.stale_seconds ?? 9999);
        });
    }, [data]);

    // Auto-select the top-ranked config the first time we hit dropdown mode
    // (or when the current selection is no longer in the list).
    useEffect(() => {
        if (!data) return;
        const items = data.items || [];
        if (items.length <= DROPDOWN_THRESHOLD) return;
        const ids = items.map(i => i.config_id);
        if (selectedId && selectedId !== "__all__" && ids.includes(selectedId)) return;
        if (selectedId === "__all__") return;
        const top = rankedItems[0];
        if (top) setSelectedId(top.config_id);
    }, [data, selectedId, rankedItems]);

    if (err) {
        return (
            <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-4 py-2 text-xs text-[#FF3B30] font-mono"
                 data-testid="bot-pulse-error">
                BOT PULSE · {err}
            </div>
        );
    }
    if (!data) return null;
    const items = data.items || [];
    if (items.length === 0) return null;

    // Banner state — if any active bot has been silent without a recent EXEC,
    // we surface the loudest reason at the top so the user can see it without
    // scrolling. We rank by level (block > warn > info) and pick the freshest.
    const activeItems = items.filter(i => i.active || i.paper_shadow_mode);
    const activeRanked = rankedItems.filter(i => i.active || i.paper_shadow_mode);
    const top = activeRanked[0];

    const useDropdown = items.length > DROPDOWN_THRESHOLD;
    // What to render in the body when dropdown mode is active.
    const dropdownVisible = (useDropdown && selectedId && selectedId !== "__all__")
        ? items.filter(i => i.config_id === selectedId)
        : items;

    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="bot-pulse-panel">
            {/* Header / summary line */}
            <button onClick={() => setOpen(o => !o)}
                    className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-[#1F1F1F]/30 transition-colors"
                    data-testid="bot-pulse-toggle">
                <Activity className="w-3.5 h-3.5 text-[#00FF41]" />
                <span className="font-mono text-[10px] tracking-widest text-[#A1A1AA]">BOT PULSE</span>
                {top?.pulse && (
                    <span className={`font-mono text-[10px] tracking-widest ${LEVEL_STYLE[top.pulse.level || "info"].fg} truncate`}>
                        · {top.label.toUpperCase()} · {top.pulse.action} · {top.pulse.reason}
                    </span>
                )}
                <span className="ml-auto flex items-center gap-2">
                    <span className="text-[10px] font-mono tracking-widest text-[#52525B]">
                        {activeItems.length} ACTIVE · {items.length} TOTAL · LOOP {data.loop_interval_sec}s
                    </span>
                    {open ? <ChevronUp className="w-3.5 h-3.5 text-[#52525B]" />
                          : <ChevronDown className="w-3.5 h-3.5 text-[#52525B]" />}
                </span>
            </button>

            {open && useDropdown && (
                <div className="border-t border-[#1F1F1F] px-4 py-2.5 flex items-center gap-3 flex-wrap bg-[#050505]"
                     data-testid="bot-pulse-selector-row">
                    <span className="font-mono text-[10px] tracking-widest text-[#52525B]">SELECT ACCOUNT ·</span>
                    <select
                        value={selectedId || ""}
                        onChange={(e) => setSelectedId(e.target.value)}
                        data-testid="bot-pulse-selector"
                        className="bg-[#0A0A0A] border border-[#1F1F1F] px-2 py-1 text-xs font-mono text-white focus:outline-none focus:border-[#00FF41] transition-colors max-w-[420px]">
                        {rankedItems.map(i => {
                            const state = i.active ? "LIVE" : (i.paper_shadow_mode ? "SHADOW" : "OFF");
                            const strat = i.strategy_label ? ` · ${i.strategy_label}` : "";
                            const lvl = i.pulse?.level ? ` · ${i.pulse.level.toUpperCase()}` : "";
                            return (
                                <option key={i.config_id} value={i.config_id}>
                                    {`[${state}] ${i.label}${strat}${lvl}`}
                                </option>
                            );
                        })}
                    </select>
                    <button
                        onClick={() => setSelectedId(selectedId === "__all__" ? (rankedItems[0]?.config_id || "") : "__all__")}
                        data-testid="bot-pulse-show-all"
                        className={`px-2 py-1 text-[10px] font-mono tracking-widest border transition-colors ${
                            selectedId === "__all__"
                                ? "border-[#00FF41] text-[#00FF41]"
                                : "border-[#1F1F1F] text-[#A1A1AA] hover:border-[#333333]"
                        }`}>
                        {selectedId === "__all__" ? `HIDE (${items.length})` : `SHOW ALL (${items.length})`}
                    </button>
                </div>
            )}

            {open && (
                <div className="border-t border-[#1F1F1F] divide-y divide-[#1F1F1F]"
                     data-testid="bot-pulse-rows">
                    {dropdownVisible.map(item => (
                        <PulseRow key={item.config_id} item={item} />
                    ))}
                </div>
            )}
        </div>
    );
}
