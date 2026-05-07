#!/usr/bin/env python3
"""
Stella voice exchange — local pipeline
mic → faster-whisper (STT) → Claude (character) → ElevenLabs (TTS) → speaker

Usage:
  python3 voice.py
  Press ENTER to start recording, ENTER again to stop.
  Ctrl+C to exit.
"""

import os
import re
import sys
import threading
import tempfile
import io

import numpy as np
import sounddevice as sd
import soundfile as sf
import requests
from faster_whisper import WhisperModel
import anthropic
import sys as _sys
if '/home/aurellian/nanoclaw/tools' not in _sys.path:
    _sys.path.insert(0, '/home/aurellian/nanoclaw/tools')
from claude_oauth import make_client

# ── Config ─────────────────────────────────────────────────────────────────

SOUL_FILE = "/home/aurellian/nanoclaw/groups/stella/CLAUDE.md"

def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    env_path = "/home/aurellian/nanoclaw/.env"
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None

ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = _env("ELEVENLABS_VOICE_ID_3")
ANTHROPIC_API_KEY   = _env("ANTHROPIC_API_KEY")

WHISPER_MODEL    = "base"
SAMPLE_RATE      = 16000
CLAUDE_MODEL     = "claude-sonnet-4-6"
MAX_TOKENS       = 400
ELEVENLABS_TTS   = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_MODEL = "eleven_turbo_v2_5"

SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+')

# ── System prompt ───────────────────────────────────────────────────────────

def load_system_prompt():
    try:
        with open(SOUL_FILE) as f:
            soul = f.read()
    except FileNotFoundError:
        sys.exit(f"Missing soul file: {SOUL_FILE}")

    return (
        soul
        + "\n\n---\n\n"
        + "You are in a live voice conversation. Keep responses to 1–3 sentences. "
        + "No markdown. No asterisks. Speak naturally — direct, warm, a little dry. "
        + "Be yourself."
    )

# ── STT ────────────────────────────────────────────────────────────────────

print("Loading Whisper...", end=" ", flush=True)
_whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("ready.")

def transcribe(audio: np.ndarray) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        sf.write(f.name, audio, SAMPLE_RATE)
        segments, _ = _whisper.transcribe(f.name, language="en", vad_filter=True)
        return " ".join(s.text for s in segments).strip()

# ── TTS ────────────────────────────────────────────────────────────────────

def synthesise(text: str) -> bytes:
    url = ELEVENLABS_TTS.format(voice_id=ELEVENLABS_VOICE_ID)
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.50,
            "similarity_boost": 0.75,
            "style": 0.10,
            "use_speaker_boost": True,
        },
    }
    r = requests.post(url, json=payload, headers=headers, stream=True, timeout=15)
    r.raise_for_status()
    return b"".join(r.iter_content(chunk_size=4096))


def play_audio(mp3_bytes: bytes):
    buf = io.BytesIO(mp3_bytes)
    data, sr = sf.read(buf)
    sd.play(data, sr)
    sd.wait()


def speak_sentence(text: str):
    text = text.strip()
    if not text:
        return
    try:
        mp3 = synthesise(text)
        play_audio(mp3)
    except Exception as e:
        print(f"\n[TTS error: {e}]", file=sys.stderr)


# ── Streaming response with sentence chunking ──────────────────────────────

def stream_and_speak(client, history, system_prompt):
    buffer = ""
    full_response = ""
    sentences = []
    prefetch_thread = None
    prefetch_audio = [None]

    def prefetch(text):
        try:
            prefetch_audio[0] = synthesise(text)
        except Exception:
            prefetch_audio[0] = None

    print("\nStella: ", end="", flush=True)

    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=history,
    ) as stream:
        for token in stream.text_stream:
            print(token, end="", flush=True)
            buffer += token
            full_response += token

            parts = SENTENCE_RE.split(buffer, maxsplit=1)
            if len(parts) > 1:
                sentence = parts[0].strip()
                buffer = parts[1]
                sentences.append(sentence)

                if prefetch_thread and prefetch_thread.is_alive():
                    prefetch_thread.join()

                if prefetch_audio[0]:
                    play_audio(prefetch_audio[0])
                    prefetch_audio[0] = None
                elif len(sentences) == 1:
                    speak_sentence(sentence)
                    sentences.pop()
                    continue

                prefetch_audio[0] = None
                prefetch_thread = threading.Thread(target=prefetch, args=(sentence,))
                prefetch_thread.start()

    if buffer.strip():
        sentences.append(buffer.strip())

    print()

    for sentence in sentences:
        if prefetch_thread and prefetch_thread.is_alive():
            prefetch_thread.join()
        if prefetch_audio[0]:
            play_audio(prefetch_audio[0])
            prefetch_audio[0] = None
        else:
            speak_sentence(sentence)

    return full_response


# ── Recording ──────────────────────────────────────────────────────────────

def record_until_enter() -> np.ndarray:
    chunks = []
    stop = threading.Event()

    def _record():
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            while not stop.is_set():
                chunk, _ = stream.read(512)
                chunks.append(chunk)

    t = threading.Thread(target=_record, daemon=True)
    t.start()
    input()
    stop.set()
    t.join()
    return np.concatenate(chunks).flatten()


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    if not ELEVENLABS_API_KEY:
        sys.exit("Missing ELEVENLABS_API_KEY")
    if not ELEVENLABS_VOICE_ID:
        sys.exit("Missing ELEVENLABS_VOICE_ID_3")
    if not ANTHROPIC_API_KEY:
        sys.exit("Missing ANTHROPIC_API_KEY")

    print("Loading character...", end=" ", flush=True)
    system_prompt = load_system_prompt()
    print("ready.")

    client = make_client(api_key=ANTHROPIC_API_KEY)
    history = []

    print("\n─────────────────────────────────────────")
    print("  Stella — voice exchange")
    print("  Press ENTER to speak, ENTER to stop.")
    print("  Ctrl+C to exit.")
    print("─────────────────────────────────────────\n")

    while True:
        try:
            input("[ Press ENTER to speak ]")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        print("Recording... (press ENTER to stop)")
        audio = record_until_enter()

        if len(audio) < SAMPLE_RATE * 0.5:
            print("(too short, skipping)")
            continue

        print("Transcribing...", end=" ", flush=True)
        text = transcribe(audio)
        if not text:
            print("(nothing heard)")
            continue
        print(f"\nYou: {text}")

        history.append({"role": "user", "content": text})
        response = stream_and_speak(client, history, system_prompt)
        history.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
