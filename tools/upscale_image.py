#!/usr/bin/env python3
"""
Image upscaler — CLI wrapper around upscayl-bin (Real-ESRGAN ncnn-vulkan).

Single file or directory mode. Idempotent (skips outputs that already exist).
Host-side tool: the binary is a Linux ELF and uses Vulkan compute. WSL2 falls
back to Mesa llvmpipe if no GPU is exposed, which still works but is slower.

Usage:
  python3 upscale_image.py figure.jpg
  python3 upscale_image.py figure.jpg --model digital-art-4x --scale 4
  python3 upscale_image.py book_dir/ --out book_dir_upscaled/
  python3 upscale_image.py --list-models

Inside the Velikov ingest pipeline, set VELIKOV_UPSCALE_IMAGES=1 — the
ingest_book.py hook calls this script in directory mode after image extraction.
"""

import argparse
import os
import shutil
import subprocess
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BIN = os.path.join(THIS_DIR, "upscayl-linux", "bin", "upscayl-bin")
DEFAULT_MODELS = os.path.join(THIS_DIR, "upscayl-linux", "models")
DEFAULT_MODEL = "upscayl-standard-4x"
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def resolve_bin():
    return os.environ.get("UPSCAYL_BIN") or DEFAULT_BIN


def resolve_models():
    return os.environ.get("UPSCAYL_MODELS") or DEFAULT_MODELS


def list_models(models_dir):
    names = set()
    if os.path.isdir(models_dir):
        for f in os.listdir(models_dir):
            stem, ext = os.path.splitext(f)
            if ext in (".param", ".bin"):
                names.add(stem)
    return sorted(names)


def is_supported(path):
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTS


def upscale_one(bin_path, models_dir, model, scale, in_path, out_path, fmt, compress, tile, verbose):
    cmd = [
        bin_path,
        "-i", in_path,
        "-o", out_path,
        "-m", models_dir,
        "-n", model,
        "-s", str(scale),
        "-c", str(compress),
    ]
    if tile is not None:
        cmd += ["-t", str(tile)]
    if fmt:
        cmd += ["-f", fmt]
    if verbose:
        cmd.append("-v")
        print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=not verbose)
    if result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace") if not verbose else ""
        print(f"  ✗ {os.path.basename(in_path)}: upscayl-bin exit {result.returncode}\n{err}", file=sys.stderr)
        return False
    return True


def collect_inputs(in_path):
    if os.path.isfile(in_path):
        return [in_path] if is_supported(in_path) else []
    out = []
    for root, _, files in os.walk(in_path):
        for f in files:
            p = os.path.join(root, f)
            if is_supported(p):
                out.append(p)
    return sorted(out)


def default_output_for(in_file, scale):
    stem, ext = os.path.splitext(in_file)
    return f"{stem}.x{scale}{ext if ext.lower() in SUPPORTED_EXTS else '.png'}"


def main():
    parser = argparse.ArgumentParser(description="Upscale images via upscayl-bin (Real-ESRGAN ncnn-vulkan)")
    parser.add_argument("input", nargs="?", help="Input file or directory")
    parser.add_argument("--out", "-o", default=None, help="Output file or directory (default: sibling .x{scale} file, or <dir>-upscaled/)")
    parser.add_argument("--model", "-n", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--scale", "-s", type=int, default=4, choices=[2, 3, 4], help="Output scale (default: 4)")
    parser.add_argument("--tile", "-t", type=int, default=None, help="Tile size, 0=auto (default: tool's default)")
    parser.add_argument("--format", "-f", default=None, help="Output format jpg/png/webp (default: from extension)")
    parser.add_argument("--compress", "-c", type=int, default=0, help="Output compression 0-100 (default: 0)")
    parser.add_argument("--force", action="store_true", help="Re-upscale even if the output already exists")
    parser.add_argument("--verbose", "-v", action="store_true", help="Stream upscayl-bin output")
    parser.add_argument("--list-models", action="store_true", help="List available models and exit")
    args = parser.parse_args()

    bin_path = resolve_bin()
    models_dir = resolve_models()
    available = list_models(models_dir)

    if args.list_models:
        print("Models in", models_dir)
        for m in available:
            print(" ", m)
        return 0

    if not args.input:
        parser.error("input is required (file or directory)")

    if not os.path.exists(bin_path):
        sys.exit(f"upscayl-bin not found at {bin_path}; set UPSCAYL_BIN env var")
    if not shutil.which(bin_path) and not os.access(bin_path, os.X_OK):
        sys.exit(f"upscayl-bin at {bin_path} is not executable")
    if args.model not in available:
        sys.exit(f"unknown model '{args.model}'. Available: {', '.join(available) or '(none found in ' + models_dir + ')'}")
    if not os.path.exists(args.input):
        sys.exit(f"input not found: {args.input}")

    is_dir = os.path.isdir(args.input)
    inputs = collect_inputs(args.input)
    if not inputs:
        print(f"No supported images ({', '.join(sorted(SUPPORTED_EXTS))}) under {args.input}", file=sys.stderr)
        return 0

    out_root = ""
    if is_dir:
        out_root = args.out or args.input.rstrip(os.sep) + "-upscaled"
        os.makedirs(out_root, exist_ok=True)

    ok = 0
    skipped = 0
    failed = 0
    for in_file in inputs:
        if is_dir:
            rel = os.path.relpath(in_file, args.input)
            stem, ext = os.path.splitext(rel)
            out_ext = ext if ext.lower() in SUPPORTED_EXTS else ".png"
            out_file = os.path.join(out_root, stem + out_ext)
            parent = os.path.dirname(out_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
        else:
            out_file = args.out or default_output_for(in_file, args.scale)

        if not args.force and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            skipped += 1
            continue

        if upscale_one(bin_path, models_dir, args.model, args.scale,
                       in_file, out_file, args.format, args.compress, args.tile, args.verbose):
            ok += 1
            print(f"  ✓ {os.path.basename(in_file)} → {out_file}")
        else:
            failed += 1

    print(f"Upscale complete: {ok} done, {skipped} skipped (existing), {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
