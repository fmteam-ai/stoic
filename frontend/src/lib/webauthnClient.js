// WebAuthn browser ceremony helpers (iter-177) — base64url <-> ArrayBuffer
// conversions around navigator.credentials.create()/get().

function bufToB64u(buffer) {
    const bytes = new Uint8Array(buffer);
    let bin = "";
    bytes.forEach((b) => { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function b64uToBuf(b64u) {
    const b64 = b64u.replace(/-/g, "+").replace(/_/g, "/");
    const pad = b64.length % 4 ? "=".repeat(4 - (b64.length % 4)) : "";
    const bin = atob(b64 + pad);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
}

export function webauthnSupported() {
    return typeof window !== "undefined" && !!window.PublicKeyCredential;
}

export async function createPasskey(options) {
    const pk = { ...options };
    pk.challenge = b64uToBuf(pk.challenge);
    pk.user = { ...pk.user, id: b64uToBuf(pk.user.id) };
    pk.excludeCredentials = (pk.excludeCredentials || []).map((c) => ({
        ...c, id: b64uToBuf(c.id),
    }));
    const cred = await navigator.credentials.create({ publicKey: pk });
    if (!cred) throw new Error("No credential returned");
    const r = cred.response;
    return {
        id: cred.id,
        rawId: bufToB64u(cred.rawId),
        type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment || null,
        clientExtensionResults: cred.getClientExtensionResults(),
        response: {
            attestationObject: bufToB64u(r.attestationObject),
            clientDataJSON: bufToB64u(r.clientDataJSON),
            transports: r.getTransports ? r.getTransports() : [],
        },
    };
}

export async function getPasskeyAssertion(options) {
    const pk = { ...options };
    pk.challenge = b64uToBuf(pk.challenge);
    pk.allowCredentials = (pk.allowCredentials || []).map((c) => ({
        ...c, id: b64uToBuf(c.id),
    }));
    const cred = await navigator.credentials.get({ publicKey: pk });
    if (!cred) throw new Error("No assertion returned");
    const r = cred.response;
    return {
        id: cred.id,
        rawId: bufToB64u(cred.rawId),
        type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment || null,
        clientExtensionResults: cred.getClientExtensionResults(),
        response: {
            authenticatorData: bufToB64u(r.authenticatorData),
            clientDataJSON: bufToB64u(r.clientDataJSON),
            signature: bufToB64u(r.signature),
            userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
        },
    };
}
