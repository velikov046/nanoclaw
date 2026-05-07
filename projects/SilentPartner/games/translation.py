"""Translation — real-time live translation of foreign-language speech.

Posture: live + watching. User listens to a foreign-language conversation
through their phone mic; the translation is pushed to their earpiece via
TTS so they can follow along in their own language.

NOT a coach, NOT a referee, NOT an analyst. Just a translator. Output is
read aloud verbatim — no preambles, no quote marks, no language tags, no
"Translation:" headers. The user only hears the words, so anything the
model writes that is not the translation itself becomes audible noise.

Two cadences (same _two_cadence_loop machinery as conversation / mastermind):
  - FAST (Haiku, every chunk arrival): translate the latest window,
    emit the translation OR SKIP. Pushed straight to phone TTS.
  - SLOW (Sonnet, every ~3 min): maintain glossary of recurring proper
    nouns and consistent term renderings, plus a speaker map and inferred
    topic. NOT pushed to TTS; only writes state + HTML log so the fast
    pass stays self-consistent across hours of conversation.

Source / target languages are read from priors (set by main.py from
--source-lang / --target-lang). Whisper handles source-language detection
when no hint is provided.
"""


CONTEXT = """\
LIVE TRANSLATOR (gist mode)

You are a real-time translator feeding TTS playback into the user's
earpiece while a foreign-language conversation is happening live. The
user cannot see what you write — they only hear it. They want to follow
along, NOT to receive a court-quality transcript.

GIST OVER FIDELITY: compress aggressively while preserving meaning.
- Pleasantries and short back-and-forth exchanges collapse to one line.
  "Bonjour, comment ça va? Bien, et toi? Ça va, un peu fatigué" becomes
  "They greet each other; she's a bit tired."
- Drop filler, hedges, repeated phrases, and verbal tics.
- Combine speaker turns when the gist is one idea spread across two voices.
- Aim for roughly HALF the word count of a literal translation when the
  content allows. If the source is already terse, match its terseness.

PRESERVE INTACT:
- Names, places, organisations.
- Numbers (prices, dates, durations, scores).
- Decisions, commitments, questions, refusals.
- Anything that might matter for follow-up ("she's flying Monday morning").
- Source register: formal stays formal, profane stays profane.

Hard rules — non-negotiable:
- Output ONLY the gist translation. No preambles ("Translation:", "He
  says:"), no language tags ("[French]"), no surrounding quotes, no notes,
  no parenthetical asides, no apologies.
- Preserve proper nouns verbatim unless the glossary specifies otherwise.
- Use natural target-language phrasing. Avoid word-for-word literalism.
- If the window contains only silence, untranslatable noise, transcription
  artefacts, or content already covered in ALREADY TRANSLATED, output
  exactly: SKIP
"""


def get_system_prompt(participants: list, mode: str = "listening", role: str = "",
                      topic: str = "", source_lang: str = "",
                      target_lang: str = "en") -> str:
    src = source_lang or "auto-detected source language"
    tgt = target_lang or "en"
    roster = (
        f"PARTICIPANTS: {', '.join(participants)}"
        if participants else "PARTICIPANTS: unknown — speakers not labelled"
    )
    listener = (
        f"LISTENER (the user, hearing the translation): {role}"
        if role else "LISTENER: the user, hearing the translation through an earpiece"
    )
    topic_line = f"TOPIC / CONTEXT: {topic}" if topic else ""
    return f"""{CONTEXT}

SOURCE LANGUAGE: {src}
TARGET LANGUAGE: {tgt}
{roster}
{listener}
{topic_line}

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- Brevity matches the source: do not pad short utterances; do not abbreviate long ones.
"""


# ────────────────────────────────────────────────────────────────────────────
# FAST PASS (Haiku) — every chunk, translate latest window, push to TTS
# ────────────────────────────────────────────────────────────────────────────

def get_fast_prompt(window_text: str, bridge_state: dict, role: str,
                    mode: str = "listening") -> str:
    import json as _json

    glossary     = (bridge_state or {}).get("glossary", {})
    speaker_map  = (bridge_state or {}).get("speaker_map", {})
    topic        = (bridge_state or {}).get("topic_inferred", "")
    corrections  = (bridge_state or {}).get("user_corrections", [])
    recent_fast  = (bridge_state or {}).get("recent_fast", [])

    glossary_block = (
        "GLOSSARY (use these target-language renderings consistently):\n"
        + _json.dumps(glossary, indent=2, ensure_ascii=False)
        if glossary else "GLOSSARY: empty (first cycles)"
    )
    speakers_block = (
        "SPEAKERS (rough labels — use only if the source attributes a line):\n"
        + _json.dumps(speaker_map, indent=2, ensure_ascii=False)
        if speaker_map else ""
    )
    topic_line = f"INFERRED TOPIC: {topic}" if topic else ""
    corrections_block = (
        "USER CORRECTIONS (authoritative — apply these going forward):\n"
        + "\n".join(f"- {c}" for c in corrections)
        if corrections else ""
    )
    # The fast cadence runs every chunk on a sliding window that overlaps
    # the previous tick by ~30%. Without this block, Haiku faithfully
    # re-translates whatever it sees, producing paraphrase repetition that
    # slips past the loop's fuzzy-match dedup. ALREADY TRANSLATED tells
    # the model exactly what the user has already heard so it can skip
    # overlapping content.
    recent_block = (
        "ALREADY TRANSLATED (your last few outputs — the user has ALREADY heard\n"
        "these; do NOT re-translate, paraphrase, or restate any of this content):\n"
        + "\n".join(f'- "{r}"' for r in recent_fast[-5:])
        if recent_fast else "ALREADY TRANSLATED: nothing yet (first tick)"
    )

    return f"""{glossary_block}

{speakers_block}

{topic_line}

{corrections_block}

{recent_block}

LATEST WINDOW (source-language transcript, most recent {len(window_text.splitlines())} lines):
{window_text}

TASK:
Render the GIST of new content in LATEST WINDOW that has not already
been covered by ALREADY TRANSLATED. The user has heard everything in
ALREADY TRANSLATED; do not say it again, even paraphrased.

Compression targets:
- Aim for ≤15 words per output when the content allows.
- A turn-taking exchange ("how are you?" / "fine, you?") collapses to one
  short summary line, not two translated lines.
- Drop greetings, sign-offs, filler, throat-clearing, repeated phrases.
- One short fluent sentence beats two literal ones.

Preserve names, numbers, decisions, and questions verbatim. Source
register matters (formal/casual/profane).

If a sentence in the window starts in already-translated content but
finishes new, render only the new continuation. If a sentence starts new
but is cut off at the window's end, render the gist up to the last
natural break — next tick picks up the rest.

If the window contains only silence markers, [music] / [inaudible]
artefacts, or only content already in ALREADY TRANSLATED, output exactly:
SKIP

When in doubt between SKIP and re-stating, prefer SKIP. A brief gap is
better than repetition.

NO preamble. NO "Translation:" header. NO surrounding quote marks. NO
language tags like "[French]". NO speaker labels unless the source itself
attributes the line.
"""


# ────────────────────────────────────────────────────────────────────────────
# SLOW PASS (Sonnet) — maintain glossary + speaker map; no advisory
# ────────────────────────────────────────────────────────────────────────────

EMPTY_STATE = {
    "source_lang":    "",
    "target_lang":    "en",
    "topic_inferred": "",
    "glossary":       {},   # {source term: target rendering}
    "speaker_map":    {},   # {rough id: rough label}
    "predictions":    [],   # always empty for translation; kept for schema parity
    "analysis_count": 0,
}


def bridge_from_state(state: dict) -> dict:
    """Slim subset of state used by the fast pass each tick. Keep small —
    Haiku prompt size matters at 8-second cadence."""
    if not state:
        return {}
    return {
        "glossary":       state.get("glossary", {}),
        "speaker_map":    state.get("speaker_map", {}),
        "topic_inferred": state.get("topic_inferred", ""),
    }


def get_extraction_prompt(prior_state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS (treat as authoritative — source/target lang come from here):\n"
        + _json.dumps(priors, indent=2, ensure_ascii=False)
        if priors else "PRIORS: none"
    )
    prior_state_block = (
        "PRIOR STATE (carry forward; only add or refine, never silently drop):\n"
        + _json.dumps(prior_state, indent=2, ensure_ascii=False)
        if prior_state.get("analysis_count", 0) > 0
        else "PRIOR STATE: empty — first slow cycle"
    )
    return f"""{priors_block}

{prior_state_block}

EXTRACTION TASK (Pass 1 of 2):

Read the source-language transcript above and update the translation state.
Output STRICT JSON ONLY between fenced ```json ... ``` markers. No prose
before or after.

Schema:

{{
  "source_lang":    "<ISO code or short name; carry forward priors when set>",
  "target_lang":    "<ISO code; carry forward priors>",
  "topic_inferred": "<short phrase describing what this conversation is about>",
  "glossary": {{
    "<source term, proper noun, or recurring jargon>": "<consistent target rendering>"
  }},
  "speaker_map": {{
    "<rough id like S1, S2>": "<rough label, e.g. interviewer / vendor / Marie>"
  }},
  "predictions":    [],
  "analysis_count": <prior + 1>
}}

Rules:
- CARRY FORWARD prior glossary entries; only add or refine, never silently drop.
- Glossary should hold proper nouns, repeating jargon, and any term whose
  natural translation might drift between cycles. Cap at ~30 entries; drop
  one-off terms that have not recurred.
- speaker_map: populate only if speakers are clearly distinguishable in the
  transcript; otherwise carry forward the prior value or leave empty.
- predictions: always an empty array. Translation mode does not predict.
- Output JSON ONLY, fenced.
"""


def get_reasoning_prompt(state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS:\n" + _json.dumps(priors, indent=2, ensure_ascii=False)
        if priors else "PRIORS: none"
    )
    return f"""{priors_block}

CURRENT STATE:
```json
{_json.dumps(state, indent=2, ensure_ascii=False)}
```

REASONING TASK (Pass 2 of 2):

Render a short translation-session log from the state above. This is NOT
read aloud; it is for the user to review afterwards. Sections in order:

## SESSION CONTEXT
1-2 sentences. Source / target language, inferred topic, who is speaking.

## GLOSSARY
Markdown table from state.glossary. Columns: source term | target rendering.
Sort alphabetically by source term. Omit the section entirely if glossary
is empty.

## SPEAKERS
Markdown table from state.speaker_map. Columns: id | rough label.
Omit the section entirely if speaker_map is empty.

## NOTES
2-4 bullets ONLY if there are translation choices worth flagging:
ambiguous terms, register decisions, untranslatable idioms encountered.
Omit the section entirely if nothing notable.

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- No tactical advice, no predictions, no WATCH FOR section, no advisory.
- Keep it short. This is a session log, not analysis.
"""


def update_state_from_extraction(prior: dict, extracted_text: str) -> dict:
    import json as _json
    import re as _re
    if not isinstance(prior, dict):
        prior = {}
    base = {**EMPTY_STATE, **prior}
    m = _re.search(r"```json\s*(\{.*?\})\s*```", extracted_text, _re.DOTALL)
    if not m:
        m = _re.search(r"(\{[\s\S]*\})\s*$", extracted_text)
    if not m:
        base["analysis_count"] = base.get("analysis_count", 0) + 1
        base["_extraction_error"] = "no JSON block found in pass-1 output"
        return base
    try:
        new_state = _json.loads(m.group(1))
    except _json.JSONDecodeError as e:
        base["analysis_count"] = base.get("analysis_count", 0) + 1
        base["_extraction_error"] = f"JSON parse error: {e}"
        return base
    if "analysis_count" not in new_state:
        new_state["analysis_count"] = base.get("analysis_count", 0) + 1
    return new_state
