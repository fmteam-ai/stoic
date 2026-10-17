// v1.60.40 — Config Drift Check: after SAVE, every field we sent must come back from the server unchanged.
// Anything that differs is listed ("saved X, server has Y") so a silent revert (15 Oct: gold_off_hours_*,
// which _serialize() dropped) or a server-side clamp/coercion is visible immediately.

const normalise = (v) => {
    if (v === undefined || v === null || v === "") return null;
    if (typeof v === "boolean") return v;
    if (typeof v === "number") return Number.isFinite(v) ? Number(v.toFixed(6)) : null;
    if (typeof v === "string") {
        const n = Number(v);
        return v.trim() !== "" && Number.isFinite(n) ? Number(n.toFixed(6)) : v;
    }
    if (Array.isArray(v)) return v.map(normalise);
    if (typeof v === "object") {
        return Object.keys(v).sort().reduce((acc, k) => { acc[k] = normalise(v[k]); return acc; }, {});
    }
    return v;
};

export const fmtValue = (v) => {
    if (v === undefined || v === null || v === "") return "unset";
    if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
};

// Returns [{ field, sent, server }] for every sent field whose server value differs. Missing on the
// server counts as drift (that is exactly the bug class this guards against).
export function diffSavedConfig(sent, server) {
    if (!sent || typeof sent !== "object") return [];
    const got = server || {};
    const out = [];
    for (const field of Object.keys(sent)) {
        const a = normalise(sent[field]);
        const b = normalise(got[field]);
        if (JSON.stringify(a) !== JSON.stringify(b)) out.push({ field, sent: sent[field], server: got[field] });
    }
    return out;
}

// v1.60.48 (audit item 10) — PATCH semantics: only the fields the user actually changed since the page loaded
// (or last saved) are sent, so a full-form save can no longer revert values written by background jobs
// (auto-heal, loss advisor, cooldown clamps) after the page was opened.
export function changedFields(payload, baseline) {
    if (!baseline || typeof baseline !== "object") return { ...payload };
    const out = {};
    for (const field of Object.keys(payload || {})) {
        if (JSON.stringify(normalise(payload[field])) !== JSON.stringify(normalise(baseline[field]))) out[field] = payload[field];
    }
    return out;
}
