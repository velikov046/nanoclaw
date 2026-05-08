"""
gen_closer.py — Build a closing segment for a YouTube video.

A real closer lands the message and earns the next click — narration's last
beat alone leaves viewers on dead air. This script produces a closer.mp4
with:

  - A closing line voiced via ElevenLabs (callback / thesis recap / hook
    for the next video — the agent picks the angle in script.json).
  - A visual (Aurora video by default; fallback to title-derived still gen).
  - Music that swells UP under the line and fades to black.
  - Optional end-card text overlay (channel name, subscribe prompt).

Reads from script.json:
  - closer_script   REQUIRED. 1-2 sentences. Aim for ≤4s of speech.
  - closer_visual_prompt   Aurora video prompt for the closing visual.
                           Falls back to thumbnail_prompt or title.
  - end_card        Optional bold text to burn in for the last 2-3s.
  - music_outro     Optional path. Falls back to per-char-profile music dir.

Writes:
  <job_dir>/closer.mp4   landscape 1920x1080, ~5-7s with audio + music swell.
  Updates script.json: closer_file = "<container path>/closer.mp4".

compose.py will append closer_file at the end of the timeline.

Usage:
  python3 gen_closer.py --job <job_dir> --voice Malakai --char-profile velikov
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from narrate import (
    apply_emotion_tags,
    convert_with_timestamps,
    get_audio_duration,
    resolve_voice_id,
)
from _aurora_via_grok import generate as aurora_generate
from gen_opener import (
    pick_music_sting,
    build_title_ass,
    _ass_for_filter,
)

CLOSER_TAIL_FADE_S = 1.5  # last 1.5s fades to black + music tail


def synthesise_closer(api_key: str, voice_name: str, line: str,
                      agent: str, out_audio: Path) -> float:
    voice_id = resolve_voice_id(api_key, voice_name)
    tagged = apply_emotion_tags(line, agent=agent)
    print(f"  [closer] tagged: {tagged}", file=sys.stderr)
    payload = convert_with_timestamps(api_key, voice_id, tagged)
    audio_b64 = payload.get("audio_base64") or ""
    if not audio_b64:
        raise RuntimeError("ElevenLabs returned no audio_base64 for closer.")
    out_audio.write_bytes(base64.b64decode(audio_b64))
    return get_audio_duration(str(out_audio))


def compose_closer(visual_mp4: Path, voice_audio: Path, music: Path | None,
                   end_card_ass: Path | None, out_path: Path,
                   dims: tuple[int, int], target_dur: float) -> None:
    """Mux visual + voice + music swell + (optional) end card → out_path.

    Music starts at -20 dB and swells to -10 dB over the line, then both
    music and video fade to black/silent in the last CLOSER_TAIL_FADE_S.
    No sidechain ducking — the closer wants emotional lift, not the clean
    voice-on-top mix the opener uses.
    """
    w, h = dims
    fade_start = max(0.0, target_dur - CLOSER_TAIL_FADE_S)

    inputs: list[str] = ["-i", str(visual_mp4), "-i", str(voice_audio)]
    music_idx = None
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_idx = 2

    vf = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},setsar=1,"
        f"fade=t=out:st={fade_start:.3f}:d={CLOSER_TAIL_FADE_S}"
    )
    if end_card_ass:
        vf += f",subtitles={_ass_for_filter(end_card_ass)}"
    vf += "[vout]"

    if music_idx is not None:
        # Linear swell: music starts at 0.1 (-20dB-ish), ramps to 0.32
        # (-10dB-ish) over the line, then fades to silence in tail.
        af = (
            f"[2:a]volume='min(0.1+0.22*t/{max(0.1, fade_start):.3f},0.32)':"
            f"eval=frame,afade=t=out:st={fade_start:.3f}:d={CLOSER_TAIL_FADE_S}[m];"
            f"[1:a][m]amix=inputs=2:duration=first:dropout_transition=0:"
            f"normalize=0[aout]"
        )
    else:
        af = (
            f"[1:a]afade=t=out:st={fade_start:.3f}:d={CLOSER_TAIL_FADE_S}[aout]"
        )

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", vf + ";" + af,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-r", "25",
        "-pix_fmt", "yuv420p",
        "-t", f"{target_dur:.3f}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main():
    ap = argparse.ArgumentParser(description="Build the closing segment of a video")
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

    line = (script.get("closer_script") or "").strip()
    if not line:
        # Fallback so legacy scripts produce something. Agents should set
        # closer_script for real production.
        title = script.get("title", "this story")
        line = f"And that's the question they don't want you asking about {title}."
        print(f"  [closer] no closer_script; falling back: {line}", file=sys.stderr)

    visual_prompt = (
        script.get("closer_visual_prompt")
        or script.get("thumbnail_prompt")
        or script.get("title", "")
    )
    end_card = script.get("end_card") or ""

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # 1) Voice the closing line
    audio_path = job / "closer_voice.mp3"
    print(f"[1/3] synthesising closer ({len(line)} chars)...", file=sys.stderr)
    line_dur = synthesise_closer(api_key, args.voice, line, args.char_profile, audio_path)
    print(f"  [closer] audio: {line_dur:.2f}s", file=sys.stderr)

    # 2) Visual via Aurora video
    visual_path = job / "closer_visual.mp4"
    print(f"[2/3] generating Aurora video for closer...", file=sys.stderr)
    aurora_generate(visual_prompt, visual_path, mode="video",
                    resolution="720p", duration="6s", timeout_s=300)

    # 3) Compose. Target = line + tail-fade, capped at 7s so the closer
    # doesn't outstay its welcome.
    target = min(7.0, line_dur + CLOSER_TAIL_FADE_S + 0.3)
    music = pick_music_sting(args.char_profile, script.get("music_outro"))
    if music:
        print(f"  [music] outro: {music.name}", file=sys.stderr)
    end_card_ass = None
    if end_card:
        from compose import dimensions_for
        w, h = dimensions_for(args.aspect)
        ass_path = job / "closer_endcard.ass"
        end_card_ass = build_title_ass(ass_path, end_card, target, w, h)
        print(f"  [end-card] {end_card!r}", file=sys.stderr)

    from compose import dimensions_for as _dims
    dims = _dims(args.aspect)
    out_path = job / "closer.mp4"
    print(f"[3/3] composing closer ({target:.2f}s)...", file=sys.stderr)
    compose_closer(visual_path, audio_path, music, end_card_ass, out_path,
                   dims, target)

    script["closer_file"] = str(out_path)
    script_path.write_text(json.dumps(script, indent=2))
    print(f"OK closer: {out_path} ({out_path.stat().st_size} bytes, {target:.2f}s)")


if __name__ == "__main__":
    main()
