#!/usr/bin/env python3
"""
Mood Analyser — CLI (rule-based, no API required)

Classifies the emotional tone of text and returns a mood label,
intensity score, and best matching ElevenLabs v3 tag.

Usage:
  echo "text" | python3 mood_cli.py
  python3 mood_cli.py "text here"
  python3 mood_cli.py input.txt
  python3 mood_cli.py "text" --format tag    # ElevenLabs tag only
  python3 mood_cli.py "text" --format mood   # label only
"""

import argparse
import json
import os
import re
import sys

# keyword sets → (mood, elevenlabs_tag)
MOODS = [
    ("excited", "[excited]", [
        r"\bexcited?\b", r"\bamazing\b", r"\bwow\b", r"\byes!\b", r"\byesss\b",
        r"\bcan't wait\b", r"\bcannot wait\b", r"\bincredible\b", r"\bfantastic\b",
        r"\bbrilliant\b", r"\bawesome\b", r"\blet's go\b", r"\blets go\b",
        r"\!\s*\!\s*\!", r"\bfinally\b",
    ]),
    ("happy", "[happy]", [
        r"\bhappy\b", r"\bglad\b", r"\bdelighted\b", r"\bpleased\b", r"\bjoy\b",
        r"\bjoyful\b", r"\blovely\b", r"\bwonderful\b", r"\bsunny\b", r"\bsmiling\b",
        r"\bso good\b", r"\bthank you\b", r"\bthanks\b", r"\bgrateful\b",
        r"\blove it\b", r"\bperfect\b",
    ]),
    ("mischievous", "[mischievously]", [
        r"\bmischiev", r"\bcheeky\b", r"\bnaughty\b", r"\boh really\b",
        r"\bis that so\b", r"\boh?\b", r"\breally now\b", r"\bwell well\b",
        r"\bdare\b", r"\bbet\b", r"\bwe'll see\b", r"\binteresting\b",
        r"\bsmirk", r"\b😏\b", r"\b😈\b", r"\b😜\b",
    ]),
    ("playful", "[playfully]", [
        r"\bplayful\b", r"\bteasing\b", r"\bwink\b", r"\b😄\b", r"\b😁\b",
        r"\bhaha\b", r"\blol\b", r"\bha\b", r"\bfun\b", r"\bjoke\b",
        r"\bkidding\b", r"\bjust saying\b", r"\bcheeky\b", r"\blighten up\b",
        r"\bdon't tell\b", r"\bssh\b",
    ]),
    ("warm", "[cheerfully]", [
        r"\bwarm\b", r"\bsweet\b", r"\bkind\b", r"\bcare\b", r"\bcaring\b",
        r"\bhug\b", r"\bhere for you\b", r"\bi'm here\b", r"\bdon't worry\b",
        r"\bit's okay\b", r"\byou've got this\b", r"\bproud of you\b",
        r"\bmiss you\b", r"\b❤\b", r"\b🤗\b",
    ]),
    ("curious", "[curious]", [
        r"\bcurious\b", r"\bwonder\b", r"\bwhat if\b", r"\bhow does\b",
        r"\bwhy does\b", r"\binteresting\b", r"\btell me\b", r"\bexplain\b",
        r"\bi'd love to know\b", r"\bfascinating\b", r"\bwait —", r"\bwait,",
        r"\bhold on\b",
    ]),
    ("calm", "[calm]", [
        r"\bcalm\b", r"\brelax\b", r"\bsteady\b", r"\bno rush\b",
        r"\btake your time\b", r"\bit's fine\b", r"\ball good\b",
        r"\bno worries\b", r"\bbreath\b", r"\bpeace\b", r"\bquiet\b",
        r"\bsettled\b",
    ]),
    ("nervous", "[nervous]", [
        r"\bnervous\b", r"\banxious\b", r"\bworried\b", r"\bscared\b",
        r"\buneasy\b", r"\bnot sure\b", r"\bi hope\b", r"\bfinger\b",
        r"\bwhat if\b.*\bbad\b", r"\boh god\b", r"\boh no\b",
        r"\bplease\b.*\bwork\b", r"\bgulps\b", r"\buh oh\b",
    ]),
    ("flat", "[flatly]", [
        r"\bfine\b", r"\bwhatever\b", r"\bsure\b", r"\bok\b", r"\bokay\b",
        r"\bright\b", r"\bif you say so\b", r"\bgreat\.\s*$", r"\bnoted\b",
        r"\bunderstood\b", r"\bas you wish\b", r"\byes\.\s*$",
    ]),
    ("sad", "[sighs]", [
        r"\bsad\b", r"\bunhappy\b", r"\bdisappoint\b", r"\bsorry\b",
        r"\bwish\b", r"\bmiss\b", r"\balone\b", r"\btired\b",
        r"\bsigh\b", r"\b😔\b", r"\b😞\b", r"\b😢\b", r"\balas\b",
        r"\bunfortunately\b",
    ]),
]

# Non-verbal reactions that can override
REACTIONS = [
    (r"\b(laughs?|haha|lmao|😂|🤣)\b", "playful", "[laughs]"),
    (r"\b(chuckles?|snorts?|😄)\b", "playful", "[light chuckle]"),
    (r"\b(sighs?|ugh|😮‍💨)\b", "flat", "[sighs]"),
    (r"\b(gasps?|oh!\s*my|what!\s*really|no!\s*way|😱)\b", "excited", "[gasps]"),
    (r"\b(stammers?|um+|er+|uh+|i[- ]i[- ]i)\b", "nervous", "[stammers]"),
    (r"\b(hesitates?|pauses?|\.\.\.)\b", "flat", "[hesitates]"),
    (r"\b(whispers?|quietly|sotto voce)\b", "calm", "[whispers]"),
]


def score(text: str) -> dict:
    text_lower = text.lower()
    scores: dict[str, int] = {m[0]: 0 for m in MOODS}

    for mood, _tag, patterns in MOODS:
        for p in patterns:
            if re.search(p, text_lower):
                scores[mood] += 1

    # Check non-verbal reactions first (they take priority)
    for pattern, mood, tag in REACTIONS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            total = sum(scores.values()) or 1
            intensity = min(1.0, round(scores.get(mood, 1) / total + 0.3, 2))
            secondary = max(
                (m for m in scores if m != mood),
                key=lambda m: scores[m],
                default=None,
            )
            return {
                "mood": mood,
                "intensity": intensity,
                "secondary": secondary if scores.get(secondary, 0) > 0 else None,
                "elevenlabs_tag": tag,
            }

    total = sum(scores.values())
    if total == 0:
        return {"mood": "calm", "intensity": 0.3, "secondary": None, "elevenlabs_tag": ""}

    primary = max(scores, key=lambda m: scores[m])
    primary_count = scores[primary]

    # Find secondary (different mood with hits)
    remaining = {m: s for m, s in scores.items() if m != primary and s > 0}
    secondary = max(remaining, key=lambda m: remaining[m]) if remaining else None

    intensity = min(1.0, round(primary_count / total + 0.2, 2))

    tag = next(t for m, t, _ in MOODS if m == primary)

    return {
        "mood": primary,
        "intensity": intensity,
        "secondary": secondary,
        "elevenlabs_tag": tag,
    }


def read_file_or_str(val: str) -> str:
    if val and os.path.exists(val):
        with open(val, encoding="utf-8") as f:
            return f.read()
    return val or ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse emotional mood of text (no API required)")
    parser.add_argument("input", nargs="?", help="Text string or file path (omit to read stdin)")
    parser.add_argument(
        "--format",
        choices=["json", "tag", "mood"],
        default="json",
        help="Output: json (default), tag (ElevenLabs tag only), mood (label only)",
    )
    args = parser.parse_args()

    if args.input:
        text = read_file_or_str(args.input)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    if not text.strip():
        sys.exit("Error: no input text.")

    result = score(text)

    if args.format == "tag":
        print(result["elevenlabs_tag"])
    elif args.format == "mood":
        print(result["mood"])
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
