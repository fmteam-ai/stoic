import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { Siren, Check, CheckCheck, BellOff, Bell } from "lucide-react";

const SEV_STYLE = {
    critical: "border-[#FF3B30]/40 text-[#FF3B30]",
    warning: "border-[#FFD700]/40 text-[#FFD700]",
    info: "border-[#00FF41]/40 text-[#00FF41]",
};

const fmtUntil = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

export const AlertsCard = () => {
    const [alerts, setAlerts] = useState(null);
    const [meta, setMeta] = useState({});
    const [mutes, setMutes] = useState({});
    const [mutableKinds, setMutableKinds] = useState(null);
    const [hidden, setHidden] = useState(false);
    const [busy, setBusy] = useState(false);

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/ops/alerts");
            setAlerts(data.alerts || []);
            setMutes(Object.fromEntries((data.mutes || []).map(m => [m.kind, m.muted_until])));
            setMutableKinds(data.mutable_kinds || null);
            setMeta({ unacked: data.unacked, unacked_critical: data.unacked_critical, as_of: data.as_of });
        } catch {
            setHidden(true);
        }
    }, []);

    useEffect(() => {
        load();
        const id = setInterval(load, 30000);
        return () => clearInterval(id);
    }, [load]);

    const run = async (fn) => { setBusy(true); try { await fn(); await load(); } finally { setBusy(false); } };
    const ack = (id) => run(() => api.post(`/ops/alerts/${id}/ack`));
    const ackAll = () => run(() => api.post("/ops/alerts/ack-all"));
    // N109-2 — mutes the Telegram/e-mail pushes of this kind for 24 h; the alert row stays open and keeps gating
    const mute = (kind) => run(() => api.post("/ops/alerts/mute", { kind, hours: 24 }));
    const unmute = (kind) => run(() => api.delete(`/ops/alerts/mute/${kind}`));

    if (hidden || alerts === null) return null;
    const hasCritical = alerts.some(a => a.severity === "critical");

    return (
        <div className={`border p-4 ${alerts.length === 0 ? "border-[#1F1F1F] bg-[#0A0A0A]"
            : hasCritical ? "border-[#FF3B30]/30 bg-[#FF3B30]/5" : "border-[#FFD700]/30 bg-[#FFD700]/5"}`}
            data-testid="ops-alerts-card">
            <div className="flex items-center justify-between flex-wrap gap-2">
                <div className="flex items-center gap-2">
                    <Siren className={`w-4 h-4 ${alerts.length === 0 ? "text-[#00FF41]" : hasCritical ? "text-[#FF3B30]" : "text-[#FFD700]"}`} />
                    <span className="font-display font-bold text-sm text-white">Ops Alerts</span>
                    <span className="font-mono text-[10px] tracking-widest px-2 py-0.5 border border-[#1F1F1F] text-[#A1A1AA]"
                        data-testid="ops-alerts-count"
                        title={meta.as_of ? `single snapshot as of ${meta.as_of} — synthetic/test alerts excluded` : undefined}>
                        {meta.unacked ?? alerts.length} UNACKED{meta.unacked_critical ? ` · ${meta.unacked_critical} CRITICAL` : ""}
                    </span>
                </div>
                {alerts.length > 0 && (
                    <button onClick={ackAll} disabled={busy} data-testid="ops-alerts-ack-all"
                        title="Acknowledge every row. A persisting condition re-opens its row within a minute — only its notifications pause (6 h)."
                        className="flex items-center gap-1.5 font-mono text-[10px] tracking-widest text-[#A1A1AA] hover:text-white border border-[#1F1F1F] px-2 py-1 disabled:opacity-50">
                        <CheckCheck className="w-3 h-3" /> ACK ALL
                    </button>
                )}
            </div>
            {alerts.length === 0 ? (
                <div className="font-mono text-[10px] tracking-widest text-[#52525B] mt-3" data-testid="ops-alerts-empty">
                    NO ACTIVE ALERTS — ALL SYSTEMS NOMINAL
                </div>
            ) : (
                <div className="mt-3 space-y-2">
                    {alerts.map(a => {
                        const snoozed = a.snoozed_until && new Date(a.snoozed_until) > new Date();
                        const kindMuted = !!mutes[a.kind];
                        // N111-6 — only kinds the server accepts for a mute get the button (else a 400)
                        const canMute = !mutableKinds || mutableKinds.includes(a.kind);
                        return (
                            <div key={a.id} className={`border ${SEV_STYLE[a.severity] || SEV_STYLE.warning} px-3 py-2 flex items-center justify-between gap-3`}
                                data-testid={`ops-alert-${a.kind}`}>
                                <div className="min-w-0">
                                    <div className="font-mono text-[9px] tracking-widest opacity-70 flex items-center gap-2 flex-wrap">
                                        <span>{(a.severity || "").toUpperCase()} · {a.kind} · ×{a.occurrences || 1}</span>
                                        {snoozed && (
                                            <span className="px-1.5 py-0.5 border border-current/40 text-[#A1A1AA] flex items-center gap-1" data-testid={`ops-alert-snoozed-${a.kind}`}
                                                title="Notifications (Telegram / e-mail) are muted until then. The alert itself stays open and keeps gating.">
                                                <BellOff className="w-2.5 h-2.5" /> NOTIFICATIONS SNOOZED UNTIL {fmtUntil(a.snoozed_until)}
                                            </span>
                                        )}
                                    </div>
                                    <div className="text-xs mt-0.5 truncate">{a.message}</div>
                                </div>
                                <div className="shrink-0 flex items-center gap-1.5">
                                    {kindMuted ? (
                                        <button onClick={() => unmute(a.kind)} disabled={busy} data-testid={`ops-alert-unmute-${a.kind}`}
                                            title="Resume notifications for this alert kind"
                                            className="flex items-center gap-1 font-mono text-[10px] tracking-widest border border-[#1F1F1F] text-[#A1A1AA] px-2 py-1 hover:bg-white/5 disabled:opacity-50">
                                            <Bell className="w-3 h-3" /> UNMUTE
                                        </button>
                                    ) : canMute ? (
                                        <button onClick={() => mute(a.kind)} disabled={busy} data-testid={`ops-alert-mute-${a.kind}`}
                                            title="Mute Telegram / e-mail pushes for this alert kind for 24 h. The row stays open."
                                            className="flex items-center gap-1 font-mono text-[10px] tracking-widest border border-[#1F1F1F] text-[#A1A1AA] px-2 py-1 hover:bg-white/5 disabled:opacity-50">
                                            <BellOff className="w-3 h-3" /> MUTE 24H
                                        </button>
                                    ) : null}
                                    <button onClick={() => ack(a.id)} disabled={busy}
                                        data-testid={`ops-alert-ack-${a.kind}`}
                                        title="Acknowledge. If the condition persists the row re-opens within a minute; only its notifications pause for 6 h."
                                        className="flex items-center gap-1 font-mono text-[10px] tracking-widest border border-current px-2 py-1 hover:bg-white/5 disabled:opacity-50">
                                        <Check className="w-3 h-3" /> ACK
                                    </button>
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
};
