import { useEffect, useState, useCallback } from "react";
import { AppLayout, PageHeader } from "@/components/AppLayout";
import api, { formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Loader2, Landmark, Pencil, Plus, Trash2, X } from "lucide-react";
import { AccountEnvironmentsPanel } from "@/components/admin/AccountEnvironmentsPanel";
import { AccountPositionModesPanel } from "@/components/admin/AccountPositionModesPanel";
import { InventoryGoLivePanel } from "@/components/admin/InventoryGoLivePanel";

const EMPTY = {
    broker_id: "", name: "", server_aliases: [], symbol_map: {},
    contract_specs: {}, stop_level_points: 0, freeze_level_points: 0, sessions: [],
};

function EditModal({ broker, onClose, onSaved }) {
    const isNew = !broker.id;
    const [f, setF] = useState({
        ...broker,
        aliasesText: (broker.server_aliases || []).join("\n"),
        symbolMapText: JSON.stringify(broker.symbol_map || {}, null, 2),
        specsText: JSON.stringify(broker.contract_specs || {}, null, 2),
    });
    const [busy, setBusy] = useState(false);

    const save = async () => {
        let symbol_map, contract_specs;
        try { symbol_map = JSON.parse(f.symbolMapText || "{}"); }
        catch { return toast.error("Symbol map is not valid JSON"); }
        try { contract_specs = JSON.parse(f.specsText || "{}"); }
        catch { return toast.error("Contract specs is not valid JSON"); }
        const payload = {
            broker_id: f.broker_id, name: f.name,
            server_aliases: f.aliasesText.split("\n").map(s => s.trim()).filter(Boolean),
            symbol_map, contract_specs,
            stop_level_points: Number(f.stop_level_points) || 0,
            freeze_level_points: Number(f.freeze_level_points) || 0,
            sessions: broker.sessions || [],
        };
        setBusy(true);
        try {
            if (isNew) await api.post("/admin/brokers", payload);
            else await api.put(`/admin/brokers/${broker.broker_id}`, payload);
            toast.success("Broker saved");
            onSaved();
        } catch (e) { toast.error(formatApiError(e)); }
        finally { setBusy(false); }
    };

    const Field = ({ label, children }) => (
        <div>
            <div className="text-[9px] font-mono tracking-widest text-[#52525B] uppercase mb-1">{label}</div>
            {children}
        </div>
    );
    const inputCls = "w-full bg-[#0F0F0F] border border-[#1F1F1F] focus:border-[#00FF41] outline-none px-3 py-2 text-sm font-mono";

    return (
        <div className="fixed inset-0 z-[70] bg-black/80 flex items-center justify-center p-4" data-testid="broker-edit-modal">
            <div className="w-full max-w-2xl bg-[#0A0A0A] border border-[#1F1F1F] max-h-[90vh] overflow-y-auto">
                <div className="flex items-center justify-between p-4 border-b border-[#1F1F1F]">
                    <div className="font-display text-white">{isNew ? "Add Broker" : `Edit ${broker.name}`}</div>
                    <button onClick={onClose} data-testid="broker-modal-close"><X className="w-4 h-4 text-[#52525B] hover:text-white" /></button>
                </div>
                <div className="p-4 space-y-4">
                    <div className="grid grid-cols-2 gap-3">
                        <Field label="Broker ID (slug)">
                            <input className={inputCls} value={f.broker_id} disabled={!isNew}
                                data-testid="broker-id-input"
                                onChange={e => setF({ ...f, broker_id: e.target.value })} />
                        </Field>
                        <Field label="Display name">
                            <input className={inputCls} value={f.name} data-testid="broker-name-input"
                                onChange={e => setF({ ...f, name: e.target.value })} />
                        </Field>
                    </div>
                    <Field label="Server aliases (one per line)">
                        <textarea rows={4} className={inputCls} value={f.aliasesText}
                            data-testid="broker-aliases-input"
                            onChange={e => setF({ ...f, aliasesText: e.target.value })} />
                    </Field>
                    <Field label='Symbol map (canonical → broker ticker, JSON — e.g. {"XAUUSD": "GOLD"})'>
                        <textarea rows={4} className={inputCls} value={f.symbolMapText}
                            data-testid="broker-symbolmap-input"
                            onChange={e => setF({ ...f, symbolMapText: e.target.value })} />
                    </Field>
                    <Field label="Contract specs (JSON)">
                        <textarea rows={5} className={inputCls} value={f.specsText}
                            data-testid="broker-specs-input"
                            onChange={e => setF({ ...f, specsText: e.target.value })} />
                    </Field>
                    <div className="grid grid-cols-2 gap-3">
                        <Field label="Stop level (points)">
                            <input type="number" className={inputCls} value={f.stop_level_points}
                                data-testid="broker-stoplevel-input"
                                onChange={e => setF({ ...f, stop_level_points: e.target.value })} />
                        </Field>
                        <Field label="Freeze level (points)">
                            <input type="number" className={inputCls} value={f.freeze_level_points}
                                data-testid="broker-freezelevel-input"
                                onChange={e => setF({ ...f, freeze_level_points: e.target.value })} />
                        </Field>
                    </div>
                    <button onClick={save} disabled={busy} data-testid="broker-save-btn"
                        className="px-5 py-2.5 bg-[#00FF41] text-black text-xs font-mono tracking-widest disabled:opacity-40">
                        {busy ? "SAVING…" : "SAVE BROKER"}
                    </button>
                </div>
            </div>
        </div>
    );
}

export default function AdminBrokers() {
    const [brokers, setBrokers] = useState(null);
    const [editing, setEditing] = useState(null);

    const load = useCallback(async () => {
        try { setBrokers((await api.get("/admin/brokers")).data); }
        catch (e) { toast.error(formatApiError(e)); }
    }, []);
    useEffect(() => { load(); }, [load]);

    const remove = async (b) => {
        if (!window.confirm(`Delete ${b.name} from the registry?`)) return;
        try { await api.delete(`/admin/brokers/${b.broker_id}`); toast.success("Deleted"); load(); }
        catch (e) { toast.error(formatApiError(e)); }
    };

    return (
        <AppLayout>
            <PageHeader title="Broker Registry"
                subtitle="Normalized server aliases, symbol mappings and contract specs — consulted by the execution path before dynamic detection."
                testid="admin-brokers-header"
                action={
                    <button onClick={() => setEditing({ ...EMPTY })} data-testid="broker-add-btn"
                        className="px-3 py-1.5 text-xs font-mono tracking-widest bg-[#00FF41] text-black flex items-center gap-1.5">
                        <Plus className="w-3.5 h-3.5" /> ADD BROKER
                    </button>
                } />

            {brokers === null ? (
                <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-[#52525B]" /></div>
            ) : (
                <div className="border border-[#1F1F1F] rounded-lg overflow-x-auto" data-testid="brokers-table">
                    <table className="w-full text-xs">
                        <thead className="bg-[#0A0A0A] text-[#52525B] font-mono">
                            <tr>
                                <th className="text-left px-4 py-2">BROKER</th>
                                <th className="text-left px-4 py-2">ALIASES</th>
                                <th className="text-left px-4 py-2">SYMBOL MAP</th>
                                <th className="text-right px-4 py-2">STOP/FREEZE</th>
                                <th className="text-right px-4 py-2">ACTIONS</th>
                            </tr>
                        </thead>
                        <tbody>
                            {brokers.map((b, i) => (
                                <tr key={b.broker_id} data-testid={`broker-row-${b.broker_id}`}
                                    className={i % 2 === 0 ? "bg-[#0A0A0A]" : "bg-[#0F0F0F]"}>
                                    <td className="px-4 py-2.5">
                                        <div className="text-white flex items-center gap-2"><Landmark className="w-3.5 h-3.5 text-[#00FF41]" /> {b.name}</div>
                                        <div className="text-[#52525B] font-mono text-[10px]">{b.broker_id}</div>
                                    </td>
                                    <td className="px-4 py-2.5 text-[#A1A1AA] font-mono text-[10px] max-w-[220px]">
                                        {(b.server_aliases || []).slice(0, 3).join(", ")}{(b.server_aliases || []).length > 3 ? ` +${b.server_aliases.length - 3}` : ""}
                                    </td>
                                    <td className="px-4 py-2.5 text-[#A1A1AA] font-mono text-[10px]">
                                        {Object.entries(b.symbol_map || {}).map(([k, v]) => `${k}→${v}`).join(", ")}
                                    </td>
                                    <td className="px-4 py-2.5 text-right font-mono text-[#A1A1AA]">{b.stop_level_points}/{b.freeze_level_points}</td>
                                    <td className="px-4 py-2.5 text-right">
                                        <button onClick={() => setEditing(b)} data-testid={`broker-edit-${b.broker_id}`}
                                            className="text-[#00FF41] hover:underline mr-3"><Pencil className="w-3.5 h-3.5 inline" /></button>
                                        <button onClick={() => remove(b)} data-testid={`broker-delete-${b.broker_id}`}
                                            className="text-[#FF3B30] hover:underline"><Trash2 className="w-3.5 h-3.5 inline" /></button>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
            {editing && <EditModal broker={editing} onClose={() => setEditing(null)}
                onSaved={() => { setEditing(null); load(); }} />}
            <div className="px-4 md:px-8 pb-10 space-y-6"><AccountEnvironmentsPanel /><AccountPositionModesPanel /><InventoryGoLivePanel /></div>
        </AppLayout>
    );
}
