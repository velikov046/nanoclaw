#!/usr/bin/env python3
"""
Image Caption — CLI
Describes an image using Claude Vision.

Usage:
  python3 caption_image.py photo.jpg
  python3 caption_image.py photo.jpg --brief
  python3 caption_image.py photo.jpg --context "Leo sent this from work"
  python3 caption_image.py photo.jpg --out description.txt
"""

import argparse
import base64
import os
import sys

try:
    import anthropic
except ImportError:
    sys.exit("anthropic package not found. Run: pip install anthropic")

from claude_oauth import make_client

SYSTEM_FULL = "Describe this image naturally and directly. Cover what's in it, the mood or tone, any notable details. 2–4 sentences. No preamble like 'This image shows' — just describe it."
SYSTEM_BRIEF = "Describe this image in one sentence. Direct, no preamble."

MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    for path in [
        r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env",
        "/home/aurellian/nanoclaw/.env",
    ]:
        try:
            with open(path, errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            continue
    return None


def caption(image_path, brief=False, context=""):
    ext = os.path.splitext(image_path)[1].lower()
    media_type = MEDIA_TYPES.get(ext, "image/jpeg")

    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    user_content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": image_data},
        }
    ]
    if context.strip():
        user_content.append({"type": "text", "text": context.strip()})

    client = make_client(api_key=_env("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_BRIEF if brief else SYSTEM_FULL,
        messages=[{"role": "user", "content": user_content}],
    )
    return msg.content[0].text


def main():
    parser = argparse.ArgumentParser(description="Caption an image using Claude Vision")
    parser.add_argument("image", help="Image file path (jpg, png, gif, webp)")
    parser.add_argument("--brief", action="store_true", help="One-sentence caption")
    parser.add_argument("--context", default="", help="Optional context to include with the image")
    parser.add_argument("--out", default="", help="Output file (default: print to stdout)")
    args = parser.parse_args()

    if not os.path.exists(args.image):
        sys.exit(f"Error: file not found: {args.image}")

    result = caption(args.image, brief=args.brief, context=args.context)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
