const TIERS = [
    { max: 1000, label: "TIGHT", color: "#00FF41" },
    { max: 5000, label: "OK", color: "#A1A1AA" },
    { max: 15000, label: "CAUTION", color: "#FFB000" },
    { max: Infinity, label: "HALT", color: "#FF3B30" },
];

const fmtOffset = (ms) => {
    const sign = ms >= 0 ? "+" : "−";
    const abs = Math.abs(ms);
    const h = Math.floor(abs / 3600000);
    const m = Math.floor((abs % 3600000) / 60000);
    return `${sign}${h}h${String(m).padStart(2, "0")}`;
};

export const DriftGauge = ({ residualMs, offsetMs, samples, limitMs = 15000, testid = "drift-gauge" }) => {
    const measuring = !samples || samples < 10;
    const res = Number(residualMs || 0);
    const abs = Math.abs(res);
    const tier = TIERS.find(t => abs < t.max) || TIERS[TIERS.length - 1];
    const pct = Math.min(100, (abs / limitMs) * 100);
    const color = measuring ? "#52525B" : tier.color;
    const value = measuring ? "measuring" : abs < 1000 ? `${res >= 0 ? "+" : "−"}${abs}ms` : `${res >= 0 ? "+" : "−"}${(abs / 1000).toFixed(1)}s`;
    return (
        <div data-testid={testid}
             title={`Broker clock vs server after timezone snap. Raw offset ${fmtOffset(offsetMs || 0)} (${samples || 0} samples). Trading halts beyond ${limitMs / 1000}s.`}>
            Drift <span className="font-mono" style={{ color }} data-testid={`${testid}-value`}>{value}</span>
            {!measuring && <span className="ml-1 text-[9px] tracking-widest" style={{ color }} data-testid={`${testid}-tier`}>{tier.label}</span>}
            <div className="mt-1 h-1 w-full bg-[#1F1F1F] overflow-hidden">
                <div className="h-full transition-[width] duration-500" style={{ width: `${measuring ? 0 : pct}%`, backgroundColor: color }} />
            </div>
        </div>
    );
};
