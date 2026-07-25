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

VOICE = "ash"         # energetic, expressive — passionate trailer delivery
MODEL = "tts-1-hd"
SPEED = 1.0

NARRATION = """Eighty-five percent of retail traders blow up their accounts. Eighty-five percent! And the bots they trusted? They made it happen faster.

You dreamed of an AI co-pilot. And what did you get? A fast Excel macro! A bot that panics the moment the market gets loud. A bot that keeps firing while its own data feed is dying. That's not intelligence. That's a coin flip with better marketing.

STOIC is different. This is a multi-agent AI hedge fund in your pocket — a machine that thinks before it trades, and has the discipline to refuse any trade it can't prove is safe. That discipline? Nobody else has it.

Six reasons this changes everything.

One! Calibrated probabilities. Real, honest win-rate forecasts. No confidence theater. No guessing.

Two! Loss Lab. Every single loss gets interrogated by AI, and the guardrails tighten themselves. Your bot literally learns from pain.

Three! The one nobody else dares to build: fail-closed safety. When health drops, STOIC demotes itself. It steps back before it ever hurts you. A trading bot with self-control!

Four! Explainable AI. Every trade tells you why. Why it entered. Why that size. What could go wrong. No black boxes. Ever.

Five! One command — and your bot lives on a hardened VPS, trading twenty-four seven. One terminal. One account. Zero excuses.

Six! Digital Twin and Research Lab. Your strategies evolve in the shadows — and only the proven ones ever touch real money.

STOIC won't promise you the moon. It promises something better: a bot that refuses — absolutely refuses — to lose stupidly.

Stop gambling. Start trading like a quant fund. STOIC AI Trader. Your edge starts now.
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
