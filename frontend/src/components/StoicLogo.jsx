/**
 * STOIC brand marks — distinct logo options for the platform identity.
 *
 * Active mark: <StoicMark />  ← the one used everywhere
 * Alternates:  <StoicLattice />, <StoicObelisk />, <StoicCut />,
 *              <StoicPillar />, <StoicWedge />, <StoicSeal />
 *
 * Design rationale per option:
 *   • Lattice — 2×2 cell grid: 3 outlined + 1 filled. Directly visualises
 *     your 3-engine consensus → 1 final verdict. Most product-aligned.
 *   • Obelisk — tapered monument with two horizontal etched lines.
 *     Vertical authority + ancient minimalism.
 *   • Cut    — solid square with a sharp triangular corner sliced off.
 *     "Discipline = removing the unnecessary." Most minimalist.
 *   • Pillar — fluted Doric column doubling as a 4-bar price chart.
 *   • Wedge  — ascending mark with horizontal discipline line.
 *   • Seal   — angular octagonal seal with etched Σ.
 *
 * All marks share dimensions + props so they can be swapped via the
 * single export at the bottom of this file.
 */

/**
 * THE LATTICE — 2×2 consensus grid. Three outlined cells (Quant, Semantic,
 * Meta-Labeler engines) plus one filled (the unified decision). Reads as
 * a technical, modern, product-aligned mark at every size.
 */
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
            {/* 3 engine outline cells */}
            <rect x="2"  y="2"  width="12" height="12" fill="none" stroke="#00FF41" strokeWidth="2" />
            <rect x="18" y="2"  width="12" height="12" fill="none" stroke="#00FF41" strokeWidth="2" />
            <rect x="2"  y="18" width="12" height="12" fill="none" stroke="#00FF41" strokeWidth="2" />
            {/* 1 consensus cell — filled, the verdict */}
            <rect x="18" y="18" width="12" height="12" fill="#00FF41" />
        </svg>
    );
}

/**
 * THE OBELISK — tapered monument with two horizontal etched bands.
 * Tall, narrow, confident.
 */
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
            {/* Tapered obelisk silhouette */}
            <path
                d="M 13 28 L 13 9 L 16 3 L 19 9 L 19 28 Z"
                fill="#00FF41"
            />
            {/* Two etched bands — top (cap) and lower (pivot line) */}
            <rect x="11" y="10.5" width="10" height="1.5" fill="#050505" />
            <rect x="11" y="21"   width="10" height="1.5" fill="#050505" />
            {/* Wider base — adds gravity */}
            <rect x="10" y="27.5" width="12" height="2" fill="#00FF41" />
        </svg>
    );
}

/**
 * THE CUT — solid square with a precise triangular corner sliced off.
 * "Discipline = removing the unnecessary." Maximum minimalism.
 */
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
            {/* Pentagon — square with top-right corner cleanly sliced */}
            <path
                d="M 3 3 L 21 3 L 29 11 L 29 29 L 3 29 Z"
                fill="#00FF41"
            />
        </svg>
    );
}

/**
 * THE PILLAR — fluted Doric column. Three flutes flanked by capital + base.
 * Doubles as a 4-bar price chart.
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

/**
 * THE WEDGE — ascending mark with discipline line through apex.
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
            <path d="M 4 26 L 16 6 L 28 26 Z" fill="#00FF41" />
            <rect x="2" y="13.5" width="28" height="2" fill="#050505" />
            <circle cx="16" cy="9" r="1.5" fill="#050505" />
        </svg>
    );
}

/**
 * THE SEAL — angular octagonal seal with etched Σ.
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

// Active mark — change this single line to swap brand mark.
export const StoicMark = StoicLattice;

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
