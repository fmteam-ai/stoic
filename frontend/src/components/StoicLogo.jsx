/**
 * STOIC brand marks — distinct logo options for the platform identity.
 *
 * Active mark: <StoicMark />  ← the one used everywhere
 * Alternates:  <StoicPillar />, <StoicWedge />, <StoicSeal />
 *
 * Design rationale per option:
 *   • Pillar — fluted Doric column; doubles as a chart of vertical bars.
 *     Says "ancient strength + structured trading" in one glyph.
 *   • Wedge  — sharp ascending mark with a horizontal "discipline line"
 *     cutting through the apex. Modern fintech feel.
 *   • Seal   — angular octagonal coin/seal frame with a minimal Σ inside.
 *     Premium institutional feel.
 *
 * All marks share the same dimensions and accept identical props so they
 * can be swapped via a single export change.
 */

/**
 * THE PILLAR — fluted Doric column. Three flutes (vertical bars) flanked
 * by a thick capital (top) and base (bottom). Doubles as a 3-bar chart.
 */
export function StoicPillar({ size = 28, className = "", glow = false }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 32 32"
            xmlns="http://www.w3.org/2000/svg"
            className={className}
            aria-label="STOIC"
            style={glow ? { filter: "drop-shadow(0 0 8px rgba(0,255,65,0.5))" } : undefined}
        >
            {/* Capital — top slab */}
            <rect x="3" y="4" width="26" height="3.5" fill="#00FF41" />
            <rect x="5" y="7.5" width="22" height="1.5" fill="#00FF41" />
            {/* Fluted shaft — 4 vertical bars of slightly varying heights so it
                reads as both an architectural column AND a price chart. */}
            <rect x="7"  y="11" width="3" height="10" fill="#00FF41" />
            <rect x="12" y="9"  width="3" height="14" fill="#00FF41" />
            <rect x="17" y="11" width="3" height="12" fill="#00FF41" />
            <rect x="22" y="13" width="3" height="8"  fill="#00FF41" />
            {/* Base — bottom slab */}
            <rect x="5" y="23" width="22" height="1.5" fill="#00FF41" />
            <rect x="3" y="24.5" width="26" height="3.5" fill="#00FF41" />
        </svg>
    );
}

/**
 * THE WEDGE — sharp ascending mark with a "discipline line" through the apex.
 * Reads as a chart arrow that knows when to stop.
 */
export function StoicWedge({ size = 28, className = "", glow = false }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 32 32"
            xmlns="http://www.w3.org/2000/svg"
            className={className}
            aria-label="STOIC"
            style={glow ? { filter: "drop-shadow(0 0 8px rgba(0,255,65,0.5))" } : undefined}
        >
            {/* Ascending wedge */}
            <path d="M 4 26 L 16 6 L 28 26 Z" fill="#00FF41" />
            {/* Discipline line — sharp horizontal cut through upper third */}
            <rect x="2" y="13.5" width="28" height="2" fill="#050505" />
            {/* Inner notch — tiny dot at apex emphasising the "edge point" */}
            <circle cx="16" cy="9" r="1.5" fill="#050505" />
        </svg>
    );
}

/**
 * THE SEAL — angular octagonal seal with a minimal Σ etched at center.
 * Coin / institutional feel.
 */
export function StoicSeal({ size = 28, className = "", glow = false }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 32 32"
            xmlns="http://www.w3.org/2000/svg"
            className={className}
            aria-label="STOIC"
            style={glow ? { filter: "drop-shadow(0 0 8px rgba(0,255,65,0.5))" } : undefined}
        >
            {/* Octagonal seal outline */}
            <path
                d="M 10 2 L 22 2 L 30 10 L 30 22 L 22 30 L 10 30 L 2 22 L 2 10 Z"
                fill="#00FF41"
                stroke="#050505"
                strokeWidth="0.5"
            />
            {/* Etched Σ */}
            <path
                d="M 10 9 L 22 9 L 14 16 L 22 23 L 10 23"
                stroke="#050505"
                strokeWidth="2.4"
                strokeLinejoin="miter"
                strokeLinecap="square"
                fill="none"
            />
        </svg>
    );
}

// Active mark exported as <StoicMark /> — change this single line to swap brand mark.
export const StoicMark = StoicPillar;

export function StoicWordmark({ size = "text-sm", tagline, className = "" }) {
    return (
        <div className={`flex items-center gap-2.5 ${className}`}>
            <StoicMark size={28} />
            <div>
                <div className={`font-display font-bold tracking-[0.18em] ${size}`}>STOIC</div>
                {tagline && (
                    <div className="font-mono text-[10px] text-[#52525B] tracking-widest">
                        {tagline}
                    </div>
                )}
            </div>
        </div>
    );
}
