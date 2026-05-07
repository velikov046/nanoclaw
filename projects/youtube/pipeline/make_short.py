"""
make_short.py — derive a short cut from an existing job's script.json.

Two modes:
  1. Author-flagged (preferred): segments with `"short": true` get assembled
     in declared order, capped at --max-duration.
  2. Prefix fallback: when no segments are flagged, take from the start of
     the script and accumulate until the cap is hit.

Original audio + per-beat images are referenced by path — no re-narration,
no re-image-gen, no file copies. Run compose.py against the output dir to
produce the actual short:

    python3 make_short.py --job <job> --max-duration 60          # TikTok
    python3 make_short.py --job <job> --max-duration 120 --out <job>/short_yt
    python3 compose.py   --job <job>/short --aspect vertical --char-profile velikov

Cap recommendations:
    60   TikTok / Instagram Reels (algorithm sweet spot)
    120  YouTube Shorts (cap is 3min, but ~2min keeps retention strong)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def select_segments(segments: list[dict], max_duration: float) -> tuple[list[dict], str]:
    """Pick segments under the duration cap. Returns (chosen, mode)."""
    flagged = [s for s in segments if s.get("short") is True]
    pool = flagged if flagged else segments
    mode = "highlights" if flagged else "prefix"

    chosen: list[dict] = []
    total = 0.0
    for s in pool:
        d = float(s.get("duration") or 0.0)
        # Always keep the first pick even if it alone overshoots —
        # otherwise an over-cap short returns empty, which is worse.
        if chosen and total + d > max_duration:
            break
        chosen.append(s)
        total += d

    return chosen, mode


def build_short_script(src_script: dict, chosen: list[dict],
                        src_job: str, mode: str, max_duration: float) -> dict:
    """Build the derivative script.json. Preserves all per-segment fields
    (text, tagged_text, audio_file, words, beats with image_file). Renumbers
    ids from 1. Drops `short` flag on copied segments since the derivative
    is the short itself."""
    out_segments = []
    for new_id, seg in enumerate(chosen, 1):
        copy = {k: v for k, v in seg.items() if k != "short"}
        copy["id"] = new_id
        out_segments.append(copy)

    return {
        **{k: v for k, v in src_script.items() if k != "segments"},
        "segments": out_segments,
        "_short_meta": {
            "source_job": os.path.abspath(src_job),
            "mode": mode,                       # "highlights" or "prefix"
            "max_duration": max_duration,
            "total_duration": round(sum(float(s.get("duration") or 0) for s in chosen), 3),
            "segment_count": len(chosen),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def make_short(src_job: str, out_dir: str, max_duration: float) -> None:
    src_script_path = os.path.join(src_job, "script.json")
    if not os.path.exists(src_script_path):
        sys.exit(f"ERROR: script.json not found in {src_job}")
    with open(src_script_path) as f:
        src_script = json.load(f)

    segments = src_script.get("segments") or []
    if not segments:
        sys.exit("ERROR: source script has no segments")

    chosen, mode = select_segments(segments, max_duration)
    if not chosen:
        sys.exit("ERROR: no segments selected (check segment durations / flags)")

    short_script = build_short_script(src_script, chosen, src_job, mode, max_duration)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "script.json")
    with open(out_path, "w") as f:
        json.dump(short_script, f, indent=2)

    meta = short_script["_short_meta"]
    print(f"Mode:          {mode}")
    print(f"Cap:           {max_duration:.1f}s")
    print(f"Selected:      {meta['segment_count']} of {len(segments)} segments "
          f"(total {meta['total_duration']:.1f}s)")
    print(f"Output:        {out_path}")
    print(f"\nNext: compose.py --job {out_dir} --aspect vertical --char-profile <char>")


def main():
    p = argparse.ArgumentParser(description="Derive a short cut from an existing job's script.json")
    p.add_argument("--job", required=True, help="Source job dir (with script.json)")
    p.add_argument("--out", default=None,
                   help="Output dir for derivative script.json (default: <job>/short)")
    p.add_argument("--max-duration", type=float, default=60.0,
                   help="Duration cap in seconds. 60 = TikTok / Reels, 120 = YouTube Shorts (default 60)")
    args = p.parse_args()

    out_dir = args.out or os.path.join(args.job, "short")
    make_short(args.job, out_dir, args.max_duration)


if __name__ == "__main__":
    main()
