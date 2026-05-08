"""
compose.py — Assemble final MP4 from images + audio with Ken Burns effect.

Usage:
  python3 compose.py --job <job_dir> [--music path/to/track.mp3] [--music-volume 0.15]

Reads: <job_dir>/script.json (image_file, audio_file, duration per segment;
       optionally kling_file and broll_files if gen_broll.py was run)
Writes: <job_dir>/output.mp4

If kling_file or broll_files are present, they are prepended as a silent visual
opener before the narrated segments begin. Add --music to cover the silence.
"""

import argparse
import json
import os
import random
import subprocess
import sys

from caption_utils import build_ass_file

# Default location for curated per-character music. Override with --music-dir
# or by passing an absolute --music path. Layout:
#   <music_dir>/<char_profile>/track1.mp3
#   <music_dir>/<char_profile>/track2.mp3
#   ...
DEFAULT_MUSIC_DIR = os.path.join(os.path.dirname(__file__), "..", "music")
MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav")


ASPECT_DIMS = {
    "landscape": (1920, 1080),  # 16:9 — YouTube long-form, default
    "vertical": (1080, 1920),   # 9:16 — TikTok, YouTube Shorts, Instagram Reels
}
ZOOM_SPEED = 0.0008   # subtle — too fast looks cheap


def dimensions_for(aspect: str) -> tuple[int, int]:
    return ASPECT_DIMS.get(aspect, ASPECT_DIMS["landscape"])


def pick_music(char_profile: str, music_dir: str) -> str | None:
    """Pick a music file from <music_dir>/<char_profile>/ avoiding the immediate repeat.

    Returns absolute path to the chosen file, or None if the folder is missing
    / empty. Records the pick in `<char_dir>/.last_picked.json` so the next call
    can dodge a back-to-back duplicate when more than one track is available.
    """
    char_dir = os.path.abspath(os.path.join(music_dir, char_profile))
    if not os.path.isdir(char_dir):
        return None
    candidates = sorted(
        f for f in os.listdir(char_dir)
        if f.lower().endswith(MUSIC_EXTS) and not f.startswith(".")
    )
    if not candidates:
        return None

    state_path = os.path.join(char_dir, ".last_picked.json")
    last = None
    if os.path.exists(state_path):
        try:
            last = json.load(open(state_path)).get("last")
        except Exception:
            last = None

    pool = [c for c in candidates if c != last] or candidates
    pick = random.choice(pool)
    try:
        with open(state_path, "w") as f:
            json.dump({"last": pick}, f)
    except OSError:
        pass  # state file is best-effort, not load-bearing
    return os.path.join(char_dir, pick)


def resolve_music(script: dict, char_profile: str, music_dir: str,
                   cli_music: str | None) -> str | None:
    """Decide which music file (if any) compose should mix in.

    Precedence:
      1. CLI --music wins (absolute path or filename inside the char folder).
      2. script.json `music` field:
           false / missing → no music
           true            → auto-pick from <music_dir>/<char>/
           "name.ext"      → look up in <music_dir>/<char>/ then anywhere relative
           "/abs/path"     → use as-is if it exists
    """
    def resolve_token(token: str) -> str | None:
        if os.path.isabs(token):
            return token if os.path.exists(token) else None
        char_local = os.path.join(music_dir, char_profile, token)
        if os.path.exists(char_local):
            return char_local
        if os.path.exists(token):
            return os.path.abspath(token)
        return None

    if cli_music:
        return resolve_token(cli_music)

    music = script.get("music", False)
    if music is False or music is None:
        return None
    if music is True:
        return pick_music(char_profile, music_dir)
    if isinstance(music, str):
        return resolve_token(music)
    return None


def get_audio_duration(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


# zoompan x/y expressions per focus anchor. Each formula uses the live `zoom`
# variable so the viewport adjusts smoothly across the duration.
FOCUS_XY: dict[str, tuple[str, str]] = {
    "center":       ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "top":          ("iw/2-(iw/zoom/2)", "0"),
    "bottom":       ("iw/2-(iw/zoom/2)", "ih-ih/zoom"),
    "left":         ("0",                "ih/2-(ih/zoom/2)"),
    "right":        ("iw-iw/zoom",       "ih/2-(ih/zoom/2)"),
    "top-left":     ("0",                "0"),
    "top-right":    ("iw-iw/zoom",       "0"),
    "bottom-left":  ("0",                "ih-ih/zoom"),
    "bottom-right": ("iw-iw/zoom",       "ih-ih/zoom"),
}


# Supersample factor: zoompan defaults to integer-pixel positioning, so a
# slow zoom on a final-resolution source rounds to visible 1-pixel jumps
# every few frames. Pre-scaling the source 4x and letting zoompan output at
# the target dim gives sub-pixel precision and visibly smoother motion.
ZOOMPAN_SUPERSAMPLE = 4


def _zoompan_pre(w: int, h: int) -> str:
    """Pre-filter chain that supersamples the source for smooth zoompan output.

    Input is upscaled with lanczos to ZOOMPAN_SUPERSAMPLE * (w, h) and cropped
    to the target aspect. zoompan then operates on this high-res source and
    emits at WxH, which gives the appearance of sub-pixel motion without
    needing a custom interpolation filter.
    """
    sw, sh = w * ZOOMPAN_SUPERSAMPLE, h * ZOOMPAN_SUPERSAMPLE
    return (
        f"scale={sw}:{sh}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={sw}:{sh},"
    )


def _directed_zoom_filter(frames: int, dims: tuple[int, int],
                           zoom: str, focus: str) -> str:
    """Build a zoompan expression from author-supplied zoom + focus directives."""
    w, h = dims
    fx, fy = FOCUS_XY.get(focus, FOCUS_XY["center"])
    if zoom == "out":
        z_expr = f"if(lte(zoom,1.0),1.3,max(1.001,zoom-{ZOOM_SPEED}))"
    else:
        z_expr = f"min(zoom+{ZOOM_SPEED},1.3)"
    return (
        f"{_zoompan_pre(w, h)}"
        f"zoompan=z='{z_expr}':d={frames}:s={w}x{h}:x='{fx}':y='{fy}'"
    )


def ken_burns_filter(duration: float, index: int, dims: tuple[int, int],
                      motion: dict | None = None) -> str:
    """Return a zoompan + scale filter string for a single image clip.

    If `motion` is supplied with `zoom` and/or `focus` keys, the filter is
    deterministic. Otherwise the index picks one of four alternating patterns
    so adjacent beats don't all pan the same way. The variants must NOT share
    a center coordinate that differs by a pixel-quantised offset (the prior
    `+/- {index%2*2}` trick) — that produces visible 2-4px jumps at every
    beat boundary. Use distinct continuous motions instead.
    """
    fps = 25
    frames = max(1, int(duration * fps))
    w, h = dims

    if motion:
        return _directed_zoom_filter(
            frames, dims,
            zoom=motion.get("zoom", "in"),
            focus=motion.get("focus", "center"),
        )

    pre = _zoompan_pre(w, h)
    patterns = [
        # zoom in, centered
        f"{pre}zoompan=z='min(zoom+{ZOOM_SPEED},1.3)':d={frames}:s={w}x{h}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'",
        # zoom in, drifting toward upper-right (continuous, scales with iw)
        f"{pre}zoompan=z='min(zoom+{ZOOM_SPEED},1.3)':d={frames}:s={w}x{h}:x='iw*0.55-(iw/zoom/2)':y='ih*0.45-(ih/zoom/2)'",
        # zoom out from top-left
        f"{pre}zoompan=z='if(lte(zoom,1.0),1.3,max(1.001,zoom-{ZOOM_SPEED}))':d={frames}:s={w}x{h}:x='0':y='0'",
        # zoom out from bottom-right
        f"{pre}zoompan=z='if(lte(zoom,1.0),1.3,max(1.001,zoom-{ZOOM_SPEED}))':d={frames}:s={w}x{h}:x='iw-iw/zoom':y='ih-ih/zoom'",
    ]
    return patterns[index % len(patterns)]


def beats_for_segment(seg: dict) -> list[dict]:
    """Return a normalised beats list for the segment.

    Backward compat: if no beats[], synthesise a single beat using image_file
    on the segment itself. Trailing beats with at_sec >= duration are dropped.
    If seg.words is present (from narrate.py with-timestamps), beat at_sec is
    snapped to the nearest word boundary so cuts hit on actual narration beats.
    """
    duration = float(seg.get("duration") or 0.0)
    raw = seg.get("beats")
    if not raw:
        return [{
            "at_sec": 0.0,
            "image_file": seg.get("image_file"),
        }]
    beats = []
    for b in raw:
        at = float(b.get("at_sec", 0.0) or 0.0)
        if duration > 0 and at >= duration:
            continue
        beats.append({
            "at_sec": at,
            "image_file": b.get("image_file"),
            "motion": b.get("motion"),
        })
    if not beats:
        beats = [{"at_sec": 0.0, "image_file": seg.get("image_file"), "motion": None}]
    beats.sort(key=lambda b: b["at_sec"])
    # First beat must start at 0 — clamp if author left a gap.
    beats[0]["at_sec"] = 0.0

    words = seg.get("words")
    if words and len(beats) > 1:
        word_starts = [float(w["start"]) for w in words]
        prev_end = 0.0
        for k in range(1, len(beats)):
            target = beats[k]["at_sec"]
            # Candidates must come strictly after the previous beat to avoid
            # a beat collapsing onto its predecessor.
            candidates = [ws for ws in word_starts if ws > prev_end]
            if not candidates:
                continue
            best = min(candidates, key=lambda ws: abs(ws - target))
            beats[k]["at_sec"] = round(best, 3)
            prev_end = best
    return beats


BROLL_MAX_DURATION = 6.0  # trim each B-roll clip to this length


def normalize_video_clip(input_path: str, out_path: str,
                         dims: tuple[int, int],
                         max_duration: float | None = None):
    """Resize+crop to target dims, strip audio, optionally trim. For kling/broll clips."""
    w, h = dims
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}"
    )
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
    ]
    if max_duration:
        cmd += ["-t", str(max_duration)]
    cmd.append(out_path)
    subprocess.run(cmd, check=True, capture_output=True)


def _ass_for_filter(path: str) -> str:
    """Escape a path for use inside an ffmpeg filter argument.

    ffmpeg filtergraph parses : and , as separators and \\ as escape, so absolute
    paths need their colons and backslashes escaped. Single quotes in filter args
    also need wrapping for safety.
    """
    return path.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


def build_segment_video(seg: dict, audio_path: str, duration: float,
                         segment_index: int, out_path: str,
                         char_profile: str, tmp_dir: str,
                         aspect: str = "landscape"):
    """Render one segment as N beat sub-clips concatenated, muxed with audio.

    Each beat gets its own Ken Burns. Beat duration = next_beat.at_sec - this.at_sec
    (or remaining segment duration for the last beat). If seg has tagged_text and
    words from narrate.py, captions are burned in via ASS subtitles.
    """
    beats = beats_for_segment(seg)
    fps = 25
    dims = dimensions_for(aspect)

    # Caption track (optional — only if narrate.py left us alignment + tags)
    ass_path: str | None = None
    if seg.get("words") and seg.get("tagged_text"):
        ass_candidate = os.path.join(tmp_dir, f"seg_{segment_index + 1:02d}.ass")
        ass_path = build_ass_file(seg, char_profile, 0.0, ass_candidate, aspect=aspect)
    subtitle_filter = f"subtitles={_ass_for_filter(ass_path)}" if ass_path else None

    # Resolve per-beat durations
    beat_durations: list[float] = []
    for k, b in enumerate(beats):
        end = beats[k + 1]["at_sec"] if k + 1 < len(beats) else duration
        d = max(0.1, end - b["at_sec"])  # guard against zero-length beats
        beat_durations.append(d)

    # Validate inputs
    for k, b in enumerate(beats):
        img = b.get("image_file")
        if not img or not os.path.exists(img):
            raise FileNotFoundError(
                f"segment {segment_index + 1} beat {k + 1}: image not found ({img})"
            )

    # Single-beat fast path — keep behaviour identical to legacy compose
    if len(beats) == 1:
        vf_parts = [ken_burns_filter(duration, segment_index, dims, beats[0].get("motion"))]
        if subtitle_filter:
            vf_parts.append(subtitle_filter)
        vf = ",".join(vf_parts)
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", beats[0]["image_file"],
            "-i", audio_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-r", str(fps),
            "-t", str(duration),
            "-pix_fmt", "yuv420p",
            "-shortest",
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return

    # Multi-beat path — one ffmpeg run, filter_complex with per-beat zoompan + concat
    cmd: list[str] = ["ffmpeg", "-y"]
    for b, d in zip(beats, beat_durations):
        cmd += ["-loop", "1", "-t", f"{d:.3f}", "-i", b["image_file"]]
    cmd += ["-i", audio_path]

    # The audio input index sits after all image inputs
    audio_idx = len(beats)

    filter_parts = []
    for k, d in enumerate(beat_durations):
        # ken_burns_filter index varies per beat to alternate pan direction
        # when no per-beat motion hint is supplied; with motion, it's deterministic.
        kb = ken_burns_filter(d, segment_index * 7 + k, dims, beats[k].get("motion"))
        filter_parts.append(f"[{k}:v]{kb},setsar=1[v{k}]")
    concat_inputs = "".join(f"[v{k}]" for k in range(len(beats)))
    concat_out = "[vcat]" if subtitle_filter else "[vout]"
    filter_parts.append(f"{concat_inputs}concat=n={len(beats)}:v=1:a=0{concat_out}")
    if subtitle_filter:
        filter_parts.append(f"[vcat]{subtitle_filter}[vout]")
    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(fps),
        "-t", f"{duration:.3f}",
        "-pix_fmt", "yuv420p",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def compose(job_dir: str, music_path: str | None, music_volume: float,
            char_profile: str = "velikov", aspect: str = "landscape"):
    script_path = os.path.join(job_dir, "script.json")
    if not os.path.exists(script_path):
        print(f"ERROR: script.json not found in {job_dir}")
        sys.exit(1)

    with open(script_path) as f:
        script = json.load(f)

    segments = script["segments"]
    # Tmp dir is namespaced by aspect so landscape and vertical renders in the
    # same job dir don't trample each other's intermediates.
    tmp_dir = os.path.join(job_dir, "tmp" if aspect == "landscape" else f"tmp_{aspect}")
    os.makedirs(tmp_dir, exist_ok=True)
    dims = dimensions_for(aspect)

    segment_videos = []

    # Prepend opener if any. Two flavours:
    #   - opener_file (NEW): produced by gen_opener.py, already has voice +
    #     music + correct landscape framing. Use as-is, just normalise to
    #     ensure same encode params as the rest of the timeline.
    #   - kling_file (LEGACY): silent visual clip from kling/aurora video.
    #     normalize_video_clip strips its audio, so we add a silent stereo
    #     AAC track before concat — the concat demuxer drops audio from the
    #     entire timeline if any input has a different stream layout.
    opener_file = script.get("opener_file")
    if opener_file and os.path.exists(opener_file):
        opener_norm = os.path.join(tmp_dir, "opener_reencoded.mp4")
        print(f"Re-encoding opener ({os.path.basename(opener_file)})...")
        # Pass through audio + video at the timeline's encode params so
        # concat doesn't have to mux mismatched codec configs.
        subprocess.run([
            "ffmpeg", "-y", "-i", opener_file,
            "-vf", f"scale={dims[0]}:{dims[1]}:force_original_aspect_ratio=increase,"
                   f"crop={dims[0]}:{dims[1]},setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-r", "25", "-pix_fmt", "yuv420p",
            opener_norm,
        ], check=True, capture_output=True)
        segment_videos.append(opener_norm)
    else:
        kling_file = script.get("kling_file")
        if kling_file and os.path.exists(kling_file):
            kling_norm = os.path.join(tmp_dir, "opener_norm.mp4")
            print(f"Normalizing legacy opener ({os.path.basename(kling_file)})...")
            normalize_video_clip(kling_file, kling_norm, dims)
            opener_padded = os.path.join(tmp_dir, "opener_with_silence.mp4")
            subprocess.run([
                "ffmpeg", "-y",
                "-i", kling_norm,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                opener_padded,
            ], check=True, capture_output=True)
            segment_videos.append(opener_padded)

    # Prepend B-roll clips (muted, trimmed) if generated. Same silent-audio
    # padding as the opener — concat demuxer needs every input to carry the
    # same stream layout or it drops audio from the whole timeline.
    broll_files = script.get("broll_files", [])
    for bi, broll_path in enumerate(broll_files):
        if not os.path.exists(broll_path):
            print(f"  [B-roll] Missing: {broll_path} — skipping.")
            continue
        broll_norm = os.path.join(tmp_dir, f"broll_{bi:02d}_norm.mp4")
        print(f"Normalizing B-roll {bi+1}/{len(broll_files)}: {os.path.basename(broll_path)}...")
        normalize_video_clip(broll_path, broll_norm, dims, max_duration=BROLL_MAX_DURATION)
        broll_padded = os.path.join(tmp_dir, f"broll_{bi:02d}_with_silence.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-i", broll_norm,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            broll_padded,
        ], check=True, capture_output=True)
        segment_videos.append(broll_padded)

    for i, seg in enumerate(segments, 1):
        audio_file = seg.get("audio_file")
        duration = seg.get("duration")

        if not audio_file or not os.path.exists(audio_file):
            print(f"ERROR: audio not found for segment {i}: {audio_file}")
            sys.exit(1)

        beats = beats_for_segment(seg)
        for k, b in enumerate(beats, 1):
            if not b.get("image_file") or not os.path.exists(b["image_file"]):
                print(f"ERROR: image not found for segment {i} beat {k}: {b.get('image_file')}")
                sys.exit(1)

        seg_video = os.path.join(tmp_dir, f"seg_{i:02d}.mp4")
        beat_summary = f"{len(beats)} beat" + ("s" if len(beats) != 1 else "")
        cap_summary = " + captions" if seg.get("words") and seg.get("tagged_text") else ""
        print(f"Composing segment {i}/{len(segments)} ({duration:.1f}s, {beat_summary}{cap_summary})...")
        build_segment_video(seg, audio_file, duration, i - 1, seg_video, char_profile, tmp_dir, aspect)
        segment_videos.append(seg_video)

    # Append closer if any. gen_closer.py produces a clip with voice + music
    # swell + fade-to-black already baked in, so we just re-encode to
    # timeline params and tack it on the end.
    closer_file = script.get("closer_file")
    if closer_file and os.path.exists(closer_file):
        closer_norm = os.path.join(tmp_dir, "closer_reencoded.mp4")
        print(f"Re-encoding closer ({os.path.basename(closer_file)})...")
        subprocess.run([
            "ffmpeg", "-y", "-i", closer_file,
            "-vf", f"scale={dims[0]}:{dims[1]}:force_original_aspect_ratio=increase,"
                   f"crop={dims[0]}:{dims[1]},setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-r", "25", "-pix_fmt", "yuv420p",
            closer_norm,
        ], check=True, capture_output=True)
        segment_videos.append(closer_norm)

    # Concat all segments
    concat_list = os.path.join(tmp_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for v in segment_videos:
            f.write(f"file '{v}'\n")

    raw_output = os.path.join(tmp_dir, "raw.mp4")
    print("Concatenating segments...")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_list, "-c", "copy", raw_output
    ], check=True, capture_output=True)

    final_name = "output.mp4" if aspect == "landscape" else f"output_{aspect}.mp4"
    final_output = os.path.join(job_dir, final_name)

    if music_path and os.path.exists(music_path):
        print(f"Mixing music: {music_path} (volume {music_volume}, sidechain ducking on)")
        total_duration = get_audio_duration(raw_output)
        # Narration is asplit so the same stream can feed both the final mix
        # and the sidechain key (ffmpeg refuses to consume a stream twice).
        # The compressor drops music ~10-12dB whenever VO crosses the threshold;
        # attack/release tuned so music recovers smoothly between sentences.
        # amix's default normalize=1 divides every input by the input count, so
        # narration would silently halve and the (already-attenuated) music
        # vanishes. normalize=0 keeps both at their pre-mix levels and just sums.
        # Threshold 0.1 / ratio 4 = lighter ducking than the previous guesses;
        # the music breathes between sentences instead of being squashed flat.
        filter_complex = (
            f"[0:a]asplit=2[a_main][a_key];"
            f"[1:a]volume={music_volume},afade=t=out:st={max(0, total_duration-3)}:d=3[music_pre];"
            f"[music_pre][a_key]sidechaincompress=threshold=0.1:ratio=4:attack=20:release=300[music_ducked];"
            f"[a_main][music_ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
        subprocess.run([
            "ffmpeg", "-y",
            "-i", raw_output,
            "-stream_loop", "-1", "-i", music_path,
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", str(total_duration),
            final_output,
        ], check=True, capture_output=True)
    else:
        os.rename(raw_output, final_output)

    duration = get_audio_duration(final_output)
    print(f"\nOutput: {final_output} ({duration:.1f}s)")

    script["output_file"] = final_output
    with open(script_path, "w") as f:
        json.dump(script, f, indent=2)

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Compose video with Ken Burns effect")
    parser.add_argument("--job", required=True, help="Job directory path")
    parser.add_argument("--music", default=None,
                        help="Music override: absolute path, or filename inside "
                             "<music-dir>/<char-profile>/. Beats script.json `music`.")
    parser.add_argument("--music-dir", default=DEFAULT_MUSIC_DIR,
                        help="Root music dir; expects per-char-profile subfolders "
                             "(default: projects/youtube/music)")
    parser.add_argument("--music-volume", type=float, default=0.3,
                        help="Music volume gain (default 0.3). Real tracks normalize "
                             "around -1 dB peak; 0.3 puts them ~10 dB below VO before ducking.")
    parser.add_argument("--char-profile", default="velikov",
                        choices=["velikov", "stella", "lydia"],
                        help="Caption styling profile (default velikov)")
    parser.add_argument("--aspect", default="landscape",
                        choices=list(ASPECT_DIMS.keys()),
                        help="Render aspect — 'landscape' (1920x1080) or 'vertical' "
                             "(1080x1920 for TikTok / YouTube Shorts / Reels)")
    args = parser.parse_args()

    # Resolve music up front so failures here surface before composition starts.
    with open(os.path.join(args.job, "script.json")) as f:
        script_for_music = json.load(f)
    music_path = resolve_music(script_for_music, args.char_profile,
                                args.music_dir, args.music)
    if music_path:
        print(f"Music resolved: {music_path}")
    elif script_for_music.get("music") or args.music:
        # User asked for music but we couldn't find any — warn but continue silent.
        print(f"WARN: music requested but no track resolved from {args.music_dir}/{args.char_profile}/")

    compose(args.job, music_path, args.music_volume, args.char_profile, args.aspect)


if __name__ == "__main__":
    main()
