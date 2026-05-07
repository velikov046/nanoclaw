#!/usr/bin/env python3
"""
Voice Note Transcriber — CLI
Transcribes Telegram OGG voice notes (and other audio) to text using Whisper.

Usage:
  python3 transcribe_ogg.py voice.ogg
  python3 transcribe_ogg.py voice.ogg --model small
  python3 transcribe_ogg.py voice.ogg --lang en
  python3 transcribe_ogg.py voice.ogg --out transcript.txt
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

CONVERTIBLE = {".ogg", ".oga", ".mp3", ".m4a", ".flac", ".aac", ".opus"}


def find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.exists(p):
            return p
    return None


def convert_to_wav(input_path, wav_path):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        sys.exit("ffmpeg not found. Install with: apt install ffmpeg")
    result = subprocess.run(
        [ffmpeg, "-y", "-i", input_path, "-ar", "16000", "-ac", "1", wav_path],
        capture_output=True,
    )
    if result.returncode != 0:
        sys.exit(f"ffmpeg conversion failed:\n{result.stderr.decode()}")


def transcribe(audio_path, model="base", language=None):
    try:
        import whisper
    except ImportError:
        sys.exit("whisper not found. Run: pip install openai-whisper")

    print(f"Loading Whisper '{model}'…", file=sys.stderr)
    w = whisper.load_model(model)
    opts = {}
    if language:
        opts["language"] = language
    result = w.transcribe(audio_path, **opts)
    return result["text"].strip()


def main():
    parser = argparse.ArgumentParser(description="Transcribe voice notes to text via Whisper")
    parser.add_argument("input", help="Audio file: ogg, mp3, m4a, flac, wav, etc.")
    parser.add_argument(
        "--model",
        default="base",
        help="Whisper model: tiny, base, small, medium, large (default: base)",
    )
    parser.add_argument("--lang", default=None, help="Language code e.g. en, fr (auto-detect if omitted)")
    parser.add_argument("--out", default="", help="Output file (default: print to stdout)")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"Error: file not found: {args.input}")

    ext = os.path.splitext(args.input)[1].lower()

    if ext in CONVERTIBLE:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            convert_to_wav(args.input, wav_path)
            text = transcribe(wav_path, model=args.model, language=args.lang)
        finally:
            os.unlink(wav_path)
    else:
        text = transcribe(args.input, model=args.model, language=args.lang)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
