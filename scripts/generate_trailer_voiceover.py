"""Generate the STOIC intro trailer voiceover via ElevenLabs.

Run once to produce /app/frontend/public/trailer.mp3 — the WelcomeTrailer
page's <audio> element auto-loads from /trailer.mp3 and drives the scene
state machine off the audio's currentTime.
"""
import os
from pathlib import Path
from elevenlabs.client import ElevenLabs

# Brian — calm, measured, slightly cynical. Matches the "we refuse to do
# dumb things" brand. Voice ID is from the public ElevenLabs library.
VOICE_ID = "nPczCjzI2devNBz1zQrb"  # "Brian"
MODEL_ID = "eleven_multilingual_v2"

NARRATION = """85% of retail traders blow up their accounts within twelve months.
Most trading bots make it worse.

You wanted an AI co-pilot. You got a fast Excel macro.
Bots that fire trades the moment the market gets noisy.
Bots that don't know what they don't know.
Bots that stack you into five-times leverage on gold, when gold is already moving against you.

STOIC is different. Not one model. Not one signal. A seven-agent AI hedge fund that thinks before it trades.
Claude Sonnet 4.5. Calibrated probabilities. And a ten-layer risk veto cascade that refuses dumb trades — even the ones the AI wants to take.

One. Calibrated AI probabilities. Real win-rate forecasts. Not LLM confidence theater.
Two. Loss Lab. When you lose, the AI investigates the trade and auto-tightens the guardrails.
Three. Correlation-aware Kelly sizing. Your bot will never double-stack you into a moving market.
Four. ADWIN drift detection. Auto-retrains your model the moment the regime shifts.
Five. Multi-account isolation. RoboForex, VT Markets, Binance — each with its own circuit breaker.

STOIC doesn't promise you the moon.
It promises a bot that refuses to lose stupidly.

Stop guessing. Start trading like a quant fund.
STOIC AI Trader.
"""

def main():
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise SystemExit("ELEVENLABS_API_KEY env var not set.")
    client = ElevenLabs(api_key=api_key)
    out_path = Path("/app/frontend/public/trailer.mp3")
    print(f"Generating ~{len(NARRATION)} chars to {out_path} …")

    # ElevenLabs v2 SDK streams chunks — concatenate to MP3.
    audio_gen = client.text_to_speech.convert(
        voice_id=VOICE_ID,
        model_id=MODEL_ID,
        text=NARRATION,
        # Default voice settings — slightly slower than default for our
        # measured narrator brand. stability=0.55 keeps cadence consistent.
        voice_settings={
            "stability": 0.55,
            "similarity_boost": 0.75,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    )
    out_path.write_bytes(b"".join(audio_gen))
    size_kb = out_path.stat().st_size / 1024
    print(f"OK — wrote {size_kb:.1f} KB")

if __name__ == "__main__":
    main()
