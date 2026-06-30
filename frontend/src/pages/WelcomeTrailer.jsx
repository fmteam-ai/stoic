import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import "@/styles/intro_trailer.css";

/* Scene-by-scene state machine — timings in ms from t=0, anchored
   to the ACTUAL trailer.mp3 (86.42s, transcribed via Whisper).
   Each scene starts/ends precisely on a spoken phrase boundary. */
const SCENES = [
    {
        id: "hook", start: 0, end: 6680,
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
        id: "pain", start: 6680, end: 22760,
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
        id: "pivot", start: 22760, end: 40640,
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
        id: "pillars", start: 40640, end: 76600,
        kicker: "Five reasons it's different",
        /* Per-pillar beat timings anchored to "One.", "Two." ... "Five."
           spoken in the audio. Pillar lights up the moment its number lands. */
        pillarBeats: [40640, 47920, 54400, 60840, 67200],
        render: (t) => {
            const beats = [40640, 47920, 54400, 60840, 67200];
            const activeBeat = beats.findIndex((s, i) =>
                t >= s && (i === beats.length - 1 || t < beats[i + 1]));
            return (
                <>
                    <h1 className="headline">Five reasons it&apos;s <em>different.</em></h1>
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
                        ].map(([n, title, body], i) => (
                            <div className={`pillar ${i === activeBeat ? "beat" : i < activeBeat ? "lit" : ""}`}
                                 key={n} data-testid={`pillar-${n}`}>
                                <div className="pillar-num">{n}</div>
                                <div className="pillar-title">{title}</div>
                                <div className="pillar-body">{body}</div>
                            </div>
                        ))}
                    </div>
                </>
            );
        },
    },
    {
        id: "close", start: 76600, end: 86420,
        kicker: "The promise",
        render: () => (
            <h1 className="headline">
                STOIC doesn&apos;t promise you the moon.<br/>
                It promises a bot that <em>refuses to lose stupidly.</em>
            </h1>
        ),
        sub: "Stop guessing. Start trading like a quant fund.",
    },
];

const TRAILER_END_MS = 86420;

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
    const finished = started && (t >= TRAILER_END_MS || (!playing && t > 0));

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
                <div className="trailer-skip" style={{ display: "flex", gap: "0.75rem" }}>
                    <button className="cta-secondary"
                            onClick={() => nav("/login")}
                            data-testid="trailer-skip">
                        Sign in
                    </button>
                    <button className="cta-secondary"
                            onClick={() => nav("/register")}
                            data-testid="trailer-create-account">
                        Create account
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
                    <div className="kicker">STOIC · 90-second trailer</div>
                    <h1 className="headline">
                        An AI hedge fund<br/>
                        that <em>refuses</em> to lose stupidly.
                    </h1>
                    <p className="subline">
                        Sound on. Watch how a 7-agent AI pipeline +
                        a 10-layer risk veto cascade trades gold and
                        bitcoin — so you don&apos;t have to.
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
                            Sign in
                        </button>
                        <button
                            className="cta-secondary"
                            onClick={() => nav("/register")}
                            data-testid="trailer-create-account-intro">
                            Create account
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
                    {s.render(t)}
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
