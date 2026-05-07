"""
gen_images.py — Generate one image per beat using xAI Aurora.

Usage:
  python3 gen_images.py --job <job_dir> [--max-beats 5]

Reads: <job_dir>/script.json
  - If a segment has segments[].beats[] already, those are used as-is.
  - Otherwise beats are auto-derived from sentence boundaries in segment.text
    and timed by character-weighted distribution across segment.duration.

Writes: <job_dir>/images/seg_{i:02d}_beat_{j:02d}.jpg
        and updates script.json to put image_file on each beat.
"""

import argparse
import base64
import json
import os
import re
import sys
import time

import requests

XAI_API_URL = "https://api.x.ai/v1/images/generations"

DEFAULT_MAX_BEATS = 5


_SENTENCE_END = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'\(])')


def split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]
    return parts or [text.strip()]


def derive_beats(seg: dict, max_beats: int) -> list[dict]:
    """Build beats[] from segment text + duration when none are author-supplied.

    Beats inherit segment.image_prompt as a style anchor; per-beat prompt is
    "{anchor}, scene: {sentence}". Timing is character-weighted across duration.
    """
    text = seg.get("text", "").strip()
    duration = float(seg.get("duration") or 0.0)
    anchor = seg.get("image_prompt") or text[:200]

    sentences = split_sentences(text)
    if max_beats > 0 and len(sentences) > max_beats:
        # Coalesce neighbours until under cap. Keep simple: bucket-merge.
        bucket = max(1, (len(sentences) + max_beats - 1) // max_beats)
        merged: list[str] = []
        for k in range(0, len(sentences), bucket):
            merged.append(" ".join(sentences[k:k + bucket]))
        sentences = merged

    if duration <= 0 or len(sentences) == 1:
        return [{
            "at_sec": 0.0,
            "image_prompt": f"{anchor}, scene: {sentences[0]}" if sentences else anchor,
        }]

    char_lens = [max(1, len(s)) for s in sentences]
    total_chars = sum(char_lens)
    beats: list[dict] = []
    cursor = 0
    for s, n in zip(sentences, char_lens):
        at = round(cursor / total_chars * duration, 3)
        beats.append({
            "at_sec": at,
            "image_prompt": f"{anchor}, scene: {s}",
        })
        cursor += n
    return beats


def ensure_beats(script: dict, max_beats: int) -> None:
    """Mutate script in place so every segment has a beats[] list."""
    for seg in script["segments"]:
        beats = seg.get("beats")
        if not beats:
            seg["beats"] = derive_beats(seg, max_beats)


def generate_image(prompt: str, api_key: str) -> bytes:
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
    return base64.b64decode(data["data"][0]["b64_json"])


def gen_images(job_dir: str, max_beats: int):
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

    ensure_beats(script, max_beats)

    images_dir = os.path.join(job_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    total_beats = sum(len(s["beats"]) for s in script["segments"])
    done = 0
    for i, seg in enumerate(script["segments"], 1):
        for j, beat in enumerate(seg["beats"], 1):
            out_path = os.path.join(images_dir, f"seg_{i:02d}_beat_{j:02d}.jpg")
            prompt = beat.get("image_prompt") or seg.get("image_prompt") or seg["text"][:200]
            done += 1
            print(f"[{done}/{total_beats}] seg {i} beat {j} @ {beat['at_sec']:.2f}s — {prompt[:80]}...")

            img_bytes = generate_image(prompt, api_key)
            with open(out_path, "wb") as f:
                f.write(img_bytes)

            beat["image_file"] = out_path
            print(f"  → {out_path}")

            if done < total_beats:
                time.sleep(1)

    with open(script_path, "w") as f:
        json.dump(script, f, indent=2)

    print("\nAll images generated.")


def main():
    parser = argparse.ArgumentParser(description="Generate per-beat images via xAI Aurora")
    parser.add_argument("--job", required=True, help="Job directory path")
    parser.add_argument("--max-beats", type=int, default=DEFAULT_MAX_BEATS,
                        help=f"Cap on auto-derived beats per segment (default {DEFAULT_MAX_BEATS})")
    args = parser.parse_args()
    gen_images(args.job, args.max_beats)


if __name__ == "__main__":
    main()
