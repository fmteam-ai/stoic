import { Brain } from "lucide-react";

const STATE = {
    running: { tone: "good", label: "RUNNING", note: "Forecasts are being produced." },
    idle: { tone: "warn", label: "IDLE", note: "Model loaded, no forecast in the last 30 min — the trading loop may not have needed one." },
    not_loaded: { tone: "warn", label: "NOT LOADED", note: "Model loads on first use by the trading loop." },
    gated_off: { tone: "warn", label: "GATED OFF", note: "Heavy-ML gate is OFF (memory budget). Enable the forecast compose profile." },
    not_installed: { tone: "bad", label: "NOT INSTALLED", note: "torch / chronos are not in this image. Build with ML_FORECAST=1." },
    failed: { tone: "bad", label: "FAILED", note: "Model failed to load — see last error." },
    disabled: { tone: "neutral", label: "DISABLED", note: "FORECAST_AGENT_ENABLED=false — bot trades without the forecast veto." },
};
const TONES = {
    neutral: "border-[#1F1F1F] text-[#A1A1AA]",
    good: "border-[#00FF41]/40 text-[#00FF41]",
    warn: "border-[#FFB000]/40 text-[#FFB000]",
    bad: "border-[#FF3B30]/40 text-[#FF3B30]",
};

const ago = (s) => s == null ? "never" : s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${(s / 3600).toFixed(1)}h ago`;
const ms = (v) => v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)}s` : `${v} ms`;

function Chip({ label, value, tone = "neutral", testid }) {
    return (
        <div className={`border px-3 py-2 ${TONES[tone]}`} data-testid={testid}>
            <div className="font-mono text-[9px] tracking-widest text-[#52525B]">{label}</div>
            <div className="font-mono text-sm mt-0.5 truncate" title={String(value)}>{value}</div>
        </div>
    );
}

export function ForecastHealthCard({ data }) {
    if (!data) return null;
    const st = STATE[data.state] || STATE.not_loaded;
    const lead = data.lead || {};
    const cacheTone = lead.model_loaded ? "good" : "neutral";
    const latTone = lead.last_latency_ms == null ? "neutral" : lead.last_latency_ms < 1500 ? "good" : lead.last_latency_ms < 5000 ? "warn" : "bad";
    return (
        <div className="border border-[#1F1F1F] bg-[#0A0A0A]" data-testid="forecast-health-card">
            <div className="px-4 py-3 border-b border-[#1F1F1F] flex items-center gap-2 flex-wrap">
                <Brain className="w-3.5 h-3.5 text-[#0099FF]" />
                <span className="font-display font-bold text-sm">Forecast Health</span>
                <span className={`px-2 py-0.5 border font-mono text-[10px] tracking-widest ${TONES[st.tone]}`} data-testid="forecast-state">{st.label}</span>
                <span className="px-2 py-0.5 border border-[#1F1F1F] font-mono text-[9px] tracking-widest text-[#52525B]" title={data.notice || ""} data-testid="forecast-advisory-label">ADVISORY · FAIL-OPEN</span>
                <span className="ml-auto font-mono text-[9px] tracking-widest text-[#52525B]">CHRONOS · {lead.role ? lead.role.toUpperCase() : "—"}</span>
            </div>
            <div className="p-4 space-y-3">
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
                    <Chip label="MODEL" value={data.model} testid="forecast-model" />
                    <Chip label="MODEL CACHE" tone={cacheTone} testid="forecast-cache"
                        value={lead.model_loaded ? `loaded · ${ms(lead.load_ms)} load${lead.load_ms != null && lead.load_ms < 3000 ? " (warm)" : lead.load_ms != null ? " (cold)" : ""}` : lead.model_failed ? "failed" : "not loaded"} />
                    <Chip label="LAST FORECAST LATENCY" tone={latTone} value={ms(lead.last_latency_ms)} testid="forecast-latency" />
                    <Chip label="LAST FORECAST" tone={lead.last_forecast_age_s != null && lead.last_forecast_age_s <= 1800 ? "good" : "neutral"}
                        value={`${ago(lead.last_forecast_age_s)}${lead.last_symbol ? ` · ${lead.last_symbol}` : ""}${lead.last_source ? ` · ${lead.last_source}` : ""}`} testid="forecast-last" />
                </div>
                <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
                    <Chip label="FORECASTS" value={lead.forecasts_total ?? 0} testid="forecast-total" />
                    <Chip label="RESULT CACHE" value={`${lead.cache_entries ?? 0} symbol(s) · ${lead.cache_hits ?? 0} hits / ${lead.cache_misses ?? 0} misses · TTL ${Math.round((data.cache_ttl_s || 0) / 60)}m`} testid="forecast-result-cache" />
                    <Chip label="ML GATE" tone={lead.ml_runtime_enabled ? "good" : "warn"} value={`${lead.ml_runtime_enabled ? "ON" : "OFF"}${lead.memory_budget_gb != null ? ` · ${lead.memory_budget_gb} GB budget` : " · unlimited"}`} testid="forecast-ml-gate" />
                    <Chip label="ERRORS" tone={lead.inference_errors ? "warn" : "neutral"} value={lead.inference_errors ?? 0} testid="forecast-errors" />
                </div>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
                    <Chip label="LAST TRADING DECISION" testid="forecast-last-decision"
                        tone={lead.last_decision_consumed == null ? "neutral" : lead.last_decision_consumed ? "good" : "warn"}
                        value={lead.last_decision_at == null ? "no decision recorded yet"
                            : `${ago(lead.last_decision_age_s)} · ${lead.last_decision_consumed ? "consumed a forecast" : "made WITHOUT a forecast (fail-open)"}`} />
                    <Chip label="DECISIONS WITH / WITHOUT FORECAST" testid="forecast-decision-split"
                        value={`${lead.decisions_with_forecast ?? 0} / ${lead.decisions_without_forecast ?? 0}`} />
                </div>
                <div className="font-mono text-[10px] text-[#52525B]" data-testid="forecast-note">{st.note}{lead.last_error ? <span className="text-[#FF3B30]"> · {lead.last_error}</span> : null}</div>
                <div className="font-mono text-[10px] text-[#FFB000]/80" data-testid="forecast-advisory-note">{data.notice || "Advisory forecast — fail-open. Never a trading-readiness guarantee; safety lives in the Trading Authority strip."}</div>
                {(data.stale_processes || []).length > 0 && (
                    <div className="font-mono text-[9px] text-[#52525B]" data-testid="forecast-stale-processes">
                        stale (no status in {Math.round((data.process_ttl_s || 900) / 60)}m): {data.stale_processes.map(p => p.role).join(", ")}
                    </div>
                )}
                {(data.processes || []).length > 1 && (
                    <div className="border-t border-[#141414] pt-2 space-y-1" data-testid="forecast-processes">
                        {data.processes.map(p => (
                            <div key={p.role} className="flex items-center gap-3 font-mono text-[10px] text-[#A1A1AA]">
                                <span className={`w-1.5 h-1.5 rounded-full ${p.model_loaded ? "bg-[#00FF41]" : p.model_failed ? "bg-[#FF3B30]" : "bg-[#52525B]"}`} />
                                <span className="text-white w-36 truncate">{p.role}</span>
                                <span>{p.model_loaded ? "loaded" : p.model_failed ? "failed" : "idle"}</span>
                                <span>· {p.forecasts_total ?? 0} fc</span>
                                <span>· last {ago(p.last_forecast_age_s)}</span>
                                <span className="ml-auto">status {ago(p.status_age_s)}</span>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}
