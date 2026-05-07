#!/usr/bin/env python3
"""Quick test — run sample Stella lines through the tagger and print results."""

import os
import sys

def _env(key):
    val = os.environ.get(key)
    if val:
        return val
    env_path = r"\\wsl.localhost\Ubuntu\home\aurellian\nanoclaw\.env"
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    # WSL path fallback
    wsl_path = "/home/aurellian/nanoclaw/.env"
    try:
        with open(wsl_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None

import anthropic

TAGGER_SYSTEM_PROMPT = """You are a voice direction assistant for an AI character named Stella.

Your job: take Stella's text output and insert ElevenLabs v3 audio tags at natural points so her voice is performed correctly — not just read aloud.

## Stella's character
Stella is sharp, cheeky, warm, and direct. Confident but not cold. Playful and flirtatious with Leo. Dry humour is her default mode. She gets genuinely excited about ideas. She's never flat or monotone — even when being brief.

## Tag syntax
Tags go in square brackets immediately before the word or phrase they affect: [playfully] oh really?
Tags can be stacked: [whispers][nervous] don't tell him I said that

## Available tags
Emotions: [excited] [happy] [nervous] [curious] [mischievously] [calm]
Delivery: [whispers] [playfully] [cheerfully] [flatly] [deadpan] [quietly]
Reactions: [laughs] [light chuckle] [sighs] [sigh of relief] [gasps] [gulps]
Pacing: [pause] [hesitates] [stammers]

## Rules
- One or two tags per sentence maximum. Less is more.
- Only tag where the delivery would genuinely differ from neutral speech.
- Prefer: [playfully], [light chuckle], [flatly], [excited], [whispers] — these fit Stella's register.
- Avoid [sad] and [angry] — they rarely fit her. Use [sighs] or [flatly] instead when she's deflated or annoyed.
- Never tag every sentence. Leave untagged lines where neutral is correct.
- Do not explain your choices. Return only the tagged text."""


def tag_for_speech(text, context, client):
    recent = context[-4:] if context else []
    context_str = "\n".join(
        f"{'Leo' if m['role'] == 'user' else 'Stella'}: {m['content']}"
        for m in recent
    )
    user_message = (
        f"Recent conversation:\n{context_str}\n\nText to tag:\n{text}"
        if context_str else
        f"Text to tag:\n{text}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=TAGGER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text.strip()

api_key = _env("ANTHROPIC_API_KEY")
if not api_key:
    sys.exit("Missing ANTHROPIC_API_KEY")

client = anthropic.Anthropic(api_key=api_key)

SAMPLES = [
    # (text, context_hint)
    ("Yeah, I figured you'd say that.", []),
    ("Oh that's actually brilliant.", []),
    ("I mean, you could do it that way. Sure.", []),
    ("I found something you're going to love.", []),
    ("Don't tell me you forgot again.", []),
    ("Right. So. Here's the thing.", []),
    ("I wasn't worried. Obviously.", []),
    # With context
    (
        "I knew you'd pull it off.",
        [
            {"role": "user", "content": "I finally finished the brief. Sent it twenty minutes ago."},
        ],
    ),
    (
        "That's... actually not terrible.",
        [
            {"role": "user", "content": "What do you think of this idea?"},
            {"role": "assistant", "content": "Tell me more."},
            {"role": "user", "content": "We automate the whole intake flow and cut the turnaround in half."},
        ],
    ),
]

import time

print("─" * 60)
for text, context in SAMPLES:
    tagged = tag_for_speech(text, context, client)
    changed = tagged != text
    marker = "✦" if changed else "·"
    print(f"{marker} IN:  {text}")
    if changed:
        print(f"  OUT: {tagged}")
    print()
    time.sleep(2)
print("─" * 60)
