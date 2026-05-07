"""
caption_utils.py — Tag-aware caption rendering for the video pipeline.

Pure helpers, no I/O beyond writing the .ass file. Consumed by compose.py.

Inputs come from narrate.py:
  seg.tagged_text   — raw tagger output, e.g. "[whispers] The mountain rose. [excited] Then it fell."
  seg.words         — [{text, start, end}, ...] from ElevenLabs normalized_alignment

What this module does:
  1. annotate_words_with_tags(words, tagged_text)
       Returns words enriched with: tags[] (persistent tags active during the word),
       cue (a reaction tag like "laughs" that fires before the word, or None),
       pause_before (bool: a [pause] preceded the word).
  2. build_ass_file(seg, char_profile, segment_offset_sec, out_path)
       Emits an ASS subtitle file timed to segment-local seconds + offset.
       Char profile drives the base style; tags drive inline overrides.
"""

from __future__ import annotations

import re
from typing import Iterable

# v3 tag categories (mirrors tag_cli.py)
PERSISTENT_TAGS = {
    "excited", "happy", "nervous", "curious", "mischievously", "calm",
    "whispers", "playfully", "cheerfully", "flatly", "deadpan", "quietly",
    "slowly", "softly", "breathy", "warmly",
}
REACTION_TAGS = {
    "laughs", "light chuckle", "sighs", "sigh of relief", "gasps", "gulps",
}
PACING_TAGS = {"pause", "hesitates", "stammers"}

_TAG_RE = re.compile(r"\[([^\[\]]+)\]")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?$")
_WORD_RE = re.compile(r"\S+")


def annotate_words_with_tags(words: list[dict], tagged_text: str) -> list[dict]:
    """Map each spoken word to the tag context that surrounds it in tagged_text.

    Persistent tags (delivery / emotion / pace-feel) attach to subsequent words
    until a sentence boundary clears them. Reaction tags ([laughs] etc.) attach
    to the next word as a one-off cue. Pacing tags ([pause]/[hesitates]) attach
    as pause_before flags. Unknown bracket tokens are ignored.
    """
    if not words:
        return []
    if not tagged_text:
        return [dict(w, tags=[], cue=None, pause_before=False) for w in words]

    # Walk tagged_text in source order, accumulating per-word attributes.
    active: list[str] = []
    pending_cue: str | None = None
    pending_pause = False
    word_attrs: list[dict] = []

    cursor = 0
    for m in _TAG_RE.finditer(tagged_text):
        # Process plain text up to this tag
        if m.start() > cursor:
            chunk = tagged_text[cursor:m.start()]
            for wm in _WORD_RE.finditer(chunk):
                word_attrs.append({
                    "tags": list(active),
                    "cue": pending_cue,
                    "pause_before": pending_pause,
                })
                pending_cue = None
                pending_pause = False
                if _SENTENCE_END.search(wm.group(0)):
                    active = []
        # Apply the tag
        tag = m.group(1).strip().lower()
        if tag in PERSISTENT_TAGS:
            if tag not in active:
                active.append(tag)
        elif tag in REACTION_TAGS:
            pending_cue = tag
        elif tag in PACING_TAGS:
            pending_pause = True
        # unknown → silently ignored (forward-compatible with new tags)
        cursor = m.end()

    # Trailing text after the last tag
    if cursor < len(tagged_text):
        chunk = tagged_text[cursor:]
        for wm in _WORD_RE.finditer(chunk):
            word_attrs.append({
                "tags": list(active),
                "cue": pending_cue,
                "pause_before": pending_pause,
            })
            pending_cue = None
            pending_pause = False
            if _SENTENCE_END.search(wm.group(0)):
                active = []

    # Zip with alignment words. If counts differ (normalization expanded numbers
    # or abbreviations into extra words), fall back to empty attrs for the tail.
    out: list[dict] = []
    for i, w in enumerate(words):
        if i < len(word_attrs):
            out.append({**w, **word_attrs[i]})
        else:
            out.append({**w, "tags": [], "cue": None, "pause_before": False})
    return out


# ---------- ASS subtitle rendering ----------

# Per-character base styling. ASS format docs: https://en.wikipedia.org/wiki/SubStation_Alpha
# Values: Fontname, Fontsize, PrimaryColour (BGR hex), Bold, Italic, Outline width, Shadow, Alignment (2=bottom)
CHAR_STYLES: dict[str, dict] = {
    "velikov": {
        "fontname": "DejaVu Serif",
        "fontsize": 56,
        "primary": "&H00FFFFFF",   # white
        "outline_colour": "&H00000000",  # black box
        "bold": 0,
        "italic": 0,
        "outline": 3,
        "shadow": 0,
        "border_style": 1,
    },
    "stella": {
        "fontname": "DejaVu Sans",
        "fontsize": 60,
        "primary": "&H0000FFFF",   # yellow
        "outline_colour": "&H00000000",
        "bold": 1,
        "italic": 0,
        "outline": 3,
        "shadow": 1,
        "border_style": 1,
    },
    "lydia": {
        "fontname": "DejaVu Serif",
        "fontsize": 52,
        "primary": "&H00F0F0F0",
        "outline_colour": "&H00000000",
        "bold": 0,
        "italic": 1,
        "outline": 2,
        "shadow": 0,
        "border_style": 1,
    },
}

# Tag-driven inline overrides applied via ASS \fs \i \b override codes per word
TAG_INLINE_OVERRIDES: dict[str, str] = {
    "whispers":  r"{\i1\fs44}",   # smaller italic
    "softly":    r"{\i1\fs44}",
    "breathy":   r"{\i1\fs46}",
    "slowly":    r"{\fs50}",
    "quietly":   r"{\fs48}",
    "warmly":    r"{\i1}",
    "excited":   r"{\b1\fs68}",
    "happy":     r"{\fs64}",
    "nervous":   r"{\i1}",
    "mischievously": r"{\i1}",
    "curious":   r"{}",
    "calm":      r"{}",
    "playfully": r"{\i1}",
    "cheerfully": r"{\b1}",
    "flatly":    r"{}",
    "deadpan":   r"{}",
}

WORDS_PER_CAPTION = 5  # group spoken words into short captions


def _format_ts(seconds: float) -> str:
    """ASS timestamp: H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_header(style: dict, aspect: str = "landscape") -> str:
    """Header tuned to the target aspect.

    Landscape: bottom-center captions (alignment 2) with a small bottom margin —
    standard YouTube long-form positioning.

    Vertical: middle-center (alignment 5) with no margin — keeps captions clear
    of TikTok/Shorts UI overlays at the top (~120px) and bottom (~250px).
    """
    if aspect == "vertical":
        play_x, play_y = 1080, 1920
        alignment, margin_v = 5, 0
        margin_l = margin_r = 60
    else:
        play_x, play_y = 1920, 1080
        alignment, margin_v = 2, 90
        margin_l = margin_r = 80

    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_x}\n"
        f"PlayResY: {play_y}\n"
        "Collisions: Normal\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style['fontname']},{style['fontsize']},{style['primary']},"
        f"{style['outline_colour']},&H00000000,{style['bold']},{style['italic']},0,0,"
        f"100,100,0,0,{style['border_style']},{style['outline']},{style['shadow']},"
        f"{alignment},{margin_l},{margin_r},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _word_with_overrides(w: dict) -> str:
    """Render a single word with ASS inline overrides driven by its tags."""
    text = w.get("text", "")
    tags = w.get("tags", []) or []
    if not tags:
        return text
    overrides = []
    for t in tags:
        ov = TAG_INLINE_OVERRIDES.get(t)
        if ov and ov != "{}":
            overrides.append(ov.strip("{}"))
    if not overrides:
        return text
    return "{" + "".join(overrides) + "}" + text + r"{\r}"


def build_ass_file(
    seg: dict,
    char_profile: str,
    segment_offset_sec: float,
    out_path: str,
    aspect: str = "landscape",
) -> str | None:
    """Write an ASS file for one segment. Returns out_path or None if no captions.

    Times are in absolute video-clock seconds, so callers can pass segment_offset_sec
    if the .ass is to be applied to a concatenated final video. For per-segment burn-in
    pass 0.0 — captions then use segment-local time.

    aspect controls subtitle placement: landscape → bottom-center, vertical →
    middle-center (clear of TikTok/Shorts UI bands).
    """
    annotated = annotate_words_with_tags(seg.get("words") or [], seg.get("tagged_text", ""))
    if not annotated:
        return None

    style = CHAR_STYLES.get(char_profile, CHAR_STYLES["velikov"])
    header = _ass_header(style, aspect)
    events: list[str] = []

    i = 0
    while i < len(annotated):
        # Emit a leading reaction cue as its own short event right before the next word
        cue = annotated[i].get("cue")
        if cue:
            cue_start = annotated[i]["start"] + segment_offset_sec
            cue_end = min(cue_start + 0.7, annotated[i]["end"] + segment_offset_sec)
            events.append(
                f"Dialogue: 0,{_format_ts(cue_start)},{_format_ts(cue_end)},Default,,0,0,0,,"
                f"{{\\i1\\fs40}}({cue}){{\\r}}"
            )

        # Group up to WORDS_PER_CAPTION words into one caption phrase, but stop
        # at a sentence boundary or before a word with a different cue or pause.
        group = [annotated[i]]
        j = i + 1
        while j < min(len(annotated), i + WORDS_PER_CAPTION):
            w = annotated[j]
            if w.get("cue") or w.get("pause_before"):
                break
            group.append(w)
            if _SENTENCE_END.search(w["text"]):
                j += 1
                break
            j += 1

        start = group[0]["start"] + segment_offset_sec
        end = group[-1]["end"] + segment_offset_sec
        text = " ".join(_word_with_overrides(w) for w in group)
        events.append(
            f"Dialogue: 0,{_format_ts(start)},{_format_ts(end)},Default,,0,0,0,,{text}"
        )
        i = j

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events) + "\n")
    return out_path


def first_word_starts(words: Iterable[dict]) -> list[float]:
    return [float(w["start"]) for w in words]
