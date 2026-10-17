/* Guide — sections added for the v1.60.5x series (deterministic engines, Gold Trend Pulse, guardrails, AI provider).
   Kept out of Guide.jsx so the main file stays readable; the H2/P/Callout primitives are passed in. */
import { Link } from "react-router-dom";
import { Crosshair, Gauge, ShieldAlert, KeyRound } from "lucide-react";
import { useLlmModel } from "@/hooks/useLlmModel";

const box = "border border-[#1F1F1F] bg-[#0A0A0A] p-4 space-y-2";
const label = "font-mono text-[10px] text-[#52525B] tracking-widest";
const li = "text-sm text-[#A1A1AA] space-y-1.5";

export function EnginesSection({ H2, P, Callout }) {
    return (
        <section id="engines" data-testid="guide-engines">
            <H2 id="engines" icon={Crosshair}>16. Scalp Fast Path & deterministic engines</H2>
            <P>
                Next to the Dual-AI signal path, STOIC runs a set of <strong>deterministic engines</strong> — rule-based
                strategies with fixed geometry that never ask an LLM anything. Each account chooses its engine in
                <strong> Bot Config → Strategy engine</strong>; the <Link to="/scalp" className="text-[#00FF41] hover:underline">Scalp</Link> page
                shows every runner card, its live features and its shadow performance.
            </P>
            <div className="space-y-3 mt-4">
                <div className={box}>
                    <div className={label}>THE ENGINES</div>
                    <ul className={li}>
                        <li>• <strong className="text-white">hf_scalp / hf_scalp_fast</strong> — tick-driven gold scalps: EMA slope + RSI reset + Donchian breakout, 1–3 pip spreads only, broker-side SL/TP, cost-aware break-even, time stop.</li>
                        <li>• <strong className="text-white">breakout_m15</strong> — M15 range breakout with ATR-scaled targets and retest filter.</li>
                        <li>• <strong className="text-white">range_fade</strong> — mean reversion at range edges when the regime is RANGE.</li>
                        <li>• <strong className="text-white">gold_trend_pulse</strong> — XAUUSD-only M5 trend-pulse engine (section 17). Ships <em>disabled</em>.</li>
                    </ul>
                </div>
                <div className={box}>
                    <div className={label}>LIVE-PRICE FEATURES (ENGINE_LIVE_FEATURES_ENABLED)</div>
                    <P>
                        Price-dependent inputs (breakout distance, momentum, slope) can be updated from the <strong>current broker
                        price</strong> instead of waiting for the next closed bar, so a fast gold move is seen while it happens: a breakout
                        must clear the level by at least 0.2 × ATR15, a stale/reclaimed breakdown can no longer fire a SELL, and strong
                        momentum can override a slow FLAT read. The flag is <strong>OFF by default</strong>; with it off the engines trade
                        exactly as before while a one-week <em>shadow comparison</em> records what the live-feature path would have done.
                    </P>
                </div>
                <Callout kind="info">
                    Every deterministic engine keeps its own TP clamp and break-even geometry: trend-ride widening, volatility
                    retargeting and the generic TP tiers are skipped for these scopes. What you see on the card is what the broker holds.
                </Callout>
            </div>
        </section>
    );
}

export function GoldTrendPulseSection({ H2, P, Callout }) {
    return (
        <section id="gtp" data-testid="guide-gtp">
            <H2 id="gtp" icon={Gauge}>17. Gold Trend Pulse (shadow → demo)</H2>
            <P>
                <strong>Gold Trend Pulse</strong> is a dedicated XAUUSD engine that trades M5 pullbacks in the direction of the
                completed H1/H4 bias (TREND mode) or fades range edges (RANGE mode), with optional layered legs, a
                <em> sweep-and-reclaim</em> setup for the &ldquo;big drop, then straight back up&rdquo; pattern and a
                <strong> News Mode</strong> around high-impact USD events (CPI, NFP, FOMC, PCE, GDP, Retail Sales, PPI, ISM…).
            </P>
            <div className="space-y-3 mt-4">
                <div className={box}>
                    <div className={label}>SAFETY POSTURE</div>
                    <ul className={li}>
                        <li>• <strong className="text-white">Disabled by default</strong> — <code>gold_trend_pulse_enabled=false</code>; nothing changes on any account until you switch it on.</li>
                        <li>• <strong className="text-white">Shadow first</strong> — in <code>shadow</code> mode every decision, basket and outcome is recorded but no order is sent.</li>
                        <li>• <strong className="text-white">demo_live only on attested DEMO accounts</strong> — the switch needs an explicit confirmation and a rolling live gate (≥ 100 labelled shadow baskets, positive expectancy); each basket counts once.</li>
                        <li>• <strong className="text-white">0.25 % risk per basket</strong>, SL ≤ 1.3 × TP on every leg, cost-aware break-even at 60 % of TP, 45-min (TREND) / 20-min (RANGE) time stops.</li>
                        <li>• <strong className="text-white">Fail closed</strong> — missing or stale spread, incomplete M5 bar, unknown broker time basis or H4 warm-up (&lt; 200 bars) → no entry, with a pulse line telling you why.</li>
                        <li>• <strong className="text-white">Replaces the preset engine for XAUUSD</strong> — on an account with GTP enabled, gold is evaluated by the trend-pulse rules only (other symbols keep the preset engine). In shadow mode every gold cycle returns HOLD: decisions are logged, no XAUUSD order is sent from that account until demo_live passes its gate. Bot Config shows this warning next to the toggle.</li>
                        <li>• <strong className="text-white">H4 history (v1.60.61)</strong> — the EMA50(H4) needs 200 completed H4 bars; the M15 stream alone gives ≈ 50. The server asks a terminal on <strong className="text-white">EA ≥ 1.66</strong> for 300 H4 bars (first poll, then daily); Bot Health shows <code>GTP: H4 bars n/200</code>. Terminals on 1.65 keep working but the bias stays NONE until they are updated.</li>
                    </ul>
                </div>
                <div className={box}>
                    <div className={label}>NEWS MODE</div>
                    <P>
                        Before an event the engine <em>protects</em> open baskets (break-even or close, your choice), pauses entries, and
                        after the release waits for the surprise direction to confirm on M5 before trading again. The plan for the next
                        event is shown on the Scalp card and under <strong>Market Intel</strong>. Event times come from the Finnhub
                        calendar (your own <code>FINNHUB_API_KEY</code>), matched by exact event name and HIGH impact only.
                    </P>
                </div>
                <Callout kind="warn">
                    Management never stops: even during a macro freeze the engine still runs news protection, basket invalidation and
                    range-breakout closes for its open positions — only <em>new entries</em> are blocked.
                </Callout>
            </div>
        </section>
    );
}

export function GuardrailsSection({ H2, P, Callout }) {
    return (
        <section id="guardrails" data-testid="guide-guardrails">
            <H2 id="guardrails" icon={ShieldAlert}>18. Guardrails that run on every mode</H2>
            <P>
                These rails sit between a signal and the broker on <strong>every</strong> account, Demo included. When one fires,
                the <strong>Bot Pulse</strong> on the Dashboard and <strong>Bot Health → Cooldowns</strong> name it.
            </P>
            <div className="space-y-3 mt-4">
                <div className={box}>
                    <div className={label}>AFTER A LOSS</div>
                    <ul className={li}>
                        <li>• <strong className="text-white">Loss cooldown</strong> — a real loss (&lt; −0.1 R, measured from the <em>original</em> stop) blocks the same symbol + direction for 30 min (configurable). A break-even scratch is <em>not</em> a loss.</li>
                        <li>• <strong className="text-white">Cross-account</strong> — the block applies to <em>all</em> your accounts, so a second account cannot re-open the trade the first one just lost. Pulse: &ldquo;cross-account loss cooldown&rdquo;.</li>
                        <li>• <strong className="text-white">Anti-tilt</strong> — 2 real losses in the same direction within 4 h freeze that direction; losses on stale-feature entries do not count only when the live-feature flag is ON.</li>
                        <li>• <strong className="text-white">Daily 3-loss stop</strong> (Gold Trend Pulse) — three real losses end the engine&apos;s day.</li>
                    </ul>
                </div>
                <div className={box}>
                    <div className={label}>TIME & NEWS</div>
                    <ul className={li}>
                        <li>• <strong className="text-white">Macro / calendar freeze</strong> — HIGH-impact windows for the symbol&apos;s currencies, USD medium ±5 min, avalanche widening. Management continues, entries stop.</li>
                        <li>• <strong className="text-white">Gold off-hours & Asia skip</strong> — 21:00–06:00 UTC and 00:00–07:00 UTC weekdays by default (Bot Config).</li>
                        <li>• <strong className="text-white">Friday flat</strong> — tighten, then close before the weekend; a Telegram note fires on the close.</li>
                        <li>• <strong className="text-white">EOD quiet window</strong> — 23:40–00:05 broker time: routine closes wait for the terminal to reopen; <em>panic and protection closes are never paused</em>.</li>
                    </ul>
                </div>
                <div className={box}>
                    <div className={label}>EXECUTION TRUTH</div>
                    <ul className={li}>
                        <li>• <strong className="text-white">Broker-side SL/TP mandatory</strong> — a position without a protective stop is closed by the protection guard.</li>
                        <li>• <strong className="text-white">Close watchdog</strong> — every close command is re-issued until the EA acknowledges it; stops only count as moved after the ack.</li>
                        <li>• <strong className="text-white">Direction cap, stale-quote veto, spread fail-closed, per-account policy risk cap</strong>.</li>
                    </ul>
                </div>
                <Callout kind="info">
                    Nothing here depends on an AI model. If every AI feature went dark, these rails would still run unchanged.
                </Callout>
            </div>
        </section>
    );
}

export function AiProviderSection({ H2, P, Callout }) {
    const llm = useLlmModel("analysis");
    return (
        <section id="ai-provider" data-testid="guide-ai-provider">
            <H2 id="ai-provider" icon={KeyRound}>19. The AI layer — provider, keys & what it does NOT control</H2>
            <P>
                Claude ({llm.label} for analysis) writes signal explanations, scores news sentiment and Fed tone, runs post-mortems,
                the Strategy Optimizer, Bot Doctor, the Co-Pilot and the weekly digest. It <strong>never</strong> places, sizes or
                closes a trade: the engines and guardrails are deterministic and keep running when the model is unavailable.
            </P>
            <div className="space-y-3 mt-4">
                <div className={box}>
                    <div className={label}>PROVIDER</div>
                    <ul className={li}>
                        <li>• <strong className="text-white">Direct Anthropic</strong> — paste an <code>ANTHROPIC_API_KEY</code> under <strong>Admin → Integrations → AI</strong> and press <strong>Test</strong>; it takes precedence and applies instantly. Billing goes to Anthropic.</li>
                        <li>• <strong className="text-white">Emergent Universal Key</strong> — fallback; works only for apps hosted on Emergent (other hosts get a 403 before the key is checked).</li>
                        <li>• <strong className="text-white">Bot Health → LLM PROVIDER · COST</strong> shows the active provider, today&apos;s calls per feature against the daily cap, the last (redacted) error and the live model names.</li>
                    </ul>
                </div>
                <div className={box}>
                    <div className={label}>WHEN THE MODEL IS UNAVAILABLE</div>
                    <ul className={li}>
                        <li>• Signals keep firing from the engines; their notes carry <em>&ldquo;news veto inactive — LLM unavailable&rdquo;</em> because the dual-AI news veto needs a sentiment score.</li>
                        <li>• Bot Doctor falls back to its rule-based diagnosis; narration, post-mortems, optimizer and Co-Pilot pause.</li>
                        <li>• Keys are never logged or returned by any endpoint; output is capped per tier and per day.</li>
                    </ul>
                </div>
                <Callout kind="info">
                    The Co-Pilot remembers the last six turns of your conversation per session, whichever provider is active.
                </Callout>
            </div>
        </section>
    );
}
