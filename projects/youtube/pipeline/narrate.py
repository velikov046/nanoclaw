"""
narrate.py — Generate narration audio from script segments using ElevenLabs.

Usage:
  python3 narrate.py --job <job_dir> [--voice malakai]

Reads: <job_dir>/script.json
Writes:
  <job_dir>/audio/segment_01.mp3 ... final.mp3
  Per-segment word-level alignment baked into script.json:
    seg.tagged_text  — tagger output (preserved for caption-time tag mapping)
    seg.words        — [{text, start, end}, ...] from normalized_alignment
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys

import requests

ELEVEN_API_BASE = "https://api.elevenlabs.io"

# A "word" that is entirely one-or-more bracket tags (e.g. "[whispers]" or
# "[whispers][nervous]"). ElevenLabs' normalized_alignment leaks these through
# in 2026-05; drop them so captions stay clean.
_BRACKET_TAG_WORD = re.compile(r"^(\[[^\[\]]+\])+$")

TOOLS_DIR = "/workspace/tools"
TAG_CLI = os.path.join(TOOLS_DIR, "tag_cli.py")

VOICE_ALIASES = {
    "malakai": "Malakai",
    "james_oak": "James Oak",
    "james oak": "James Oak",
}

DEFAULT_VOICE = "Malakai"


def resolve_voice_id(api_key: str, voice_name: str) -> str:
    """Map a voice alias / short name to an ElevenLabs voice_id.

    Tries exact match first, then prefix match (the library decorates names
    like "Malakai - Shadowed and Gruff" / "James Oak - Vibrant and
    Captivating", so passing just "Malakai" or "James Oak" must still resolve).
    """
    name = VOICE_ALIASES.get(voice_name.lower(), voice_name)
    resp = requests.get(
        f"{ELEVEN_API_BASE}/v1/voices",
        headers={"xi-api-key": api_key},
        timeout=20,
    )
    resp.raise_for_status()
    voices = resp.json().get("voices", [])
    needle = name.lower()
    # Exact match first
    for v in voices:
        if v.get("name", "").lower() == needle:
            return v["voice_id"]
    # Prefix match — handles "Malakai - Shadowed and Gruff" given "Malakai"
    for v in voices:
        n = v.get("name", "").lower()
        if n == needle or n.startswith(f"{needle} ") or n.startswith(f"{needle} -"):
            return v["voice_id"]
    raise ValueError(f"Voice not found: {name}")


def convert_with_timestamps(api_key: str, voice_id: str, text: str, model_id: str = "eleven_v3") -> dict:
    """POST /v1/text-to-speech/{voice_id}/with-timestamps. Returns the parsed JSON.

    Response contains `audio_base64` plus `alignment` (raw, with bracket tags) and
    `normalized_alignment` (post-normalisation; brackets/v3 tags stripped — what we
    want for captions).
    """
    url = f"{ELEVEN_API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    body = {"text": text, "model_id": model_id, "output_format": "mp3_44100_128"}
    resp = requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    return resp.json()


def alignment_to_words(alignment: dict) -> list[dict]:
    """Group character-level alignment into words.

    A word is a run of non-space characters; its start = first char start_time,
    end = last char end_time. Spaces are dropped. Empty alignment → empty list.
    """
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        return []

    words: list[dict] = []
    buf: list[str] = []
    buf_start: float | None = None
    buf_end: float = 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append({
                    "text": "".join(buf),
                    "start": float(buf_start or 0.0),
                    "end": float(buf_end),
                })
                buf = []
                buf_start = None
        else:
            if buf_start is None:
                buf_start = float(s)
            buf.append(ch)
            buf_end = float(e)
    if buf:
        words.append({
            "text": "".join(buf),
            "start": float(buf_start or 0.0),
            "end": float(buf_end),
        })
    # Drop bracket-only words leaked by ElevenLabs normalization.
    return [w for w in words if not _BRACKET_TAG_WORD.match(w["text"])]


def get_audio_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def apply_emotion_tags(text: str, agent: str = "velikov") -> str:
    if not os.path.exists(TAG_CLI):
        return text
    try:
        result = subprocess.run(
            [sys.executable, TAG_CLI, text, "--agent", agent],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"  [tag_cli] warning: {e}")
    return text


def narrate(job_dir: str, voice_name: str, agent: str = "velikov"):
    script_path = os.path.join(job_dir, "script.json")
    if not os.path.exists(script_path):
        print(f"ERROR: script.json not found in {job_dir}")
        sys.exit(1)

    with open(script_path) as f:
        script = json.load(f)

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set")
        sys.exit(1)

    print(f"Resolving voice: {voice_name}")
    voice_id = resolve_voice_id(api_key, voice_name)
    print(f"Voice ID: {voice_id}")

    audio_dir = os.path.join(job_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)

    segment_files = []

    for i, seg in enumerate(script["segments"], 1):
        out_path = os.path.join(audio_dir, f"segment_{i:02d}.mp3")
        print(f"Generating segment {i}/{len(script['segments'])}: {seg['text'][:60]}...")

        tagged_text = apply_emotion_tags(seg["text"], agent)

        result = convert_with_timestamps(api_key, voice_id, tagged_text)

        with open(out_path, "wb") as f:
            f.write(base64.b64decode(result["audio_base64"]))

        duration = get_audio_duration(out_path)
        seg["audio_file"] = out_path
        seg["duration"] = duration
        seg["tagged_text"] = tagged_text
        seg["words"] = alignment_to_words(
            result.get("normalized_alignment") or result.get("alignment") or {}
        )
        segment_files.append(out_path)
        word_count = len(seg["words"])
        print(f"  → {out_path} ({duration:.1f}s, {word_count} words aligned)")

    # Stitch all segments into final.mp3
    final_path = os.path.join(audio_dir, "final.mp3")
    concat_list = os.path.join(audio_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for seg_file in segment_files:
            f.write(f"file '{seg_file}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-c", "copy", final_path
    ], check=True, capture_output=True)

    total = get_audio_duration(final_path)
    print(f"\nFinal audio: {final_path} ({total:.1f}s total)")

    # Save updated script with durations
    with open(script_path, "w") as f:
        json.dump(script, f, indent=2)

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Generate narration via ElevenLabs")
    parser.add_argument("--job", required=True, help="Job directory path")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="ElevenLabs voice name")
    parser.add_argument("--agent", "--char-profile", default="velikov", dest="agent",
                        help="Agent name; loads groups/<agent>/voice_profile.md (e.g. stella, lydia, velikov, melody, aurelio). "
                             "--char-profile is kept as an alias for the rest of the YT pipeline.")
    args = parser.parse_args()
    narrate(args.job, args.voice, args.agent)


if __name__ == "__main__":
    main()
