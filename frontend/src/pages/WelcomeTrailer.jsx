import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LandingTestimonials } from "@/components/LandingTestimonials";
import "@/styles/intro_trailer.css";

/* Scene-by-scene state machine — timings in ms from t=0, anchored
   to the ACTUAL trailer.mp3 (98.28s, OpenAI TTS "ash", word-level
   Whisper timestamps). Each scene starts/ends on a spoken phrase. */
const SCENES = [
    {
        id: "hook", start: 0, end: 8560,
        kicker: "Trading is hard",
        render: () => (
            <h1 className="headline">
                <span className="strike">85%</span> of retail traders blow up<br/>
                their accounts. <em>85 percent!</em>
            </h1>
        ),
        sub: "And the bots they trusted? They made it happen faster.",
    },
    {
        id: "pain", start: 8560, end: 23400,
        kicker: "What you dreamed vs what you got",
        render: () => (
            <h1 className="headline">
                You dreamed of an AI co-pilot.<br/>
                <span className="strike">You got a fast Excel macro.</span>
            </h1>
        ),
        sub: "A bot that panics the moment the market gets loud. A bot that keeps firing while its own data feed is dying. That's not intelligence — that's a coin flip with better marketing.",
    },
    {
        id: "pivot", start: 23400, end: 39420,
        kicker: "Introducing",
        render: () => (
            <h1 className="headline">
                <em>STOIC</em> is different.<br/>
                Multi-agent AI trading software<br/><em>in your pocket.</em>
            </h1>
        ),
        sub: "A machine that thinks before it trades — with the discipline to refuse any trade it can't prove is safe. That discipline? Nobody else has it.",
    },
    {
        id: "pillars", start: 39420, end: 86380,
        kicker: "Six reasons this changes everything",
        /* Per-pillar beat timings anchored to "One!", "Two!" ... "Six!"
           spoken in the audio. Pillar lights up the moment its number lands. */
        pillarBeats: [39420, 45840, 53040, 64000, 70860, 78080],
        render: (t) => {
            const beats = [39420, 45840, 53040, 64000, 70860, 78080];
            const activeBeat = beats.findIndex((s, i) =>
                t >= s && (i === beats.length - 1 || t < beats[i + 1]));
            return (
                <>
                    <h1 className="headline">Six reasons this <em>changes everything.</em></h1>
                    <div className="pillar-grid">
                        {[
                            ["01", "Calibrated probabilities",
                                "Real, honest win-rate forecasts. No confidence theater. No guessing."],
                            ["02", "Loss Lab",
                                "Every single loss gets interrogated by AI — the guardrails tighten themselves. Your bot literally learns from pain."],
                            ["03", "Fail-closed safety",
                                "The one nobody else dares to build: when health drops, STOIC demotes itself — before it ever hurts you. A trading bot with self-control."],
                            ["04", "Explainable AI",
                                "Every trade tells you why. Why it entered. Why that size. What could go wrong. No black boxes. Ever."],
                            ["05", "One-command VPS",
                                "Your bot lives on a hardened VPS, trading 24/7. One terminal. One account. Zero excuses."],
                            ["06", "Digital Twin + Research Lab",
                                "Strategies evolve in the shadows — and only the proven ones ever touch real money."],
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
        id: "close", start: 86380, end: 98280,
        kicker: "The promise",
        render: () => (
            <h1 className="headline">
                STOIC won&apos;t promise you the moon.<br/>
                It promises a bot that <em>refuses — absolutely refuses —</em><br/>
                to lose stupidly.
            </h1>
        ),
        sub: "Rules-based automated trading with fund-grade risk controls. Trading involves substantial risk of loss.",
    },
];

const TRAILER_END_MS = 98280;

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
                if (elapsed >= TRAILER_END_MS) {
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
        <div className="trailer-stage" data-testid="welcome-page" data-page="welcome">
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
                Audio cannot start until the user clicks something.
                Scrollable: testimonials wall lives below the hero. */}
            {!started && (
                <div className="trailer-scroll" data-testid="trailer-scroll">
                    <div className="scene active trailer-intro" data-testid="trailer-intro">
                        <div className="kicker">STOIC · 90-second trailer</div>
                        <h1 className="headline">
                            AI trading software<br/>
                            designed to <em>refuse</em> trades that fail its risk checks.
                        </h1>
                        <p className="subline">
                            Sound on. Watch how a multi-agent AI pipeline,
                            fail-closed safety governance and one-command VPS
                            infrastructure trade gold and bitcoin — so you
                            don&apos;t have to.
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
                        <div className="intro-scroll-hint" data-testid="intro-scroll-hint">
                            <span>What traders say</span>
                            <span>▼</span>
                        </div>
                    </div>
                    <LandingTestimonials />
                    {/* audit F-15 — legal navigation on the public welcome page */}
                    <footer className="trailer-legal" data-testid="welcome-legal-footer"
                        style={{ padding: "24px 16px 40px", textAlign: "center", fontFamily: "monospace", fontSize: "10px", letterSpacing: "2px", color: "#52525B" }}>
                        <div style={{ display: "flex", gap: "18px", justifyContent: "center", flexWrap: "wrap", marginBottom: "10px" }}>
                            <a href="/terms" style={{ color: "#8A8A93" }}>TERMS OF USE</a>
                            <a href="/privacy" style={{ color: "#8A8A93" }}>PRIVACY POLICY</a>
                            <a href="/risk-disclosure" style={{ color: "#8A8A93" }}>RISK DISCLOSURE</a>
                            <a href="/support" style={{ color: "#8A8A93" }}>CONTACT</a>
                        </div>
                        <div style={{ maxWidth: "640px", margin: "0 auto", lineHeight: 1.6 }}>
                            STOIC is automated trading software — not a broker, hedge fund or
                            investment advisor. Trading involves substantial risk of loss and is
                            not suitable for everyone. Past performance does not guarantee future
                            results. © 2026 STOIC AI Technologies.
                        </div>
                    </footer>
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
                        Automated trading with <em>fund-grade risk controls.</em><br/>
                        Without becoming one.
                    </h1>
                    <p className="subline">
                        Create a free account and watch it trade in Shadow Mode.
                        Plans from $39/mo. Cancel anytime — no auto-renewals, ever.
                    </p>
                    <div className="cta-block" style={{ marginTop: "2.5rem" }}>
                        <button
                            className="cta-button"
                            onClick={() => nav("/register")}
                            data-testid="trailer-cta-register">
                            Create account →
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
