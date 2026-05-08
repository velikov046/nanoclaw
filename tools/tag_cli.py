#!/usr/bin/env python3
"""
Emotion Tagger — CLI

Usage:
  echo "Your text here" | python tag_cli.py
  python tag_cli.py "Your text here"
  python tag_cli.py input.txt
  python tag_cli.py input.txt --char character.txt --context ctx.txt
  python tag_cli.py input.txt --out output.txt
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("anthropic package not found. Run: pip install anthropic")

REPO_ROOT = Path(__file__).resolve().parent.parent
GROUPS_DIR = REPO_ROOT / "groups"
AGENT_DIRS = {
    "lydia": "lydia-clone",
}

DEFAULT_CHARACTER = """Stella is sharp, cheeky, warm, and direct. Confident but not cold. Dry humour is her default. Never flat or monotone."""

TAGGER_SYSTEM = """You are a voice direction assistant.

Your job: take the provided text and insert ElevenLabs v3 audio tags at natural points so the \
voice is performed correctly, not just read aloud.

## Character profile
{character}

## Tag syntax
Tags go in square brackets immediately before the word or phrase they affect: `[playfully] oh really?`
Tags can be stacked: `[whispers][nervous] don't tell him I said that`

## Available tags
Emotions: [excited] [happy] [nervous] [curious] [mischievously] [calm]
Delivery: [whispers] [playfully] [cheerfully] [flatly] [deadpan] [quietly]
Reactions: [laughs] [light chuckle] [sighs] [sigh of relief] [gasps] [gulps]
Pacing: [pause] [hesitates] [stammers] [slowly]
Sensual/slow: [softly] [breathy] [warmly]

## Rhythm and pause breaks
For shaping rhythm, prefer measured SSML breaks over loose pacing tags:
- `<break time="0.3s" />`  micro-pause for mid-sentence beats
- `<break time="0.6s" />`  comma-weight pause between clauses
- `<break time="1.0s" />`  end-of-thought pause; lets the line land
- `<break time="1.5s" />`  weighty pause before a load-bearing claim or punchline
Use [pause] / [hesitates] for character-coloured stalling; use `<break>` for clean rhythmic timing where you want a specific dwell. Don't stack a [pause] tag and a `<break>` in the same gap.

## Punchline emphasis
Load-bearing punchlines — the rhetorical payoff of a contrast, a reveal, a shocking pivot — must hit harder than setup lines or the structural argument loses its weight. Common patterns to recognise:
- Negation + reveal: "It doesn't just open an inquiry — it opens a market."
- Rhetorical pivot: "Same outbreak, same protocol — different ending."
- Fragment punch: "Not coincidence. Pattern."
- Ellipsis + contrast: "They tested every passenger… and quietly discarded the results that didn't fit."

When you spot a punchline, apply ALL THREE:
1. Insert `<break time="1.0s" />` (or 1.5s if the setup is long) IMMEDIATELY BEFORE the punchline — give the line a moment to land into.
2. Tag the punchline itself with one of [dramatic], [emphatic], or [slowly] — pick the one that suits the character. NEVER stack two emphasis tags.
3. Insert `<break time="1.5s" />` (or 2.0s for a heavy reveal) IMMEDIATELY AFTER the punchline — let the line breathe before the next sentence steamrolls it. The post-pause is non-negotiable; without it the punchline is just another sentence in the run.

If the user content includes a `## Punchlines to emphasise` block, treat those exact substrings as definite punchlines and apply the full break-tag-break treatment without second-guessing.

## Rules
- One or two tags per sentence maximum. Less is more.
- Rhythm breaks are not subject to the one-or-two cap, but use them sparingly (1 to 3 across a paragraph).
- Only tag where the delivery would genuinely differ from neutral speech.
- Never tag every sentence. Leave untagged lines where neutral is correct.
- Respect the character's preferred and avoided tags listed in the profile.
- Do not explain your choices. Return only the tagged text."""


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


def _build_client(agent):
    """Set up OneCLI proxy for the given agent and return an Anthropic client.
    Falls back to direct api_key from .env if OneCLI is unreachable (legacy)."""
    from claude_oauth import make_client
    try:
        target = AGENT_DIRS.get(agent, agent) if agent else None
        return make_client(agent=target)
    except Exception as e:
        legacy_key = _env("ANTHROPIC_API_KEY")
        if legacy_key:
            return make_client(api_key=legacy_key)
        sys.exit(f"OneCLI proxy unavailable ({e}) and no ANTHROPIC_API_KEY in env")


def tag_text(text, character, context, agent=None, punchlines=None):
    system_text = TAGGER_SYSTEM.format(character=character.strip())
    user_parts = []
    if context.strip():
        user_parts.append(f"## Conversation context\n{context.strip()}\n")
    if punchlines:
        # Declarative punchlines override / supplement the heuristic detection
        # in the system prompt. Each line is a substring that MUST get the
        # dramatic break + emphasis tag treatment.
        lines = "\n".join(f"- {p}" for p in punchlines if p.strip())
        if lines:
            user_parts.append(f"## Punchlines to emphasise\n{lines}\n")
    user_parts.append(f"## Text to tag\n{text.strip()}")

    client = _build_client(agent)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
            },
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
    )
    return msg.content[0].text


def read_file_or_str(val):
    if val and os.path.exists(val):
        with open(val, encoding="utf-8") as f:
            return f.read()
    return val or ""


def load_agent_profile(agent):
    folder = AGENT_DIRS.get(agent, agent)
    p = GROUPS_DIR / folder / "voice_profile.md"
    if not p.exists():
        sys.exit(f"No voice profile at {p}")
    return p.read_text()


def main():
    parser = argparse.ArgumentParser(description="Insert ElevenLabs v3 emotion tags into text via Claude")
    parser.add_argument("input",   nargs="?",  help="Text string or path to input file (omit to read stdin)")
    parser.add_argument("--agent", default="", help="Agent name; loads groups/<agent>/voice_profile.md (e.g. stella, lydia, velikov, melody, aurelio)")
    parser.add_argument("--char",  default="", help="Character profile string or file (overridden by --agent if set)")
    parser.add_argument("--context", "--ctx", default="", help="Conversation context string or file (optional)")
    parser.add_argument("--out",   default="", help="Output file (default: print to stdout)")
    parser.add_argument(
        "--punchlines", action="append", default=[],
        help="Substring to mark as a punchline (gets <break time=\"1.0s\" /> "
             "+ [dramatic]/[emphatic]/[slowly] tag). Pass multiple times for "
             "multiple punchlines.",
    )
    args = parser.parse_args()

    # Input text
    if args.input:
        text = read_file_or_str(args.input)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(1)

    if not text.strip():
        sys.exit("Error: no input text.")

    if args.agent:
        character = load_agent_profile(args.agent)
    else:
        character = read_file_or_str(args.char) or DEFAULT_CHARACTER
    context   = read_file_or_str(args.context)

    print("Tagging…", file=sys.stderr)
    result = tag_text(text.strip(), character, context,
                      agent=args.agent or None,
                      punchlines=args.punchlines or None)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()
