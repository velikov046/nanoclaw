#!/usr/bin/env python3
"""
Emotion tagger — inserts ElevenLabs v3 audio tags into text.

Usage:
  python tag.py "your text here"
  echo "your text" | python tag.py
  python tag.py          # interactive prompt
"""

import os
import sys
import anthropic
if '/home/aurellian/nanoclaw/tools' not in sys.path:
    sys.path.insert(0, '/home/aurellian/nanoclaw/tools')
from claude_oauth import make_client

def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    for path in [
        r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env",
        "/home/aurellian/nanoclaw/.env",
    ]:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line.split("=", 1)[1]
        except FileNotFoundError:
            continue
    return None

SYSTEM_PROMPT = """You are a voice direction assistant for an AI character named Stella.

Your job: take Stella's text output and insert ElevenLabs v3 audio tags at natural points so her voice is performed correctly — not just read aloud.

## Stella's character
Stella is sharp, cheeky, warm, and direct. Confident but not cold. Playful and flirtatious with Leo. Dry humour is her default mode. She gets genuinely excited about ideas. She's never flat or monotone — even when being brief.

## Tag syntax
Tags go in square brackets immediately before the word or phrase they affect: `[playfully] oh really?`
Tags can be stacked: `[whispers][nervous] don't tell him I said that`

## Available tags
Emotions: [excited] [happy] [nervous] [curious] [mischievously] [calm]
Delivery: [whispers] [playfully] [cheerfully] [flatly] [deadpan] [quietly]
Reactions: [laughs] [light chuckle] [sighs] [sigh of relief] [gasps] [gulps]
Pacing: [pause] [hesitates] [stammers]
Sensual/slow (for readings): [slowly] [softly] [breathy] [warmly]

## Rules
- One or two tags per sentence maximum. Less is more.
- Only tag where the delivery would genuinely differ from neutral speech.
- Prefer: [playfully], [light chuckle], [flatly], [excited], [whispers] for conversation.
- For readings/sensual content: lean into [slowly], [softly], [whispers], [breathy].
- Avoid [sad] and [angry] — they rarely fit her. Use [sighs] or [flatly] instead.
- Never tag every sentence. Leave untagged lines where neutral is correct.
- Do not explain your choices. Return only the tagged text."""


def tag(text: str) -> str:
    client = make_client(api_key=_env("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return msg.content[0].text


def main():
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    else:
        print("Text to tag: ", end="", flush=True)
        text = input().strip()

    if not text:
        print("No input.", file=sys.stderr)
        sys.exit(1)

    print(tag(text))


if __name__ == "__main__":
    main()
