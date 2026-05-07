#!/usr/bin/env python3
"""
Voice exchange — local pipeline for any agent.
mic -> faster-whisper (STT) -> Claude (character) -> ElevenLabs v3 + tagger (TTS) -> speaker

Press ENTER to start recording, ENTER again to stop. Ctrl+C exits.

Usage:
  python3 voice_call.py --agent stella
  python3 voice_call.py --agent lydia
  python3 voice_call.py --agent velikov

Per-agent character source is auto-detected:
  * If groups/<agent>/SOUL.md exists, loads SOUL.md + STYLE.md + SKILL.md (Lydia split).
  * Otherwise loads groups/<agent>/CLAUDE.md (unified).
The character system prompt is cached via Anthropic ephemeral cache_control;
once it crosses 1024 tokens (which it does for every agent today) subsequent
turns within ~5 minutes hit the cache.

ElevenLabs voice ID:
  Reads <AGENT_UPPER>_VOICE_ID from env or .env, e.g. STELLA_VOICE_ID.
  Stella and Lydia also fall back to ELEVENLABS_VOICE_ID_3 (legacy).

The tagger (tools/tag_cli.py) runs per sentence with the agent's voice_profile.md.
"""

import argparse
import io
import os
import re
import sys
import threading
from pathlib import Path

import anthropic
import numpy as np
import requests
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
from tag_cli import load_agent_profile, tag_text  # type: ignore[import]
from onecli_proxy import apply_for_agent  # type: ignore[import]

GROUPS_DIR = REPO_ROOT / "groups"

AGENT_DIRS = {
    "lydia": "lydia-clone",
}

LEGACY_VOICE_FALLBACK = {
    "stella": "ELEVENLABS_VOICE_ID_3",
    "lydia": "ELEVENLABS_VOICE_ID_3",
}

WHISPER_MODEL = "base"
SAMPLE_RATE = 16000
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 240
ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_MODEL = "eleven_v3"

SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")

VOICE_MODE_SUFFIX = (
    "\n\n---\n\nYou are in a live voice conversation. Keep responses to 1-3 sentences "
    "unless depth is genuinely needed. No markdown. No asterisks. Speak as you naturally "
    "speak; do not narrate stage directions."
)


def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    for path in [
        r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env",
        str(REPO_ROOT / ".env"),
    ]:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1]
        except FileNotFoundError:
            continue
    return None


def resolve_voice_id(agent):
    primary = f"{agent.upper()}_VOICE_ID"
    val = _env(primary)
    if val:
        return val, primary
    legacy = LEGACY_VOICE_FALLBACK.get(agent)
    if legacy:
        val = _env(legacy)
        if val:
            return val, legacy
    return None, primary


def load_character_text(agent):
    folder = AGENT_DIRS.get(agent) or agent
    group = GROUPS_DIR / folder
    if not group.exists():
        sys.exit(f"No group folder at {group}")

    parts = []
    soul = group / "SOUL.md"
    if soul.exists():
        for name in ("SOUL.md", "STYLE.md", "SKILL.md"):
            p = group / name
            if p.exists():
                parts.append(p.read_text())
    else:
        claude = group / "CLAUDE.md"
        if not claude.exists():
            sys.exit(f"No SOUL.md or CLAUDE.md in {group}")
        parts.append(claude.read_text())

    return "\n\n---\n\n".join(parts)


def quick_character_from_profile(profile_text, agent):
    """Quick-boot character: take voice_profile.md sections up to tag preferences,
    swap the tagger header for an agent header. Smaller and faster than CLAUDE.md
    but loses the deep context (memory, threads, scheduling, tools)."""
    cut_re = re.compile(r"\n##\s+\S+'s tag preferences", re.IGNORECASE)
    m = cut_re.search(profile_text)
    body = profile_text[: m.start()] if m else profile_text
    body = re.sub(
        r"^You are a voice direction assistant[^\n]*\n",
        f"You are {agent.title()}.\n",
        body,
        count=1,
    )
    return body.strip()


def system_blocks(character_text):
    return [
        {
            "type": "text",
            "text": character_text,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": VOICE_MODE_SUFFIX.lstrip(),
        },
    ]


print("Loading Whisper...", end=" ", flush=True)
_whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("ready.")


def transcribe(audio):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        sf.write(f.name, audio, SAMPLE_RATE)
        segments, _ = _whisper.transcribe(f.name, language="en", vad_filter=True)
        return " ".join(s.text for s in segments).strip()


def synthesise(text, voice_id, api_key):
    url = ELEVENLABS_TTS.format(voice_id=voice_id)
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.80,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    r = requests.post(url, json=payload, headers=headers, stream=True, timeout=20)
    r.raise_for_status()
    return b"".join(r.iter_content(chunk_size=4096))


def play_audio(mp3_bytes):
    buf = io.BytesIO(mp3_bytes)
    data, sr = sf.read(buf)
    sd.play(data, sr)
    sd.wait()


def make_tag_for_agent(profile_text, agent):
    def tag(text):
        try:
            return tag_text(text, profile_text, "", agent=agent)
        except Exception as e:
            print(f"\n[tag warning: {e}]", file=sys.stderr)
            return text
    return tag


def stream_and_speak(client, history, system, voice_id, eleven_key, tag_for_agent):
    """
    Stream Claude's response. As each sentence boundary fires, kick its
    tag+TTS synth in a background thread. At the next boundary, drain (wait
    for prior synth, play it), then kick the new sentence. Synth(N) overlaps
    with playback(N-1) plus the streaming gap between them.
    """
    full_response = ""
    buffer = ""
    pending = None

    def synth_into(text, box):
        try:
            box[0] = synthesise(tag_for_agent(text), voice_id, eleven_key)
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

    print("\nAgent: ", end="", flush=True)

    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=history,
    ) as stream:
        for token in stream.text_stream:
            print(token, end="", flush=True)
            buffer += token
            full_response += token

            parts = SENTENCE_RE.split(buffer, maxsplit=1)
            while len(parts) > 1:
                sentence = parts[0].strip()
                buffer = parts[1]
                drain()
                pending = kick(sentence)
                parts = SENTENCE_RE.split(buffer, maxsplit=1)

    print()

    tail = buffer.strip()
    if tail:
        drain()
        pending = kick(tail)
    drain()

    return full_response


def record_until_enter():
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="Agent name: stella, lydia, velikov, melody, aurelio")
    ap.add_argument("--quick", action="store_true",
                    help="Quick boot: skip CLAUDE.md/SOUL.md, derive a minimal character from voice_profile.md. "
                         "Faster first turn, smaller context, but no prompt caching and lighter character grounding.")
    args = ap.parse_args()

    agent = args.agent.lower()

    voice_id, env_name = resolve_voice_id(agent)
    if not voice_id:
        sys.exit(f"Missing {env_name} (set in .env to your ElevenLabs voice ID for {agent})")

    # Route Anthropic + ElevenLabs through OneCLI proxy as this agent so the
    # proxy injects the right credentials. No raw .env keys needed.
    onecli_agent = AGENT_DIRS.get(agent, agent)
    apply_for_agent(onecli_agent)
    eleven_key = "onecli-placeholder"  # proxy substitutes the real key

    print("Loading character...", end=" ", flush=True)
    profile_text = load_agent_profile(agent)
    if args.quick:
        character_text = quick_character_from_profile(profile_text, agent)
    else:
        character_text = load_character_text(agent)
    system = system_blocks(character_text)
    tag_for_agent = make_tag_for_agent(profile_text, agent)
    print(f"ready ({len(character_text)} chars{', quick boot' if args.quick else ''}).")

    client = anthropic.Anthropic(auth_token="onecli-placeholder")
    history = []

    print("\n" + "-" * 41)
    print(f"  {agent.title()} — voice exchange")
    print(f"  Voice: {env_name}")
    print("  ENTER to speak, ENTER to stop. Ctrl+C to exit.")
    print("-" * 41 + "\n")

    while True:
        try:
            input("[ Press ENTER to speak ]")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        print("Recording... (ENTER to stop)")
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
        response = stream_and_speak(client, history, system, voice_id, eleven_key, tag_for_agent)
        history.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
