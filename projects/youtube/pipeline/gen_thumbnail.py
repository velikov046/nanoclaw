"""
gen_thumbnail.py — Generate a YouTube thumbnail via xAI Aurora.

Usage:
  python3 gen_thumbnail.py --job <job_dir> [--char-profile velikov|stella|lydia]

Reads: <job_dir>/script.json (thumbnail_prompt field)
Writes: <job_dir>/thumbnail.jpg
"""

import argparse
import base64
import json
import os
import sys

import requests

XAI_API_URL = "https://api.x.ai/v1/images/generations"

THUMBNAIL_STYLES = {
    "velikov": (
        "Dark cinematic YouTube thumbnail, dramatic lighting, high contrast, "
        "esoteric and conspiratorial aesthetic, no text overlays. "
        "Ultra detailed, striking visual composition. "
    ),
    "stella": (
        "Clean modern YouTube thumbnail, bold contrast, warm tones, "
        "sharp editorial photography aesthetic, strong geometry, confident and glossy. "
        "No text overlays. The kind of image that stops a scroll. "
    ),
    "lydia": (
        "Soft contemplative YouTube thumbnail, natural light, painterly and melancholic, "
        "archival or botanical illustration aesthetic, quiet and considered. "
        "No text overlays. Should feel like something found pressed between the pages of a book. "
    ),
}


def gen_thumbnail(job_dir: str, char_profile: str = "velikov"):
    script_path = os.path.join(job_dir, "script.json")
    if not os.path.exists(script_path):
        print(f"ERROR: script.json not found in {job_dir}")
        sys.exit(1)

    with open(script_path) as f:
        script = json.load(f)

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("ERROR: XAI_API_KEY not set")
        sys.exit(1)

    style = THUMBNAIL_STYLES.get(char_profile, THUMBNAIL_STYLES["velikov"])
    prompt = style + script.get("thumbnail_prompt", script.get("title", ""))
    print(f"Generating thumbnail [{char_profile}]: {prompt[:100]}...")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "aurora",
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }
    resp = requests.post(XAI_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    img_bytes = base64.b64decode(data["data"][0]["b64_json"])

    out_path = os.path.join(job_dir, "thumbnail.jpg")
    with open(out_path, "wb") as f:
        f.write(img_bytes)

    script["thumbnail_file"] = out_path
    with open(script_path, "w") as f:
        json.dump(script, f, indent=2)

    print(f"Thumbnail saved: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate thumbnail via xAI Aurora")
    parser.add_argument("--job", required=True, help="Job directory path")
    parser.add_argument(
        "--char-profile",
        default="velikov",
        choices=list(THUMBNAIL_STYLES.keys()),
        help="Character aesthetic profile for thumbnail style",
    )
    args = parser.parse_args()
    gen_thumbnail(args.job, args.char_profile)


if __name__ == "__main__":
    main()
