/**
 * STOIC brand — Σ mark (Sigma — summation of multi-engine consensus +
 * Stoic Greek heritage). Sharp-edged, monochromatic, scales perfectly.
 */
export function StoicMark({ size = 28, className = "", glow = false }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 32 32"
            xmlns="http://www.w3.org/2000/svg"
            className={className}
            aria-label="STOIC"
            style={glow ? { filter: "drop-shadow(0 0 8px rgba(0,255,65,0.6))" } : undefined}
        >
            {/* Sharp-edged frame */}
            <rect x="1" y="1" width="30" height="30" fill="#00FF41" />
            {/* Sigma mark — apex pulled left for clear glyph reading */}
            <path
                d="M 8 7 L 23 7 L 14 16 L 23 25 L 8 25"
                stroke="#050505"
                strokeWidth="2.8"
                strokeLinejoin="miter"
                strokeLinecap="square"
                fill="none"
            />
        </svg>
    );
}

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
