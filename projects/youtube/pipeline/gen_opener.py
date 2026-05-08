"""
gen_opener.py — Build a punchy 5-second opener for a YouTube video.

The first 5 seconds of a YouTube video drive retention; a silent visual is
the worst possible opener. This script produces an opener.mp4 with:

  - A hook line voiced via ElevenLabs (audience question OR shocking truth)
  - An Aurora video clip as the visual (or fallback Aurora still + Ken Burns)
  - A music sting under the narration (sidechain-ducked beneath the voice)
  - Optional bold title-overlay text burned in via ASS

The agent writing the script.json supplies the hook content; this script is
the deterministic glue that turns those fields into an opener.mp4 sitting
next to the job's other outputs. compose.py picks it up via `opener_file`
(falls back to `kling_file` for back-compat with older scripts).

Reads from script.json:
  - hook_script    REQUIRED. 1-2 sentence hook in Velikov/Stella/Lydia voice.
                   Aim for ≤4s of speech to keep the opener under 5s total.
  - hook_mode      "question" | "truth" (informational; doesn't change behaviour).
  - opener_visual_prompt   Aurora video prompt. Falls back to title.
  - title_overlay  Optional bold text to burn over the visual.
  - music_intro    Optional path to a music sting. Falls back to per-char
                   profile first available MP3, or none.

Writes:
  <job_dir>/opener.mp4   landscape 1920x1080, ~4-6s with audio.
  Updates script.json: opener_file = "<container path>/opener.mp4".

Env:
  ELEVENLABS_API_KEY   required (also used by narrate.py).
  GROK_COOKIES_FILE    optional override for the Aurora cookies path.

Usage:
  python3 gen_opener.py --job <job_dir> --voice Malakai --char-profile velikov
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

# Reuse narrate.py's ElevenLabs glue rather than duplicating it.
from narrate import (
    apply_emotion_tags,
    convert_with_timestamps,
    get_audio_duration,
    resolve_voice_id,
)
from _aurora_via_grok import generate as aurora_generate

PIPELINE_DIR = Path(__file__).resolve().parent
DEFAULT_MUSIC_DIR = PIPELINE_DIR.parent / "music"

OPENER_TARGET_S = 5.0  # the pivotal first 5 seconds


def pick_music_sting(char_profile: str, override: str | Path | None) -> Path | None:
    """Resolve a music sting for the opener.

    Order: explicit override → <music_dir>/<char_profile>/intro.mp3 →
    first MP3 in <music_dir>/<char_profile>/ → None (silent).
    """
    if override:
        p = Path(override)
        return p if p.exists() else None
    char_dir = DEFAULT_MUSIC_DIR / char_profile
    intro = char_dir / "intro.mp3"
    if intro.exists():
        return intro
    if char_dir.exists():
        candidates = sorted(char_dir.glob("*.mp3"))
        if candidates:
            return candidates[0]
    return None


def synthesise_hook(api_key: str, voice_name: str, hook_text: str,
                    agent: str, out_audio: Path) -> float:
    """Tag + synthesise the hook line. Returns audio duration in seconds."""
    voice_id = resolve_voice_id(api_key, voice_name)
    tagged = apply_emotion_tags(hook_text, agent=agent)
    print(f"  [hook] tagged: {tagged}", file=sys.stderr)
    payload = convert_with_timestamps(api_key, voice_id, tagged)
    audio_b64 = payload.get("audio_base64") or ""
    if not audio_b64:
        raise RuntimeError("ElevenLabs returned no audio_base64 for hook line.")
    out_audio.write_bytes(base64.b64decode(audio_b64))
    return get_audio_duration(str(out_audio))


def build_title_ass(out_path: Path, title: str, duration: float,
                    aspect_w: int, aspect_h: int) -> Path:
    """Write a minimal ASS file that displays `title` centered, large, bold,
    with a fade-in over the opener duration. Caller ASS-escapes the path
    when wiring into the ffmpeg subtitles= filter."""
    # Font size relative to height (~10% of vertical).
    font_size = max(48, aspect_h // 12)
    out_path.write_text(
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {aspect_w}\nPlayResY: {aspect_h}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
        "BackColour, Bold, Outline, Shadow, Alignment, MarginV\n"
        f"Style: T,Impact,{font_size},&H00FFFFFF,&H00000000,&H80000000,"
        f"-1,4,2,2,{aspect_h // 6}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.30,{_ass_time(duration)},T,,0,0,0,,"
        f"{{\\fad(400,200)}}{title.upper()}\n",
        encoding="utf-8",
    )
    return out_path


def _ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_for_filter(p: Path) -> str:
    """Escape a path for ffmpeg's subtitles= filter argument."""
    s = str(p).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    return s


def compose_opener(visual_mp4: Path, hook_audio: Path, music: Path | None,
                   title_ass: Path | None, out_path: Path,
                   dims: tuple[int, int], target_dur: float) -> None:
    """Mux visual + voice + (ducked) music + (optional) title overlay → out_path.

    Visual is scale+crop'd to dims. Music is mixed at -14 dB under the voice
    via a sidechain compressor so the hook line stays clean. Output trims to
    target_dur (or hook duration, whichever is longer).
    """
    w, h = dims
    inputs: list[str] = ["-i", str(visual_mp4), "-i", str(hook_audio)]
    music_idx = None
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_idx = 2

    # Video filter: scale+crop to landscape, optional title overlay
    vf = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    if title_ass:
        vf += f",subtitles={_ass_for_filter(title_ass)}"
    vf += "[vout]"

    # Audio filter: voice + ducked music. Voice asplit so the same source
    # feeds the final mix and the sidechain key (ffmpeg refuses to consume
    # a stream twice). normalize=0 keeps voice at full level.
    if music_idx is not None:
        af = (
            "[1:a]asplit=2[v_main][v_key];"
            "[2:a]volume=0.2[m_pre];"
            "[m_pre][v_key]sidechaincompress=threshold=0.05:ratio=6:"
            "attack=10:release=250[m_ducked];"
            "[v_main][m_ducked]amix=inputs=2:duration=first:"
            "dropout_transition=0:normalize=0[aout]"
        )
        amap = "[aout]"
    else:
        af = "[1:a]anull[aout]"
        amap = "[aout]"

    filter_complex = vf + ";" + af

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", amap,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-r", "25",
        "-pix_fmt", "yuv420p",
        "-t", f"{target_dur:.3f}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser(description="Build a 5s opener for a YouTube video")
    ap.add_argument("--job", required=True)
    ap.add_argument("--voice", default="Malakai")
    ap.add_argument("--char-profile", default="velikov",
                    choices=["velikov", "lydia", "stella"])
    ap.add_argument("--aspect", default="landscape",
                    choices=["landscape", "vertical"])
    args = ap.parse_args()

    job = Path(args.job)
    script_path = job / "script.json"
    if not script_path.exists():
        print(f"ERROR: script.json not found in {job}", file=sys.stderr)
        sys.exit(1)
    script = json.loads(script_path.read_text())

    hook = (script.get("hook_script") or "").strip()
    if not hook:
        # Fallback: derive a question-style hook from title so legacy scripts
        # still produce something usable. The agent should set hook_script
        # explicitly for real production.
        title = script.get("title", "")
        hook = f"What if the story you've been told about {title.lower().rstrip('.')} isn't the whole truth?"
        print(f"  [hook] no hook_script set; falling back: {hook}", file=sys.stderr)
    hook_mode = script.get("hook_mode", "unspecified")
    print(f"  [hook] mode={hook_mode}", file=sys.stderr)

    visual_prompt = (
        script.get("opener_visual_prompt")
        or script.get("thumbnail_prompt")
        or script.get("title", "")
    )
    title_overlay = script.get("title_overlay") or ""

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # 1) Voice the hook
    audio_path = job / "opener_voice.mp3"
    print(f"[1/3] synthesising hook ({len(hook)} chars)...", file=sys.stderr)
    hook_dur = synthesise_hook(api_key, args.voice, hook, args.char_profile, audio_path)
    print(f"  [hook] audio: {hook_dur:.2f}s", file=sys.stderr)

    # 2) Aurora video for the visual. Quality 720p, 6s — gives us enough room
    # to trim cleanly under hook duration. We don't gate generation on the
    # circuit breaker explicitly; aurora_generate handles that.
    visual_path = job / "opener_visual.mp4"
    print(f"[2/3] generating Aurora video visual...", file=sys.stderr)
    aurora_generate(visual_prompt, visual_path, mode="video",
                    resolution="720p", duration="6s", timeout_s=300)

    # 3) Compose. Target = max(hook_dur + 0.5, OPENER_TARGET_S) so the hook
    # always lands fully and the opener doesn't end mid-word.
    target = max(OPENER_TARGET_S, hook_dur + 0.5)
    music = pick_music_sting(args.char_profile, script.get("music_intro"))
    if music:
        print(f"  [music] sting: {music.name}", file=sys.stderr)
    title_ass = None
    if title_overlay:
        ass_path = job / "opener_title.ass"
        from compose import dimensions_for  # noqa: E402  late import to avoid cycles
        w, h = dimensions_for(args.aspect)
        title_ass = build_title_ass(ass_path, title_overlay, target, w, h)
        print(f"  [title] burning overlay: {title_overlay!r}", file=sys.stderr)

    from compose import dimensions_for as _dims  # ensure import even without title
    dims = _dims(args.aspect)
    out_path = job / "opener.mp4"
    print(f"[3/3] composing opener ({target:.2f}s)...", file=sys.stderr)
    compose_opener(visual_path, audio_path, music, title_ass, out_path,
                   dims, target)

    # Update script.json so compose.py picks up opener_file. Preserve
    # kling_file as alias for back-compat.
    script["opener_file"] = str(out_path)
    script["kling_file"] = str(out_path)
    script_path.write_text(json.dumps(script, indent=2))
    print(f"OK opener: {out_path} ({out_path.stat().st_size} bytes, {target:.2f}s)")


if __name__ == "__main__":
    main()
