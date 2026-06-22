/**
 * STOIC brand mark — official client-provided logo.
 *
 * The brand image lives at `/brand/stoic-logo.png` (in `public/`). All UI
 * mounts of <StoicMark /> render the official artwork. The geometric
 * candidates below remain available in this file (StoicLattice, StoicObelisk,
 * StoicCut, StoicPillar, StoicWedge, StoicSeal) as fallback options, but the
 * active mark is the client artwork.
 */

const LOGO_SRC = "/brand/stoic-logo.png";

/**
 * THE OFFICIAL MARK — the artwork the client provided. Renders as a square
 * <img> so it works in every place a geometric SVG would (sidebar, login,
 * favicon, etc.). The `glow` prop adds a green halo for emphasis.
 */
export function StoicOfficial({ size = 28, className = "", glow = false }) {
    return (
        <img
            src={LOGO_SRC}
            width={size}
            height={size}
            alt="STOIC AI Trading Bot"
            className={`object-contain ${className}`}
            style={glow ? { filter: "drop-shadow(0 0 12px rgba(255, 215, 0, 0.45))" } : undefined}
        />
    );
}

/** Geometric candidates — retained as fallbacks. */
export function StoicLattice({ size = 28, className = "", glow = false }) {
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
            <rect x="2"  y="2"  width="12" height="12" fill="none" stroke="#00FF41" strokeWidth="2" />
            <rect x="18" y="2"  width="12" height="12" fill="none" stroke="#00FF41" strokeWidth="2" />
            <rect x="2"  y="18" width="12" height="12" fill="none" stroke="#00FF41" strokeWidth="2" />
            <rect x="18" y="18" width="12" height="12" fill="#00FF41" />
        </svg>
    );
}

export function StoicObelisk({ size = 28, className = "", glow = false }) {
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
            <path d="M 13 28 L 13 9 L 16 3 L 19 9 L 19 28 Z" fill="#00FF41" />
            <rect x="11" y="10.5" width="10" height="1.5" fill="#050505" />
            <rect x="11" y="21"   width="10" height="1.5" fill="#050505" />
            <rect x="10" y="27.5" width="12" height="2" fill="#00FF41" />
        </svg>
    );
}

export function StoicCut({ size = 28, className = "", glow = false }) {
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
            <path d="M 3 3 L 21 3 L 29 11 L 29 29 L 3 29 Z" fill="#00FF41" />
        </svg>
    );
}

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
            <rect x="3" y="4" width="26" height="3.5" fill="#00FF41" />
            <rect x="5" y="7.5" width="22" height="1.5" fill="#00FF41" />
            <rect x="7"  y="11" width="3" height="10" fill="#00FF41" />
            <rect x="12" y="9"  width="3" height="14" fill="#00FF41" />
            <rect x="17" y="11" width="3" height="12" fill="#00FF41" />
            <rect x="22" y="13" width="3" height="8"  fill="#00FF41" />
            <rect x="5" y="23" width="22" height="1.5" fill="#00FF41" />
            <rect x="3" y="24.5" width="26" height="3.5" fill="#00FF41" />
        </svg>
    );
}

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
            <path d="M 4 26 L 16 6 L 28 26 Z" fill="#00FF41" />
            <rect x="2" y="13.5" width="28" height="2" fill="#050505" />
            <circle cx="16" cy="9" r="1.5" fill="#050505" />
        </svg>
    );
}

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
            <path
                d="M 10 2 L 22 2 L 30 10 L 30 22 L 22 30 L 10 30 L 2 22 L 2 10 Z"
                fill="#00FF41"
                stroke="#050505"
                strokeWidth="0.5"
            />
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

// Active mark — set to the client-provided artwork.
export const StoicMark = StoicOfficial;

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
