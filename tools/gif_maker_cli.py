"""
gif_maker_cli.py  —  convert a video clip to GIF from the command line

Usage:
  python gif_maker_cli.py INPUT [OUTPUT] [options]

Examples:
  python gif_maker_cli.py clip.mp4
  python gif_maker_cli.py clip.mp4 out.gif --fps 12 --width 360
  python gif_maker_cli.py clip.mp4 --start 0:05 --end 0:20
  python gif_maker_cli.py clip.mp4 --subs external subs.srt
  python gif_maker_cli.py clip.mp4 --subs embedded --track 0
  python gif_maker_cli.py clip.mp4 --subs speech --model base --lang en
  python gif_maker_cli.py clip.mp4 --subs speech --size 28 --color yellow --outline
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

FFMPEG  = r"C:\Users\Sol\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\Users\Sol\ffmpeg\bin\ffprobe.exe"

COLOR_MAP = {
    "white":  "&H00FFFFFF",
    "yellow": "&H0000FFFF",
    "cyan":   "&H00FFFF00",
    "red":    "&H000000FF",
    "black":  "&H00000000",
}


def status(msg):
    print(f"  {msg}", flush=True)


def probe_subtitle_tracks(video):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "s", video],
            capture_output=True, text=True
        )
        streams = json.loads(r.stdout).get("streams", [])
        tracks = []
        for i, s in enumerate(streams):
            lang  = s.get("tags", {}).get("language", "")
            title = s.get("tags", {}).get("title", "")
            label = f"Track {i}"
            if title: label += f" — {title}"
            if lang:  label += f" [{lang}]"
            tracks.append((i, label))
        return tracks
    except Exception:
        return []


def srt_time(seconds):
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def transcribe_to_srt(audio_path, srt_path, model_name, language):
    from faster_whisper import WhisperModel
    status(f"Loading Whisper model '{model_name}'…")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    status("Transcribing audio…")
    lang = language.strip() or None
    segments, info = model.transcribe(audio_path, beam_size=5, language=lang)
    detected = getattr(info, "language", "unknown")
    status(f"Detected language: {detected}")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{srt_time(seg.start)} --> {srt_time(seg.end)}\n{seg.text.strip()}\n\n")


def extract_audio(ffmpeg, inp, start, end):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    cmd = [ffmpeg, "-y"]
    if start: cmd += ["-ss", start]
    if end:   cmd += ["-to", end]
    cmd += ["-i", inp, "-vn", "-ar", "16000", "-ac", "1", tmp.name]
    subprocess.run(cmd, capture_output=True)
    return tmp.name


def build_filter(args, sub_path):
    parts = []
    if sub_path and os.path.exists(sub_path):
        escaped = sub_path.replace("\\", "/").replace(":", "\\:")
        color   = COLOR_MAP.get(args.color, "&H00FFFFFF")
        outline = 2 if args.outline else 0
        shadow  = 2 if args.shadow else 0
        bold    = 1 if args.bold else 0
        style   = f"FontSize={args.size},PrimaryColour={color},Outline={outline},Shadow={shadow},Bold={bold}"
        parts.append(f"subtitles='{escaped}':force_style='{style}'")
    parts.append(f"fps={args.fps}")
    parts.append(f"scale={args.width}:-1:flags=lanczos")
    parts.append("split[s0][s1];[s0]palettegen=max_colors=256:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer")
    return ",".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Convert a video clip to GIF")
    parser.add_argument("input",           help="Input video file")
    parser.add_argument("output",          nargs="?", help="Output GIF (default: input basename + .gif)")
    parser.add_argument("--start",         default="",   help="Start time e.g. 00:00:05 or 5")
    parser.add_argument("--end",           default="",   help="End time (default: full clip)")
    parser.add_argument("--fps",           type=int, default=15, help="Frames per second (default: 15)")
    parser.add_argument("--width",         type=int, default=480, help="Output width in px (default: 480)")
    # Subtitles
    parser.add_argument("--subs",          choices=["external", "embedded", "speech"],
                        help="Subtitle source")
    parser.add_argument("--sub-file",      dest="sub_file", default="",
                        help="Path to subtitle file (for --subs external)")
    parser.add_argument("--track",         type=int, default=0,
                        help="Embedded subtitle track index (default: 0)")
    parser.add_argument("--model",         default="base", choices=["tiny","base","small","medium","large"],
                        help="Whisper model size (default: base)")
    parser.add_argument("--lang",          default="",
                        help="Language code for Whisper e.g. en, fr (default: auto-detect)")
    # Subtitle style
    parser.add_argument("--size",          type=int, default=24, help="Subtitle font size (default: 24)")
    parser.add_argument("--color",         default="white", choices=list(COLOR_MAP.keys()),
                        help="Subtitle color (default: white)")
    parser.add_argument("--outline",       action="store_true", help="Add outline to subtitles")
    parser.add_argument("--shadow",        action="store_true", help="Add shadow to subtitles")
    parser.add_argument("--bold",          action="store_true", help="Bold subtitles")
    # List tracks
    parser.add_argument("--list-tracks",   action="store_true",
                        help="List embedded subtitle tracks and exit")

    args = parser.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"Error: input file not found: {args.input}")

    if args.list_tracks:
        tracks = probe_subtitle_tracks(args.input)
        if not tracks:
            print("No embedded subtitle tracks found.")
        else:
            for idx, label in tracks:
                print(f"  {idx}: {label}")
        return

    output = args.output or os.path.splitext(args.input)[0] + ".gif"
    temps  = []

    try:
        sub_path = None

        if args.subs == "external":
            if not args.sub_file:
                sys.exit("Error: --subs external requires --sub-file")
            if not os.path.exists(args.sub_file):
                sys.exit(f"Error: subtitle file not found: {args.sub_file}")
            sub_path = args.sub_file

        elif args.subs == "embedded":
            tracks = probe_subtitle_tracks(args.input)
            if not tracks:
                sys.exit("Error: no embedded subtitle tracks found")
            if args.track >= len(tracks):
                sys.exit(f"Error: track {args.track} not found (available: 0–{len(tracks)-1})")
            status(f"Extracting {tracks[args.track][1]}…")
            tmp = tempfile.NamedTemporaryFile(suffix=".srt", delete=False)
            tmp.close()
            temps.append(tmp.name)
            subprocess.run(
                [FFMPEG, "-y", "-i", args.input, "-map", f"0:s:{args.track}", tmp.name],
                capture_output=True
            )
            sub_path = tmp.name

        elif args.subs == "speech":
            audio = extract_audio(FFMPEG, args.input, args.start, args.end)
            temps.append(audio)
            srt = tempfile.NamedTemporaryFile(suffix=".srt", delete=False)
            srt.close()
            temps.append(srt.name)
            transcribe_to_srt(audio, srt.name, args.model, args.lang)
            sub_path = srt.name

        status("Rendering GIF…")
        cmd = [FFMPEG, "-y"]
        if args.start: cmd += ["-ss", args.start]
        if args.end:   cmd += ["-to", args.end]
        cmd += ["-i", args.input, "-vf", build_filter(args, sub_path), output]

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            sys.exit(f"FFmpeg error:\n{result.stderr[-800:]}")

        kb = os.path.getsize(output) // 1024
        print(f"Done — {kb} KB → {output}")

    finally:
        for t in temps:
            try: os.unlink(t)
            except Exception: pass


if __name__ == "__main__":
    main()
