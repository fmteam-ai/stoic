"""Generate the STOIC intro trailer voiceover via OpenAI TTS (Emergent LLM key).

Run once to produce /app/frontend/public/trailer.mp3 — the WelcomeTrailer
page's <audio> element auto-loads from /trailer.mp3 and drives the scene
state machine off the audio's currentTime.

After regenerating, re-anchor the scene timings in WelcomeTrailer.jsx using
Whisper word timestamps (see scripts/transcribe_trailer.py pattern in git
history or run OpenAISpeechToText with verbose_json + word granularity).
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

VOICE = "onyx"        # deep, authoritative — matches the STOIC brand
MODEL = "tts-1-hd"
SPEED = 0.95          # slightly measured cadence

NARRATION = """85 percent of retail traders blow up their accounts within twelve months.
Most trading bots make it worse.

You wanted an AI co-pilot. You got a fast Excel macro.
Bots that fire trades the moment the market gets noisy.
Bots that don't know what they don't know.
Bots that keep trading while their own data feed is dying.

STOIC is different. A multi-agent AI hedge fund that thinks before it trades — and refuses to trade when it can't prove it's safe.

Six reasons nothing else comes close.

One. Calibrated AI probabilities. Real win-rate forecasts — not LLM confidence theater.

Two. Loss Lab. Every loss gets investigated by AI. Guardrails tighten automatically for next time.

Three. Fail-closed safety governance. When health drops, STOIC demotes itself — before it hurts you. No other bot does this.

Four. Explainable AI. Every trade shows exactly why it entered, why that size, and what could go wrong.

Five. One-command VPS. Auto-provision a server, or connect your own. Hardened pairing. One terminal, one account, twenty-four seven.

Six. Digital Twin and Research Lab. Strategies improve themselves in the shadows — and go live only when the evidence says so.

STOIC doesn't promise you the moon.
It promises a bot that refuses to lose stupidly.

Stop guessing. Start trading like a quant fund.
STOIC AI Trader.
"""


async def main():
    from emergentintegrations.llm.openai import OpenAITextToSpeech
    api_key = os.environ["EMERGENT_LLM_KEY"]
    tts = OpenAITextToSpeech(api_key=api_key)
    out_path = Path("/app/frontend/public/trailer.mp3")
    print(f"Generating ~{len(NARRATION)} chars to {out_path} …")
    audio_bytes = await tts.generate_speech(
        text=NARRATION, model=MODEL, voice=VOICE, speed=SPEED,
        response_format="mp3")
    out_path.write_bytes(audio_bytes)
    print(f"OK — wrote {out_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    asyncio.run(main())
