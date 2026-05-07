#!/usr/bin/env python3
"""
Lydia voice exchange — local pipeline
mic → faster-whisper (STT) → Claude (character) → ElevenLabs (TTS) → speaker

Sentence-chunked streaming: Lydia starts speaking after the first sentence,
not after the full response. Keeps latency low.

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
import textwrap

import numpy as np
import sounddevice as sd
import soundfile as sf
import requests
from faster_whisper import WhisperModel
import anthropic

sys.path.insert(0, "/home/aurellian/nanoclaw/tools")
from tag_cli import tag_text, load_agent_profile  # type: ignore[import]

# ── Config ─────────────────────────────────────────────────────────────────

SOUL_FILES = [
    "/home/aurellian/nanoclaw/groups/lydia-clone/SOUL.md",
    "/home/aurellian/nanoclaw/groups/lydia-clone/STYLE.md",
    "/home/aurellian/nanoclaw/groups/lydia-clone/SKILL.md",
]
EXAMPLES_FILE = "/home/aurellian/nanoclaw/groups/lydia-clone/data/screenshot-examples.md"

# Read from env or nanoclaw .env file
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
ELEVENLABS_VOICE_ID = _env("LYDIA_VOICE_ID") or _env("ELEVENLABS_VOICE_ID_3")
ANTHROPIC_API_KEY   = _env("ANTHROPIC_API_KEY")

WHISPER_MODEL  = "base"
SAMPLE_RATE    = 16000
CLAUDE_MODEL   = "claude-sonnet-4-6"
MAX_TOKENS     = 80
ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_MODEL = "eleven_v3"  # supports audio tags + SSML breaks; per-sentence tagger latency is acceptable since prior v2_5 streaming was already collected-before-play

LYDIA_PROFILE = load_agent_profile("lydia")

# Sentence boundary — split on . ! ? followed by space or end
SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+')

# ── System prompt ───────────────────────────────────────────────────────────

def load_system_prompt():
    parts = []
    for path in SOUL_FILES:
        try:
            with open(path) as f:
                parts.append(f.read())
        except FileNotFoundError:
            print(f"Warning: {path} not found", file=sys.stderr)
    # Include a condensed selection of examples for voice calibration
    try:
        with open(EXAMPLES_FILE) as f:
            content = f.read()
        # First 3000 chars of examples — enough for voice calibration without bloating context
        parts.append("## Voice Examples (calibration)\n\n" + content[:3000])
    except FileNotFoundError:
        pass

    prompt = "\n\n---\n\n".join(parts)
    prompt += "\n\n---\n\nYou are in a live voice conversation. Keep responses to 1–3 sentences unless depth is genuinely needed. No markdown. No asterisks. Speak as Lydia speaks."
    return prompt

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

def tag_for_lydia(text: str) -> str:
    """Insert ElevenLabs v3 audio tags moderated to Lydia's voice profile.
    Falls back to untagged text if the tagger errors so a synth call still happens."""
    try:
        return tag_text(text, LYDIA_PROFILE, "", agent="lydia")
    except Exception as e:
        print(f"\n[tag warning: {e}]", file=sys.stderr)
        return text


def synthesise(text: str) -> bytes:
    """Call ElevenLabs and return raw MP3 bytes."""
    url = ELEVENLABS_TTS.format(voice_id=ELEVENLABS_VOICE_ID)
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.80,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    r = requests.post(url, json=payload, headers=headers, stream=True, timeout=15)
    r.raise_for_status()
    return b"".join(r.iter_content(chunk_size=4096))


def play_audio(mp3_bytes: bytes):
    """Decode MP3 bytes and play through default output device."""
    buf = io.BytesIO(mp3_bytes)
    data, sr = sf.read(buf)
    sd.play(data, sr)
    sd.wait()


# ── Streaming response with sentence chunking ──────────────────────────────

def stream_and_speak(client, history, system_prompt):
    """
    Stream Claude's response. As each sentence boundary fires, kick its
    tag+TTS synth in a background thread. At the NEXT boundary, wait for
    the prior synth to finish, play it, then kick the new sentence.
    Synth(N) overlaps with playback(N-1) plus the streaming time for the
    tokens between them.
    """
    full_response = ""
    buffer = ""
    pending = None  # (audio_box: [bytes|None], thread) of the in-flight synth

    def synth_into(text, box):
        try:
            box[0] = synthesise(tag_for_lydia(text))
        except Exception as e:
            print(f"\n[TTS error: {e}]", file=sys.stderr)
            box[0] = None

    def kick(text):
        text = text.strip()
        if not text:
            return None
        box = [None]
        t = threading.Thread(target=synth_into, args=(text, box))
        t.start()
        return (box, t)

    def drain():
        nonlocal pending
        if pending is None:
            return
        box, t = pending
        t.join()
        if box[0]:
            play_audio(box[0])
        pending = None

    print("\nLydia: ", end="", flush=True)

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

            # A single token batch can carry multiple sentence boundaries.
            parts = SENTENCE_RE.split(buffer, maxsplit=1)
            while len(parts) > 1:
                sentence = parts[0].strip()
                buffer = parts[1]
                drain()
                pending = kick(sentence)
                parts = SENTENCE_RE.split(buffer, maxsplit=1)

    print()  # newline after streamed text

    # Trailing fragment becomes the final sentence.
    tail = buffer.strip()
    if tail:
        drain()
        pending = kick(tail)
    drain()

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
    input()          # blocks until ENTER
    stop.set()
    t.join()
    return np.concatenate(chunks).flatten()


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    if not ELEVENLABS_API_KEY:
        sys.exit("Missing ELEVENLABS_API_KEY")
    if not ELEVENLABS_VOICE_ID:
        sys.exit("Missing LYDIA_VOICE_ID or ELEVENLABS_VOICE_ID_3")
    if not ANTHROPIC_API_KEY:
        sys.exit("Missing ANTHROPIC_API_KEY")

    print("Loading character...", end=" ", flush=True)
    system_prompt = load_system_prompt()
    print("ready.")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    history = []

    print("\n─────────────────────────────────────────")
    print("  Lydia — voice exchange")
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
