import { useCallback, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { createPasskey, webauthnSupported } from "@/lib/webauthnClient";
import { Fingerprint, Plus, Trash2 } from "lucide-react";

export const PasskeySection = () => {
    const [creds, setCreds] = useState([]);
    const [label, setLabel] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const [msg, setMsg] = useState("");

    const load = useCallback(async () => {
        try {
            const { data } = await api.get("/auth/webauthn/credentials");
            setCreds(data.credentials || []);
        } catch { /* non-admin or unavailable */ }
    }, []);

    useEffect(() => { load(); }, [load]);

    const addPasskey = async () => {
        if (busy) return;
        setBusy(true); setErr(""); setMsg("");
        try {
            const { data } = await api.post("/auth/webauthn/register/begin",
                { origin: window.location.origin });
            const credential = await createPasskey(data.options);
            await api.post("/auth/webauthn/register/complete", {
                challenge_id: data.challenge_id, credential,
                label: label.trim() || undefined,
            });
            setLabel("");
            setMsg("Passkey registered. You can now use it for security checks.");
            await load();
        } catch (e) {
            setErr(e?.name === "NotAllowedError"
                ? "Passkey prompt was cancelled or timed out."
                : formatApiError(e));
        } finally { setBusy(false); }
    };

    const remove = async (id) => {
        setErr(""); setMsg("");
        try {
            await api.delete(`/auth/webauthn/credentials/${id}`);
            await load();
        } catch (e) { setErr(formatApiError(e)); }
    };

    return (
        <section className={`border ${creds.length ? "border-[#00FF41]/40" : "border-[#1F1F1F]"} bg-[#0A0A0A]`} data-testid="passkey-section">
            <div className="px-5 py-3 border-b border-[#1F1F1F] flex items-center gap-2">
                <Fingerprint className={`w-4 h-4 ${creds.length ? "text-[#00FF41]" : "text-[#A1A1AA]"}`} />
                <div className="flex-1">
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">SECTION 04 · ADMIN PASSKEYS</div>
                    <div className="font-display font-bold text-lg tracking-tight">Passkeys &amp; hardware security keys</div>
                </div>
                <div className={`font-mono text-[10px] tracking-widest px-2 py-1 border ${creds.length ? "border-[#00FF41]/40 text-[#00FF41] bg-[#00FF41]/10" : "border-[#1F1F1F] text-[#52525B]"}`}
                    data-testid="passkey-status-pill">
                    {creds.length ? `● ${creds.length} ENROLLED` : "○ NONE"}
                </div>
            </div>
            <div className="p-5 space-y-4">
                {err && <div className="border border-[#FF3B30]/30 bg-[#FF3B30]/10 px-3 py-2 text-xs text-[#FF3B30] font-mono" data-testid="passkey-error">{err}</div>}
                {msg && <div className="border border-[#00FF41]/30 bg-[#00FF41]/10 px-3 py-2 text-xs text-[#00FF41] font-mono" data-testid="passkey-success">{msg}</div>}
                <p className="text-xs text-[#A1A1AA] leading-relaxed">
                    Register a passkey (Face ID, Windows Hello, or a hardware key like YubiKey) to
                    approve live-sensitive admin actions without typing a TOTP code.
                </p>
                {creds.length > 0 && (
                    <div className="space-y-2" data-testid="passkey-list">
                        {creds.map((c) => (
                            <div key={c.credential_id} className="flex items-center gap-3 border border-[#1F1F1F] px-3 py-2"
                                data-testid={`passkey-item-${c.credential_id.slice(0, 8)}`}>
                                <Fingerprint className="w-4 h-4 text-[#00FF41]" />
                                <div className="flex-1 min-w-0">
                                    <div className="text-sm text-white truncate">{c.label}</div>
                                    <div className="font-mono text-[10px] text-[#52525B]">
                                        {c.device_type === "multi_device" ? "SYNCED PASSKEY" : "DEVICE-BOUND"}
                                        {c.last_used_at ? ` · LAST USED ${new Date(c.last_used_at).toLocaleDateString()}` : " · NEVER USED"}
                                    </div>
                                </div>
                                <button onClick={() => remove(c.credential_id)}
                                    data-testid={`passkey-remove-${c.credential_id.slice(0, 8)}`}
                                    className="p-1.5 border border-[#1F1F1F] text-[#A1A1AA] hover:text-[#FF3B30] hover:border-[#FF3B30]/40 transition-colors">
                                    <Trash2 className="w-3.5 h-3.5" />
                                </button>
                            </div>
                        ))}
                    </div>
                )}
                {webauthnSupported() ? (
                    <div className="flex gap-2">
                        <input value={label} onChange={(e) => setLabel(e.target.value)}
                            placeholder="Label (e.g. YubiKey 5C)" maxLength={60}
                            data-testid="passkey-label-input"
                            className="flex-1 bg-[#050505] border border-[#1F1F1F] focus:border-[#00FF41] px-3 py-2 text-sm font-mono outline-none transition-colors" />
                        <button onClick={addPasskey} disabled={busy}
                            data-testid="passkey-add-button"
                            className="bg-[#00FF41] hover:bg-[#00E53A] disabled:opacity-50 text-black font-medium px-5 py-2 text-xs tracking-widest flex items-center gap-2 transition-colors">
                            <Plus className="w-3.5 h-3.5" /> {busy ? "WAITING…" : "ADD PASSKEY"}
                        </button>
                    </div>
                ) : (
                    <p className="text-xs text-[#FFB000] font-mono" data-testid="passkey-unsupported">
                        This browser does not support WebAuthn passkeys.
                    </p>
                )}
            </div>
        </section>
    );
};
