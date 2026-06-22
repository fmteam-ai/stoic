/**
 * Capture diagnostics for bug reports — screenshot + console logs + app context.
 *
 * Hooks into window.console at load time (one-time install) to keep a ring
 * buffer of the last 50 log/warn/error calls. Uses html-to-image to grab the
 * current viewport as a PNG data URL.
 */
import { toPng } from "html-to-image";

const BUFFER_SIZE = 50;
const _logs = [];
let _installed = false;

export function installConsoleCapture() {
    if (_installed || typeof window === "undefined") return;
    _installed = true;
    const orig = { log: console.log, warn: console.warn, error: console.error };
    ["log", "warn", "error"].forEach((level) => {
        console[level] = (...args) => {
            try {
                _logs.push({
                    level,
                    ts: new Date().toISOString(),
                    msg: args.map((a) => {
                        if (a instanceof Error) return `${a.name}: ${a.message}`;
                        if (typeof a === "object") {
                            try { return JSON.stringify(a).slice(0, 300); }
                            catch { return String(a); }
                        }
                        return String(a).slice(0, 300);
                    }).join(" "),
                });
                if (_logs.length > BUFFER_SIZE) _logs.shift();
            } catch { /* never throw from console hook */ }
            orig[level](...args);
        };
    });
    window.addEventListener("error", (e) => {
        _logs.push({ level: "error", ts: new Date().toISOString(),
                     msg: `[window.onerror] ${e.message} @ ${e.filename}:${e.lineno}` });
        if (_logs.length > BUFFER_SIZE) _logs.shift();
    });
    window.addEventListener("unhandledrejection", (e) => {
        _logs.push({ level: "error", ts: new Date().toISOString(),
                     msg: `[unhandledrejection] ${e.reason}` });
        if (_logs.length > BUFFER_SIZE) _logs.shift();
    });
}

export function getRecentLogs() {
    return [..._logs];
}

/**
 * Capture the current page as a PNG data URL. Skips the Co-Pilot widget itself
 * (would just show the chat overlay) by hiding any element with data-no-capture.
 */
export async function captureViewport() {
    const node = document.body;
    // Hide anything tagged not-to-capture
    const hidden = Array.from(document.querySelectorAll("[data-no-capture]"));
    const previous = hidden.map((el) => el.style.visibility);
    hidden.forEach((el) => { el.style.visibility = "hidden"; });
    try {
        const dataUrl = await toPng(node, {
            cacheBust: true,
            pixelRatio: 1,
            backgroundColor: "#050505",
            skipFonts: true,  // avoid SecurityError on cross-origin Google Fonts
            filter: (n) => !(n.tagName === "IFRAME"),
        });
        return dataUrl;
    } catch (err) {
        console.warn("[capture] toPng failed", err?.message);
        return null;
    } finally {
        hidden.forEach((el, i) => { el.style.visibility = previous[i] || ""; });
    }
}

export function collectContext() {
    return {
        url: window.location.href,
        user_agent: navigator.userAgent,
        viewport: { w: window.innerWidth, h: window.innerHeight,
                    dpr: window.devicePixelRatio || 1 },
        console_logs: getRecentLogs(),
    };
}
