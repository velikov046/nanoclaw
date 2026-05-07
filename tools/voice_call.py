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
DEFAULT_MODEL = "claude-sonnet-4-6"
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "opus": "claude-opus-4-7",
}
MAX_TOKENS = 240
ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
ELEVENLABS_MODEL = "eleven_v3"

SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")

VOICE_MODE_SUFFIX = (
    "\n\n---\n\nYou are in a live voice conversation. Keep responses to 1-3 sentences "
    "unless depth is genuinely needed. No markdown. No asterisks. Speak as you naturally "
    "speak; do not narrate stage directions. "
    "You can discuss technical things, but never include literal API keys, OAuth tokens, "
    "bearer tokens, passwords, or `.env` values — describe their shape and location only "
    "(\"the xAI key in your env\", \"$XAI_API_KEY\"), never the literal string. "
    "If you find yourself about to read out a long alphanumeric secret, stop and refer to "
    "it by name."
)

# Defense-in-depth: regex-mask anything secret-shaped before it hits stdout or TTS.
# Catches drift in agent CLAUDE.md or model behaviour without relying on prompt alone.
_SECRET_PATTERNS = [
    re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk_[a-f0-9]{32,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(Bearer\s+)([A-Za-z0-9_\-\.]{30,})"),
    re.compile(r"(xi-api-key:\s*)([A-Za-z0-9_\-\.]{20,})"),
    re.compile(r"(Authorization:\s*Bearer\s+)([A-Za-z0-9_\-\.]{20,})"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # github tokens
    re.compile(r"\beyJ[A-Za-z0-9_\-\.]{30,}\b"),    # jwt-shaped
]


def redact_secrets(text):
    """Mask secret-shaped tokens. Idempotent. Used on every outbound text fragment
    before it reaches stdout or ElevenLabs."""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: m.group(1) + "<REDACTED>", text)
        else:
            text = pat.sub("<REDACTED>", text)
    return text


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


MESSAGES_DB = "/home/aurellian/nanoclaw/store/messages.db"
DIARY_RECENT_DAYS = 5
MESSAGE_LIMIT = 40
MESSAGE_CONTENT_CAP = 400  # per message, to prevent any single huge message from dominating


def _load_recent_messages(folder, limit=MESSAGE_LIMIT):
    """Pull the last N non-empty messages between this agent's chat and Leo from
    messages.db. Returns a chronologically-ordered transcript or None on failure.
    Read-only, falls back silently if anything is off (DB missing, schema drift)."""
    import sqlite3
    if not Path(MESSAGES_DB).is_file():
        return None
    con = None
    try:
        con = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        cur = con.cursor()
        row = cur.execute(
            "SELECT jid FROM registered_groups WHERE folder=? LIMIT 1", (folder,)
        ).fetchone()
        if not row:
            return None
        chat_jid = row[0]
        rows = cur.execute(
            """
            SELECT timestamp, sender_name, content FROM messages
            WHERE chat_jid=? AND content IS NOT NULL AND content != ''
            ORDER BY timestamp DESC LIMIT ?
            """,
            (chat_jid, limit),
        ).fetchall()
        if not rows:
            return None
        rows.reverse()  # chronological
        lines = []
        for ts, sender, content in rows:
            content = (content or "").strip()
            if len(content) > MESSAGE_CONTENT_CAP:
                content = content[:MESSAGE_CONTENT_CAP] + "…"
            lines.append(f"[{ts}] {sender or '?'}: {content}")
        return "\n".join(lines)
    except Exception as e:
        print(f"\n[continuity warning: messages.db {e}]", file=sys.stderr)
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def load_continuity(agent):
    """Load the agent's continuity sources for the voice loop system prompt:
    MEMORY.md, recent diary entries, reflections, wiki/index, and recent
    Telegram conversation. These are what her container path reads at session
    start; the voice loop should honor the same contract.

    Returns a single concatenated string (caller wraps in a system block) or
    "" if nothing is available."""
    folder = AGENT_DIRS.get(agent) or agent
    group = GROUPS_DIR / folder
    parts = []

    mem_index = group / "memory" / "MEMORY.md"
    if mem_index.exists():
        parts.append("## Memory index\n\n" + mem_index.read_text())

    diary_dir = group / "diary"
    if diary_dir.exists():
        entries = sorted(diary_dir.glob("*.md"), reverse=True)[:DIARY_RECENT_DAYS]
        if entries:
            block = "\n\n".join(f"### {p.stem}\n{p.read_text()}" for p in entries)
            parts.append(f"## Recent diary entries (last {len(entries)})\n\n" + block)

    refl = group / "reflections.md"
    if refl.exists():
        parts.append("## Reflections\n\n" + refl.read_text())

    wiki_idx = group / "wiki" / "index.md"
    if wiki_idx.exists():
        parts.append("## Wiki index\n\n" + wiki_idx.read_text())

    msgs = _load_recent_messages(folder)
    if msgs:
        parts.append(f"## Recent Telegram conversation\n\n{msgs}")

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


CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def system_blocks(character_text, continuity_text=""):
    blocks = [
        {"type": "text", "text": CLAUDE_CODE_PREFIX},
        {
            "type": "text",
            "text": character_text,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    if continuity_text:
        # Separate cache breakpoint so character cache survives continuity churn
        # (new diary entries, new messages) and continuity itself caches across
        # turns within the 5-min window.
        blocks.append({
            "type": "text",
            "text": continuity_text,
            "cache_control": {"type": "ephemeral"},
        })
    blocks.append({"type": "text", "text": VOICE_MODE_SUFFIX.lstrip()})
    return blocks


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


def stream_and_speak(client, model, history, system, voice_id, eleven_key, tag_for_agent):
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

    # Buffered print at sentence boundary instead of per-token: lets us redact
    # secret-shaped tokens before they hit stdout (and TTS). One-sentence delay
    # in the visible stream; imperceptible for short Stella replies.
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=history,
    ) as stream:
        for token in stream.text_stream:
            buffer += token
            full_response += token

            parts = SENTENCE_RE.split(buffer, maxsplit=1)
            while len(parts) > 1:
                sentence = parts[0].strip()
                buffer = parts[1]
                safe_sentence = redact_secrets(sentence)
                print(safe_sentence + " ", end="", flush=True)
                drain()
                pending = kick(safe_sentence)
                parts = SENTENCE_RE.split(buffer, maxsplit=1)

    tail = buffer.strip()
    if tail:
        safe_tail = redact_secrets(tail)
        print(safe_tail, end="", flush=True)
        drain()
        pending = kick(safe_tail)
    print()
    drain()

    return redact_secrets(full_response)


def record_until_silence(min_speech_ms=400, silence_tail_ms=1500,
                         frame_ms=30, aggressiveness=2, max_seconds=60):
    """Record until end-of-utterance is detected by VAD.

    Returns the full audio captured (including the trailing silence — Whisper
    handles that fine and it preserves prosody). If `max_seconds` is hit before
    silence triggers, returns whatever was captured so far.
    """
    import webrtcvad
    vad = webrtcvad.Vad(aggressiveness)
    frame_samples = int(SAMPLE_RATE * frame_ms / 1000)
    silence_threshold_frames = silence_tail_ms // frame_ms
    min_speech_frames = max(1, min_speech_ms // frame_ms)
    max_frames = (max_seconds * 1000) // frame_ms

    chunks = []
    speech_frames = 0
    silence_frames = 0
    total_frames = 0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        while total_frames < max_frames:
            chunk, _ = stream.read(frame_samples)
            chunks.append(chunk)
            total_frames += 1
            pcm16 = (np.clip(chunk.flatten(), -1, 1) * 32767).astype("<i2").tobytes()
            if vad.is_speech(pcm16, SAMPLE_RATE):
                speech_frames += 1
                silence_frames = 0
            elif speech_frames >= min_speech_frames:
                silence_frames += 1
                if silence_frames >= silence_threshold_frames:
                    break

    return np.concatenate(chunks).flatten()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="Agent name: stella, lydia, velikov, melody, aurelio")
    ap.add_argument("--quick", action="store_true",
                    help="Quick boot: skip CLAUDE.md/SOUL.md, derive a minimal character from voice_profile.md. "
                         "Faster first turn, smaller context, but no prompt caching and lighter character grounding.")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Claude model id or alias (sonnet|haiku|opus). Default: " + DEFAULT_MODEL)
    args = ap.parse_args()
    model = MODEL_ALIASES.get(args.model, args.model)

    agent = args.agent.lower()

    voice_id, env_name = resolve_voice_id(agent)
    if not voice_id:
        sys.exit(f"Missing {env_name} (set in .env to your ElevenLabs voice ID for {agent})")

    # Route Anthropic + ElevenLabs through OneCLI proxy as this agent so the
    # proxy injects the right credentials. No raw .env keys needed.
    onecli_agent = AGENT_DIRS.get(agent, agent)
    try:
        apply_for_agent(onecli_agent)
    except Exception as e:
        # Agent not registered in OneCLI. Fall back to a known-working bundle
        # (stella has Anthropic + ElevenLabs) so voice still works for
        # unscaffolded / discontinued agents like Florence. They borrow keys;
        # the agent character is still loaded from groups/<agent>/.
        fallback = "stella"
        print(f"\n[onecli warning: '{onecli_agent}' not in OneCLI; "
              f"falling back to '{fallback}' credential bundle]", file=sys.stderr)
        apply_for_agent(fallback)
    eleven_key = "onecli-placeholder"  # proxy substitutes the real key

    print("Loading character...", end=" ", flush=True)
    profile_text = load_agent_profile(agent)
    if args.quick:
        character_text = quick_character_from_profile(profile_text, agent)
        continuity_text = ""  # quick mode skips continuity by design
    else:
        character_text = load_character_text(agent)
        continuity_text = load_continuity(agent)
    system = system_blocks(character_text, continuity_text)
    tag_for_agent = make_tag_for_agent(profile_text, agent)
    cont_note = f", {len(continuity_text)} chars continuity" if continuity_text else ""
    print(f"ready ({len(character_text)} chars character{cont_note}{', quick boot' if args.quick else ''}).")

    from claude_oauth import make_client
    client = make_client(auth_token="onecli-placeholder")
    history = []

    print("\n" + "-" * 41)
    print(f"  {agent.title()} — voice exchange")
    print(f"  Model: {model}")
    print(f"  Voice: {env_name}")
    print("  Speak whenever; pause to send. Ctrl+C to exit.")
    print("-" * 41 + "\n")

    while True:
        print("Listening...", end=" ", flush=True)
        try:
            audio = record_until_silence()
        except KeyboardInterrupt:
            print("\nGoodbye.")
            break
        print("(captured)")

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
        response = stream_and_speak(client, model, history, system, voice_id, eleven_key, tag_for_agent)
        history.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
