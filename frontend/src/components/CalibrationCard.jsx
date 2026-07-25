import { useEffect, useState } from "react";
import api from "@/lib/api";

export function CalibrationCard() {
    const [cal, setCal] = useState(null);
    useEffect(() => {
        api.get("/analytics/calibration?days=90").then(({ data }) => setCal(data)).catch(() => {});
    }, []);
    const s = cal?.summary;
    const engines = Object.entries(cal?.engines || {});
    const errCls = (e) => e == null ? "text-[#A1A1AA]"
        : e <= 5 ? "text-[#00FF41]" : e <= 12 ? "text-[#FFD700]" : "text-[#FF3B30]";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A] p-4" data-testid="calibration-card">
            <div className="font-mono text-[10px] text-[#52525B] tracking-widest mb-1">
                AI CONFIDENCE CALIBRATION · STATED VS REALIZED (90D)
            </div>
            {s ? (
                <>
                    <div className="grid grid-cols-3 gap-3 mt-2">
                        <div>
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">PREDICTION</div>
                            <div className="font-mono text-xl text-white" data-testid="calibration-predicted">{s.predicted}%</div>
                        </div>
                        <div>
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">ACTUAL RELIABILITY</div>
                            <div className="font-mono text-xl text-[#0099FF]" data-testid="calibration-actual">{s.actual}%</div>
                        </div>
                        <div>
                            <div className="font-mono text-[9px] text-[#52525B] tracking-widest">CALIBRATION ERROR</div>
                            <div className={`font-mono text-xl ${errCls(s.calibration_error)}`} data-testid="calibration-error">
                                {s.calibration_error}pts
                            </div>
                        </div>
                    </div>
                    <div className="font-mono text-[9px] text-[#3F3F46] mt-1.5">
                        Mean absolute error across confidence buckets, weighted over {s.n} closed bot trades. ≤5pts = well calibrated.
                    </div>
                    <div className="mt-3 space-y-1.5">
                        {engines.map(([scope, ent]) => (
                            <div key={scope} data-testid={`calibration-engine-${scope}`}>
                                <div className="font-mono text-[10px] text-[#A1A1AA]">
                                    {scope} <span className="text-[#52525B]">· {ent.n} trades · Brier {ent.brier}</span>
                                </div>
                                <div className="flex flex-wrap gap-2 mt-0.5">
                                    {ent.buckets.map((b) => (
                                        <span key={b.bucket} className="font-mono text-[9px] px-1.5 py-0.5 border border-[#141414] text-[#52525B]">
                                            {b.bucket}: said {b.stated}% → got {b.realized}%{" "}
                                            <span className={errCls(Math.abs(b.gap))}>({b.gap >= 0 ? "+" : ""}{b.gap})</span>
                                        </span>
                                    ))}
                                </div>
                            </div>
                        ))}
                    </div>
                </>
            ) : (
                <div className="font-mono text-xs text-[#52525B]" data-testid="calibration-empty">
                    Not enough closed bot trades with confidence records yet.
                </div>
            )}
        </div>
    );
}
