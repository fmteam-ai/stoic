import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import "@/styles/intro_trailer.css";

/* Scene-by-scene state machine — timing in ms from t=0, matches the
   70-second voiceover script in /app/memory/intro_trailer_script.md.
   When the user drops a real ElevenLabs MP3 at /trailer.mp3 (public/),
   the <audio> element's currentTime drives the visuals in lockstep. */
const SCENES = [
    {
        id: "hook", start: 0, end: 8000,
        kicker: "Trading is hard",
        render: () => (
            <h1 className="headline">
                <span className="strike">85%</span> of retail traders blow up<br/>
                their accounts <em>within 12 months.</em>
            </h1>
        ),
        sub: "Most trading bots make it worse.",
    },
    {
        id: "pain", start: 8000, end: 22000,
        kicker: "What you wanted vs what you got",
        render: () => (
            <h1 className="headline">
                You wanted an AI co-pilot.<br/>
                <span className="strike">You got a fast Excel macro.</span>
            </h1>
        ),
        sub: "Bots that fire trades the moment the market gets noisy. Bots that double-stack you into 5× leverage on gold — when gold is already moving against you.",
    },
    {
        id: "pivot", start: 22000, end: 34000,
        kicker: "Introducing",
        render: () => (
            <h1 className="headline">
                <em>STOIC</em> is different.<br/>
                A 7-agent AI hedge fund<br/>that thinks <em>before</em> it trades.
            </h1>
        ),
        sub: "Claude Sonnet 4.5. Calibrated probabilities. A 10-layer risk veto cascade that refuses dumb trades — even the ones the AI wants to take.",
    },
    {
        id: "pillars", start: 34000, end: 58000,
        kicker: "Five reasons it's different",
        render: () => (
            <>
                <h1 className="headline">Five reasons it's <em>different.</em></h1>
                <div className="pillar-grid">
                    {[
                        ["01", "Calibrated probabilities",
                            "Real win-rate forecasts — not LLM confidence theater. Platt-scaled, Brier-score validated."],
                        ["02", "Loss Lab",
                            "When you lose, the AI investigates the trade and auto-tightens the guardrails for next time."],
                        ["03", "Correlation-aware Kelly",
                            "Never double-stacks you into a moving market. Sizes per-position against your full open book."],
                        ["04", "ADWIN drift detection",
                            "Auto-retrains your model the moment the market regime shifts. No manual upkeep."],
                        ["05", "Multi-account isolation",
                            "RoboForex, VT Markets, Binance — each broker with its own circuit breaker. Per-account everything."],
                    ].map(([n, title, body]) => (
                        <div className="pillar" key={n} data-testid={`pillar-${n}`}>
                            <div className="pillar-num">{n}</div>
                            <div className="pillar-title">{title}</div>
                            <div className="pillar-body">{body}</div>
                        </div>
                    ))}
                </div>
            </>
        ),
    },
    {
        id: "close", start: 58000, end: 72000,
        kicker: "The promise",
        render: () => (
            <h1 className="headline">
                STOIC doesn't promise you the moon.<br/>
                It promises a bot that <em>refuses to lose stupidly.</em>
            </h1>
        ),
        sub: "Stop guessing. Start trading like a quant fund.",
    },
];

export default function WelcomeTrailer() {
    const nav = useNavigate();
    const [t, setT] = useState(0);
    const [playing, setPlaying] = useState(false);   // gated until user clicks "Play"
    const [started, setStarted] = useState(false);   // false → show intro poster
    const audioRef = useRef(null);
    const [audioReady, setAudioReady] = useState(false);
    const [audioFailed, setAudioFailed] = useState(false);
    const tickerRef = useRef(null);

    /* Master clock — driven by either the audio element (if MP3 present)
       OR a setInterval fallback so the trailer plays cleanly even when no
       voiceover file has been uploaded yet. */
    useEffect(() => {
        if (!playing) return;
        let id;
        if (audioReady && audioRef.current && !audioFailed) {
            id = setInterval(() => {
                setT(Math.floor((audioRef.current?.currentTime || 0) * 1000));
            }, 80);
        } else {
            const start = Date.now() - t;
            id = setInterval(() => {
                const elapsed = Date.now() - start;
                setT(elapsed);
                if (elapsed >= 72000) {
                    setPlaying(false);
                    clearInterval(id);
                }
            }, 80);
        }
        return () => clearInterval(id);
    }, [playing, audioReady, audioFailed]);  // eslint-disable-line react-hooks/exhaustive-deps

    /* User-gesture-gated start — required by browser autoplay policy.
       Clicking "Play" attaches audio playback to a real user interaction,
       which is the ONLY reliable way to start audio in Chrome/Safari/Firefox. */
    const startTrailer = async () => {
        setStarted(true);
        setT(0);
        if (audioRef.current) {
            try {
                audioRef.current.currentTime = 0;
                await audioRef.current.play();
            } catch (err) {
                // Audio blocked or missing — fall back to silent timer
                setAudioFailed(true);
            }
        }
        setPlaying(true);
    };

    const activeIdx = SCENES.findIndex(s => t >= s.start && t < s.end);
    const finished = started && (t >= 72000 || (!playing && t > 0));

    const replay = () => {
        if (audioRef.current) {
            audioRef.current.currentTime = 0;
            audioRef.current.play().catch(() => setAudioFailed(true));
        }
        setT(0); setPlaying(true);
    };

    return (
        <div className="trailer-stage" data-testid="trailer-stage">
            <div className="particle-field" />
            {/* Ticker rails for ambient motion */}
            <div ref={tickerRef} className="ticker-rail" style={{ left: "12%" }} />
            <div className="ticker-rail" style={{ left: "32%", animationDelay: "1.2s" }} />
            <div className="ticker-rail" style={{ left: "68%", animationDelay: "2.5s" }} />
            <div className="ticker-rail" style={{ left: "88%", animationDelay: "0.6s" }} />

            {/* Skip button — top-right (hidden while intro poster is shown) */}
            {started && (
                <div className="trailer-skip">
                    <button className="cta-secondary"
                            onClick={() => nav("/login")}
                            data-testid="trailer-skip">
                        Skip → Sign in
                    </button>
                </div>
            )}

            {/* Audio element — drops the ElevenLabs MP3 in when ready. The
                file lives at frontend/public/trailer.mp3. Browser autoplay
                policy means we MUST wait for a real click before .play(). */}
            <audio
                ref={audioRef}
                src="/trailer.mp3"
                preload="auto"
                onCanPlayThrough={() => setAudioReady(true)}
                onError={() => { setAudioReady(false); setAudioFailed(true); }}
                onEnded={() => setPlaying(false)}
            />

            {/* Intro poster — required by browser autoplay policy.
                Audio cannot start until the user clicks something. */}
            {!started && (
                <div className="scene active trailer-intro" data-testid="trailer-intro">
                    <div className="kicker">STOIC · 70-second trailer</div>
                    <h1 className="headline">
                        An AI hedge fund<br/>
                        that <em>refuses</em> to lose stupidly.
                    </h1>
                    <p className="subline">
                        Sound on. 70 seconds. Watch how a 7-agent AI
                        pipeline + a 10-layer risk veto cascade trades
                        gold and bitcoin — so you don&apos;t have to.
                    </p>
                    <div className="cta-block" style={{ marginTop: "2.5rem" }}>
                        <button
                            className="cta-button trailer-play-btn"
                            onClick={startTrailer}
                            data-testid="trailer-play">
                            ▶ Play trailer
                        </button>
                        <button
                            className="cta-secondary"
                            onClick={() => nav("/login")}
                            data-testid="trailer-skip-intro">
                            Skip → Sign in
                        </button>
                    </div>
                </div>
            )}

            {/* Scene cards — only the active one is visible (post-start) */}
            {started && !finished && SCENES.map((s, i) => (
                <div key={s.id}
                     className={`scene ${i === activeIdx ? "active" : ""}`}
                     data-testid={`scene-${s.id}`}>
                    <div className="kicker">{s.kicker}</div>
                    {s.render()}
                    {s.sub && <p className="subline">{s.sub}</p>}
                </div>
            ))}

            {/* CTA scene — shown at the end */}
            {finished && (
                <div className="scene active" data-testid="scene-cta">
                    <div className="kicker">Your move.</div>
                    <h1 className="headline">
                        Trade like a <em>quant fund.</em><br/>
                        Without becoming one.
                    </h1>
                    <p className="subline">
                        Start with a free Starter plan. Upgrade when you're ready.
                        Cancel anytime — no auto-renewals, ever.
                    </p>
                    <div className="cta-block" style={{ marginTop: "2.5rem" }}>
                        <button
                            className="cta-button"
                            onClick={() => nav("/register")}
                            data-testid="trailer-cta-register">
                            Start free →
                        </button>
                        <button
                            className="cta-secondary"
                            onClick={replay}
                            data-testid="trailer-replay">
                            ↻ Replay trailer
                        </button>
                    </div>
                </div>
            )}

            {/* Scene-progress dots */}
            {started && !finished && (
                <div className="scene-rail">
                    {SCENES.map((s, i) => (
                        <div key={s.id}
                            className={`scene-dot ${i === activeIdx ? "active"
                                : i < activeIdx ? "played" : ""}`} />
                    ))}
                </div>
            )}

            {started && (
                <div className="audio-status">
                    {audioFailed ? "▶ SILENT PREVIEW" :
                     audioReady ? "▶ NARRATION SYNCED" : "▶ LOADING AUDIO…"}
                </div>
            )}
        </div>
    );
}
