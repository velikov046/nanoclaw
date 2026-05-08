"""
produce_thumbnail.py — Two-stage thumbnail design pipeline.

The simple one-shot path (gen_thumbnail.py) takes script.thumbnail_prompt,
prepends a style preamble, and asks Aurora once. That's image gen — not
thumbnail design. A real production thumbnail needs:

  1. CONCEPT DESIGN via Sonnet — read the video context (title, hook,
     thesis) + per-channel art-direction rules, output N distinct
     thumbnail concept briefs that hit different angles.
  2. IMAGE GEN via Aurora — run each concept through grok.com/imagine,
     save to <job>/thumbnail-variants/. Don't auto-pick; leave for the
     human (or eventually a vision-model judge).

Reads from script.json:
  - title, thumbnail_prompt (hint), hook_script, segments[].text
  - character_reference (optional; constrains visual continuity)

Writes:
  <job_dir>/thumbnail-variants/v{n}_{slug}.jpg     N candidate images
  <job_dir>/thumbnail-variants/briefs.json          LLM-generated concepts

Per-channel art direction:
  Loaded from `groups/<agent>/thumbnail_brand.md` if present, otherwise
  the embedded fallback (Velikov has rules; Stella/Lydia get a generic
  preamble until their trees get captured).

Env:
  GROK_COOKIES_FILE   optional override for the Aurora cookies path.

Usage:
  python3 produce_thumbnail.py --job <job_dir> [--char-profile velikov]
                               [--count 4]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _aurora_via_grok import generate as aurora_generate

PIPELINE_DIR = Path(__file__).resolve().parent
NANOCLAW_ROOT = PIPELINE_DIR.parents[2]
GROUPS_DIR = NANOCLAW_ROOT / "groups"
TOOLS_DIR = NANOCLAW_ROOT / "tools"

DEFAULT_COUNT = 4

# Per-character art-direction trees. Live as fallback when the agent
# hasn't written their own `groups/<agent>/thumbnail_brand.md`.
# Velikov tree captured 2026-05-08 from Leo's review of v5_hazmat_doc.
EMBEDDED_TREES = {
    "velikov": """\
## Velikov channel — thumbnail art direction

**Subject register: PERSONAL.** No crowd shots. No multiple people. The viewer
is the only one looking. When a human appears, it's a SOLITARY investigator
figure. Otherwise lean on object / document / iconography.

**Ghost-ship rule (institutional-suppression videos):** the setting is empty
of crew/passengers/onlookers. Abandonment as visual rhetoric for "the truth
that nobody's left to tell."

**Iconography vocabulary:**
- Hazmat-suited solitary figure (the investigator)
- Redacted document with thick censorship bars (information suppression)
- Single warning light, red, one source (danger + isolation)
- Viral capsid / pathogen creature (the threat made literal)
- Empty institutional setting (ghost ship, abandoned lab, dim corridor)

**Palette:** cold blue dominant, single warm accent (red light, amber porthole,
orange flame). No fully warm thumbnails. No bright stock-photo whites.

**Composition:** subject centered or on rule-of-thirds; if a document or
artefact is shown, it is held FORWARD toward the camera. Long depth-of-field
with foggy background pushing infinity-feeling.

**Banned:** stock-photo cleanliness, multiple people, smiling subjects,
social-media-influencer brightness, cartoon styles, generic "news anchor"
framings.

**Tone words to lean into:** somber, ominous, conspiratorial, archival,
weathered, suppressed, leaked, reluctant truth.""",
    "stella": """\
## Stella channel — thumbnail art direction

Bold contrast, warm tones, sharp editorial photography aesthetic. Strong
geometry. Confident and glossy. The kind of image that stops a scroll. No
text overlays. Subject focal and direct. No faces unless deliberately
chosen for hook.""",
    "lydia": """\
## Lydia channel — thumbnail art direction

Soft contemplative aesthetic, natural light, painterly and melancholic.
Archival or botanical illustration register. Quiet and considered. Should
feel like something pressed between the pages of a book. No text overlays.""",
}


def load_art_direction(char_profile: str) -> str:
    """Return the per-channel rules. Per-agent file overrides embedded."""
    folder = char_profile  # could remap if agents have different folder names
    custom = GROUPS_DIR / folder / "thumbnail_brand.md"
    if custom.exists():
        return custom.read_text()
    return EMBEDDED_TREES.get(char_profile, EMBEDDED_TREES["velikov"])


def slug(name: str, max_len: int = 32) -> str:
    """Slugify a concept name for a filename."""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:max_len] or "untitled"


def build_concept_brief(script: dict, art_direction: str, count: int,
                        agent: str = "velikov") -> list[dict]:
    """Ask Sonnet for N concept briefs as a JSON list.

    Each concept = {"name": "<short label>", "prompt": "<full Aurora prompt>"}.
    `agent` selects which OneCLI identity the Sonnet call authenticates as.
    """
    sys.path.insert(0, str(TOOLS_DIR))
    from claude_oauth import make_client  # noqa: E402

    title = script.get("title", "")
    hook = script.get("hook_script") or ""
    thumbnail_hint = script.get("thumbnail_prompt") or ""
    # Pull thesis from first 2-3 segment texts (concise context, not the
    # whole 300-word script).
    thesis_excerpts = []
    for seg in (script.get("segments") or [])[:3]:
        t = (seg.get("text") or "").strip()
        if t:
            thesis_excerpts.append(t[:240])
    thesis = "\n\n".join(thesis_excerpts)

    system_text = f"""You are a YouTube thumbnail designer for an essay channel.

Your task: read the video context + the channel's art-direction rules, and \
output N distinct thumbnail concept prompts. Each concept should pull a \
different visual angle on the same video — not minor variations of one idea.

{art_direction}

## Output format
Return ONLY a JSON object with this shape:
{{
  "concepts": [
    {{"name": "<3-5 word slug>", "prompt": "<one-paragraph Aurora image-gen prompt>"}},
    ...
  ]
}}

Each prompt must:
- Apply the channel rules above (palette, register, banned elements)
- Be a single self-contained paragraph that an image-gen model can run
- Reference concrete visual elements (subject, lighting, setting, materials)
- NOT include text overlay instructions — text is added in post

No commentary. No explanations. JSON object only."""

    user_text = f"""## Video context

**Title:** {title}

**Hook line:** {hook or "(none)"}

**Thumbnail hint from script:** {thumbnail_hint or "(none)"}

**First few segment excerpts:**
{thesis or "(no segments)"}

## Task
Generate {count} distinct thumbnail concept briefs."""

    client = make_client(agent=agent)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[
            {"type": "text",
             "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": system_text,
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": user_text}],
    )
    raw = msg.content[0].text  # type: ignore[attr-defined]
    # Sonnet sometimes wraps JSON in code fences despite "JSON only" instructions.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Sonnet returned non-JSON brief output: {e}\n--- raw ---\n{raw[:500]}"
        ) from e
    concepts = data.get("concepts") or []
    if not isinstance(concepts, list) or not concepts:
        raise RuntimeError(f"Brief output had no concepts: {data}")
    return concepts[:count]


def main():
    ap = argparse.ArgumentParser(
        description="Two-stage thumbnail design pipeline (concept design + image gen)"
    )
    ap.add_argument("--job", required=True, help="Job directory path")
    ap.add_argument("--char-profile", default="velikov",
                    choices=list(EMBEDDED_TREES.keys()))
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT,
                    help=f"Number of concept variants to produce (default {DEFAULT_COUNT})")
    args = ap.parse_args()

    job = Path(args.job)
    script_path = job / "script.json"
    if not script_path.exists():
        print(f"ERROR: script.json not found in {job}", file=sys.stderr)
        sys.exit(1)
    script = json.loads(script_path.read_text())

    out_dir = job / "thumbnail-variants"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/2] designing {args.count} concept briefs via Sonnet...", file=sys.stderr)
    art_direction = load_art_direction(args.char_profile)
    concepts = build_concept_brief(script, art_direction, args.count,
                                    agent=args.char_profile)
    (out_dir / "briefs.json").write_text(
        json.dumps({"char_profile": args.char_profile, "concepts": concepts}, indent=2)
    )
    for i, c in enumerate(concepts, 1):
        name = c.get("name", f"concept_{i}")
        print(f"  {i}. {name} — {c.get('prompt','')[:90]}...", file=sys.stderr)

    print(f"[2/2] generating Aurora images...", file=sys.stderr)
    written = []
    for i, c in enumerate(concepts, 1):
        name = c.get("name", f"concept_{i}")
        prompt = c.get("prompt", "")
        if not prompt:
            print(f"  v{i} '{name}': empty prompt, skipping", file=sys.stderr)
            continue
        out_path = out_dir / f"v{i}_{slug(name)}.jpg"
        try:
            aurora_generate(prompt, out_path, mode="image", timeout_s=180)
        except Exception as e:
            print(f"  v{i} '{name}' failed: {e}", file=sys.stderr)
            continue
        written.append(str(out_path))
        print(f"  v{i} '{name}': {out_path.name} ({out_path.stat().st_size} bytes)",
              file=sys.stderr)

    if not written:
        print("ERROR: no candidates produced", file=sys.stderr)
        sys.exit(1)

    print(f"\nOK {len(written)} thumbnail candidates in {out_dir}")
    print("Review and pick one as <job>/thumbnail.jpg before running compose.")


if __name__ == "__main__":
    main()
