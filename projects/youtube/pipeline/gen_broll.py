"""
gen_broll.py — Generate optional Kling AI opener and Pixabay B-roll for a job.

Usage:
  python3 gen_broll.py --job <job_dir> [--kling] [--broll] [--broll-count 3]

Reads: <job_dir>/script.json (title, thumbnail_prompt, tags)
Writes: <job_dir>/broll/opener.mp4, <job_dir>/broll/broll_*.mp4
Updates: script.json with kling_file and broll_files keys

Both flags are opt-in — neither runs unless you ask for it.
Skips silently if the relevant API key is missing.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request


def load_script(job_dir: str) -> dict:
    path = os.path.join(job_dir, "script.json")
    if not os.path.exists(path):
        print(f"ERROR: script.json not found in {job_dir}")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def save_script(job_dir: str, script: dict):
    path = os.path.join(job_dir, "script.json")
    with open(path, "w") as f:
        json.dump(script, f, indent=2)


# ---------------------------------------------------------------------------
# Kling — AI-generated opener clip
# ---------------------------------------------------------------------------

KLING_STYLE_PREFIX = (
    "Cinematic opening shot, 16:9, no text, no people, surreal and atmospheric. "
)


def build_kling_prompt(script: dict) -> str:
    base = script.get("thumbnail_prompt") or script.get("title", "")
    return KLING_STYLE_PREFIX + base


def generate_kling_clip(prompt: str, output_path: str, duration: int = 5) -> str | None:
    api_key = os.environ.get("KLING_API_KEY", "")
    if not api_key:
        print("  [Kling] No KLING_API_KEY — skipping opener.")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = json.dumps({
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": "16:9",
        "mode": "std",
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.klingai.com/v1/videos/text2video",
            data=payload, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())

        task_id = result.get("data", {}).get("task_id")
        if not task_id:
            print(f"  [Kling] No task_id in response: {result}")
            return None

        print(f"  [Kling] Job submitted (task {task_id}), polling...")

        for _ in range(36):  # up to 3 minutes
            time.sleep(5)
            poll = urllib.request.Request(
                f"https://api.klingai.com/v1/videos/text2video/{task_id}",
                headers=headers
            )
            with urllib.request.urlopen(poll, timeout=15) as r:
                status = json.loads(r.read())
            state = status.get("data", {}).get("task_status", "")
            if state == "succeed":
                video_url = status["data"]["task_result"]["videos"][0]["url"]
                urllib.request.urlretrieve(video_url, output_path)
                print(f"  [Kling] Opener downloaded: {output_path}")
                return output_path
            elif state == "failed":
                print("  [Kling] Generation failed.")
                return None

        print("  [Kling] Timed out.")
        return None

    except Exception as e:
        print(f"  [Kling] Error: {e}")
        return None


# ---------------------------------------------------------------------------
# Pixabay — B-roll clips matched to script concepts
# ---------------------------------------------------------------------------

def extract_concepts(script: dict) -> list[str]:
    # Prefer explicit tags; fall back to words from title
    tags = script.get("tags", [])
    candidates = [t for t in tags if len(t) > 4]
    if not candidates:
        title = script.get("title", "")
        candidates = [w for w in re.sub(r"[^\w\s]", "", title).split() if len(w) > 4]
    return candidates


def fetch_pixabay_clips(concepts: list[str], output_dir: str, max_clips: int = 3) -> list[str]:
    api_key = os.environ.get("PIXABAY_API_KEY", "")
    if not api_key:
        print("  [Pixabay] No PIXABAY_API_KEY — skipping B-roll.")
        return []

    os.makedirs(output_dir, exist_ok=True)
    downloaded = []

    for concept in concepts[:max_clips]:
        query = urllib.parse.urlencode({
            "key": api_key, "q": concept,
            "video_type": "film", "per_page": 3, "safesearch": "true",
        })
        url = f"https://pixabay.com/api/videos/?{query}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            hits = data.get("hits", [])
            if not hits:
                print(f"  [Pixabay] No results for: {concept}")
                continue
            video_url = hits[0]["videos"].get("medium", {}).get("url", "")
            if not video_url:
                continue
            safe_name = re.sub(r"[^\w]", "_", concept)[:30]
            out_path = os.path.join(output_dir, f"broll_{safe_name}.mp4")
            urllib.request.urlretrieve(video_url, out_path)
            downloaded.append(out_path)
            print(f"  [Pixabay] Downloaded: {concept} → {os.path.basename(out_path)}")
        except Exception as e:
            print(f"  [Pixabay] Failed for '{concept}': {e}")

    return downloaded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate Kling opener and Pixabay B-roll")
    parser.add_argument("--job", required=True, help="Job directory path")
    parser.add_argument("--kling", action="store_true", help="Generate Kling AI opener clip")
    parser.add_argument("--broll", action="store_true", help="Fetch Pixabay B-roll clips")
    parser.add_argument("--broll-count", type=int, default=3,
                        help="Max B-roll clips to fetch (default 3)")
    parser.add_argument("--kling-duration", type=int, default=5,
                        help="Kling opener duration in seconds (default 5)")
    args = parser.parse_args()

    if not args.kling and not args.broll:
        print("Nothing to do — pass --kling and/or --broll.")
        sys.exit(0)

    script = load_script(args.job)
    broll_dir = os.path.join(args.job, "broll")
    os.makedirs(broll_dir, exist_ok=True)

    if args.kling:
        prompt = build_kling_prompt(script)
        print(f"Kling prompt: {prompt[:100]}...")
        opener_path = os.path.join(broll_dir, "opener.mp4")
        result = generate_kling_clip(prompt, opener_path, duration=args.kling_duration)
        if result:
            script["kling_file"] = result

    if args.broll:
        concepts = extract_concepts(script)
        if not concepts:
            print("  [Pixabay] No concepts found in tags or title — skipping.")
        else:
            print(f"B-roll concepts: {concepts[:args.broll_count]}")
            clips = fetch_pixabay_clips(concepts, broll_dir, max_clips=args.broll_count)
            if clips:
                script["broll_files"] = clips

    save_script(args.job, script)
    print("\nscript.json updated.")


if __name__ == "__main__":
    main()
