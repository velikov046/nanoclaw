#!/usr/bin/env python3
"""
Mirror a group's knowledge/images/ tree into knowledge/images-upscaled/ via upscayl-bin.

Per-book directories under knowledge/images/<slug>/ are upscaled into
knowledge/images-upscaled/<slug>/. Idempotent: per-file skip when the output
already exists, so this can be re-run safely after each ingest.

Host-side companion to tools/upscale_image.py — the agent containers don't
have Vulkan/Mesa, so this lives outside the container loop. Run after a book
ingests, or schedule it on a cron.

Usage:
  python3 upscale_book_images.py                                  # all velikov books
  python3 upscale_book_images.py --book a_farewell_to_virology    # single book
  python3 upscale_book_images.py --group doctor
  python3 upscale_book_images.py --model digital-art-4x           # different model
  python3 upscale_book_images.py --dry-run                        # report-only

Exit codes: 0 = all books done or skipped, 1 = at least one book failed.
"""

import argparse
import os
import subprocess
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
NANOCLAW_ROOT = os.path.dirname(THIS_DIR)
GROUPS_ROOT = os.path.join(NANOCLAW_ROOT, "groups")
UPSCALE_TOOL = os.path.join(THIS_DIR, "upscale_image.py")
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def images_root_for(group):
    return os.path.join(GROUPS_ROOT, group, "researcher", "knowledge", "images")


def upscaled_root_for(group):
    return os.path.join(GROUPS_ROOT, group, "researcher", "knowledge", "images-upscaled")


def has_supported_files(d):
    for entry in os.listdir(d):
        if os.path.splitext(entry)[1].lower() in SUPPORTED_EXTS:
            return True
    return False


def list_books(images_root):
    if not os.path.isdir(images_root):
        return []
    books = []
    for name in sorted(os.listdir(images_root)):
        path = os.path.join(images_root, name)
        if os.path.isdir(path) and has_supported_files(path):
            books.append(name)
    return books


def count_pending(in_dir, out_dir):
    pending = 0
    total = 0
    for entry in os.listdir(in_dir):
        if os.path.splitext(entry)[1].lower() not in SUPPORTED_EXTS:
            continue
        total += 1
        out_path = os.path.join(out_dir, entry)
        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
            pending += 1
    return pending, total


def main():
    parser = argparse.ArgumentParser(description="Upscale a group's KB book images into a parallel images-upscaled/ tree")
    parser.add_argument("--group", default="velikov", help="Group name under groups/ (default: velikov)")
    parser.add_argument("--book", default=None, help="Single book slug (default: all books in the group)")
    parser.add_argument("--model", default="upscayl-standard-4x", help="Upscayl model (default: upscayl-standard-4x)")
    parser.add_argument("--scale", type=int, default=4, choices=[2, 3, 4], help="Output scale (default: 4)")
    parser.add_argument("--dry-run", action="store_true", help="List what would be upscaled and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="Stream upscayl-bin output")
    args = parser.parse_args()

    images_root = images_root_for(args.group)
    upscaled_root = upscaled_root_for(args.group)

    if not os.path.isdir(images_root):
        sys.exit(f"images dir not found for group '{args.group}': {images_root}")

    if args.book:
        if not os.path.isdir(os.path.join(images_root, args.book)):
            sys.exit(f"book not found: {images_root}/{args.book}")
        books = [args.book]
    else:
        books = list_books(images_root)

    if not books:
        print(f"No books with images under {images_root}")
        return 0

    os.makedirs(upscaled_root, exist_ok=True)

    plan = []
    for slug in books:
        in_dir = os.path.join(images_root, slug)
        out_dir = os.path.join(upscaled_root, slug)
        pending, total = count_pending(in_dir, out_dir if os.path.isdir(out_dir) else "/__nope__")
        plan.append((slug, in_dir, out_dir, pending, total))

    print(f"Group: {args.group}  ({len(books)} book{'s' if len(books) != 1 else ''})")
    print(f"  Source:  {images_root}")
    print(f"  Output:  {upscaled_root}")
    print(f"  Model:   {args.model} @ x{args.scale}")
    print()
    for slug, _, _, pending, total in plan:
        marker = "•" if pending else "✓"
        print(f"  {marker} {slug}: {pending}/{total} pending")
    print()

    if args.dry_run:
        return 0

    failed_books = []
    for slug, in_dir, out_dir, pending, total in plan:
        if pending == 0:
            continue
        print(f"→ {slug} ({pending}/{total})")
        cmd = [
            sys.executable, UPSCALE_TOOL, in_dir,
            "--out", out_dir,
            "--model", args.model,
            "--scale", str(args.scale),
        ]
        if args.verbose:
            cmd.append("--verbose")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            failed_books.append(slug)

    print()
    done_books = len(plan) - len(failed_books)
    print(f"Done: {done_books}/{len(plan)} books processed cleanly" + (f", failures: {', '.join(failed_books)}" if failed_books else ""))
    return 0 if not failed_books else 1


if __name__ == "__main__":
    sys.exit(main())
