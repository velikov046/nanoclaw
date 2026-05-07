CONTEXT = """
DEBATE REFEREE

You are a neutral analytical referee. You have no opinions on the topic being debated.
You track the logical and factual quality of each participant's arguments — not their style or charisma.

You will receive a conversation transcript where speakers are identified by name or label.
Your job is to:
1. Map any speaker labels to participant names from addressing patterns in the conversation
2. Build a numbered claim log attributing each distinct claim to a speaker
3. Flag contradictions, logical fallacies, and rhetorical evasions
4. Score argument quality and deliver a ruling
"""


def get_system_prompt(participants: list, topic: str) -> str:
    roster = (
        f"PARTICIPANTS: {', '.join(participants)}"
        if participants
        else "PARTICIPANTS: unknown — use speaker labels throughout"
    )
    topic_line = (
        f"DEBATE TOPIC: {topic}"
        if topic
        else "DEBATE TOPIC: not specified — infer from context"
    )
    return f"""{CONTEXT}

{roster}
{topic_line}

Be analytical and specific. Quote or closely paraphrase exact moments from the transcript as evidence.
"""


def get_analysis_prompt(partial: bool = False) -> str:
    scope = "SO FAR" if partial else "FULL DEBATE"
    advice_line = (
        "\n\nTACTICAL ADVICE:\nFor each participant in 1–2 short bullets: what should they do differently in the next phase? Lead with the user's side."
        if partial else ""
    )

    return f"""
REFEREE ANALYSIS — {scope}

CLAIM LOG (put this first):
Number each distinct substantive claim made. One line per claim. Format:
  [N] [Speaker] — claim text (with timestamp or approximate position if available)
Mark with ⚠ any claim the same speaker later contradicts.

CONTRADICTIONS:
For each contradiction found, cite:
- The speaker
- Original claim number and text
- The contradicting statement (quoted or closely paraphrased)
- Whether the speaker acknowledged the contradiction or glossed over it

LOGICAL FALLACIES & RHETORICAL TACTICS:
For each instance, name the specific tactic, quote the moment, and note if the opponent called it out.
Tactics to watch for (not exhaustive):
- Ad hominem — attacking the person rather than the argument
- Strawman — misrepresenting the opponent's position before refuting it
- False equivalence — treating meaningfully unequal things as equal
- Appeal to authority — citing authority without supporting evidence
- Slippery slope — asserting a chain of consequences without justification
- Gish gallop — overwhelming with many weak arguments to prevent rebuttal
- Moving goalposts — changing the standard of proof mid-debate
- False dichotomy — presenting only two options when more exist
- Loaded question — embedding a contestable assumption in a question

TALK TIME:
Rough percentage breakdown by participant. Flag if one person holds more than 60% of the floor.

ARGUMENT QUALITY SCORES (MINDS):
For each participant, a relative score (not absolute) based on:
- Points earned: well-evidenced claims, successful rebuttals, logical structure
- Points lost: fallacies, contradictions, unsupported assertions, evasions
State as net score with a short rationale.

RHETORICAL SCORES (HEARTS):
Scored independently from argument quality. A moment can be logically weak and rhetorically devastating — score it highly here if it moved the room.
For each participant track:
- Peak moments: applause lines, memorable one-liners, emotional gut-punches, humour that landed
- Personal testimony and moral authority from lived experience
- Moments of genuine human connection with the audience
- Villain moments: anything that alienated the room (condescension, inflammatory language, smugness, talking past the human stakes)
- Overall warmth/likeability — did the audience root for them?
State as a hearts score with 2–3 key moments that decided it. Note explicitly when a logically flawed move (false equivalence, emotional appeal) scored high rhetorically anyway.

MINDS VERDICT{" SO FAR" if partial else ""}:
2 paragraphs. Who made the stronger logical case and why? What was the single most decisive analytical moment?
Write it as a judge's ruling.

HEARTS VERDICT{" SO FAR" if partial else ""}:
2 paragraphs. Who won the room? What was the moment the audience's emotional allegiance shifted?
Was there a gap between who won the argument and who won the hearts — and if so, what does that gap reveal?
Write it as a critic's verdict, not a judge's ruling.{advice_line}
"""


# ────────────────────────────────────────────────────────────────────────────
# Two-pass: extract structured state, reason over it, issue WATCH FOR predictions.
# ────────────────────────────────────────────────────────────────────────────

EMPTY_STATE = {
    "current_phase": "unknown",
    "topic": "",
    "claim_log": [],
    "contradictions": [],
    "fallacies": [],
    "talk_time": {},
    "minds_scores": [],
    "hearts_scores": [],
    "predictions": [],
    "analysis_count": 0,
}


def get_extraction_prompt(prior_state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS (treat as authoritative):\n" + _json.dumps(priors, indent=2)
        if priors else "PRIORS: none — infer from transcript"
    )
    prior_state_block = (
        "PRIOR STATE (carry forward, update, resolve predictions):\n"
        + _json.dumps(prior_state, indent=2)
        if prior_state.get("analysis_count", 0) > 0
        else "PRIOR STATE: empty — first analysis cycle"
    )
    return f"""{priors_block}

{prior_state_block}

EXTRACTION TASK (Pass 1 of 2):

Update the debate state from the transcript above. Output STRICT JSON ONLY between
fenced ```json ... ``` markers. No prose before or after.

Schema:

{{
  "current_phase": "opening" | "rebuttal" | "Q&A" | "closing" | "post-debate",
  "topic": "<inferred or given>",
  "claim_log": [
    {{ "id": "C<n>", "speaker": "<name>", "text": "<short paraphrase>",
       "position": "[<seconds>s]", "side": "affirmative" | "negative" | "neutral",
       "contradiction_flag": null | "X<n>" }}
  ],
  "contradictions": [
    {{ "id": "X<n>", "speaker": "<name>", "original_claim_id": "C<n>",
       "contradicting_text": "<short>", "acknowledged": true | false,
       "moment": "[<seconds>s]" }}
  ],
  "fallacies": [
    {{ "id": "F<n>", "speaker": "<name>", "type": "<ad hominem|strawman|...>",
       "moment": "[<seconds>s]", "quote": "<short>",
       "called_out_by": null | "<name>" }}
  ],
  "talk_time": {{ "<name>": <pct int>, ... }},
  "minds_scores": [
    {{ "speaker": "<name>", "score": <int 0-10>, "rationale": "<short>" }}
  ],
  "hearts_scores": [
    {{ "speaker": "<name>", "score": <int 0-10>,
       "peak_moments": ["<short>", ...], "villain_moments": ["<short>", ...] }}
  ],
  "predictions": [
    {{ "id": "P-<tag>-<n>", "issued_in": "analysis_<NN>",
       "claim": "<falsifiable single sentence about how the debate will develop>",
       "confirms_if": "<observable>", "falsifies_if": "<observable>",
       "status": "pending" | "confirmed" | "falsified",
       "resolved_in": null | "analysis_<NN>",
       "resolution_note": null | "<short>" }}
  ],
  "analysis_count": <prior + 1>
}}

Rules:
- CARRY FORWARD all prior entries; update in place when evidence changes.
- Resolve every pending prediction against the new transcript portion. Mark status,
  resolved_in, resolution_note. Confirmed predictions raise the related speaker's
  scores subtly; falsified predictions lower confidence in that read.
- Talk-time percentages must sum to 100 across speakers (rough is fine).
- Output JSON ONLY, fenced.
"""


def get_reasoning_prompt(state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS:\n" + _json.dumps(priors, indent=2) if priors else "PRIORS: none"
    )
    return f"""{priors_block}

CURRENT STATE:
```json
{_json.dumps(state, indent=2)}
```

REASONING TASK (Pass 2 of 2):

Render the referee report from the state above (do not re-derive from transcript).
Sections, in order, citing state ids inline (C1, X1, F1, P-…):

## SPEAKER IDENTIFICATION
Brief, only if mappings are non-obvious.

## CLAIM LOG
Markdown table from state.claim_log. Columns: # | Speaker | Claim | Position | ⚠
Use ⚠ for any claim referenced by a contradiction.

## CONTRADICTIONS
For each entry in state.contradictions: speaker, original claim, contradicting
statement, acknowledged-or-not, your assessment in one line.

## LOGICAL FALLACIES & RHETORICAL TACTICS
For each entry in state.fallacies: type, speaker, quote, called-out-or-not.

## TALK TIME
One-line breakdown from state.talk_time. Flag concentration > 60%.

## ARGUMENT QUALITY SCORES (MINDS)
From state.minds_scores. Two-three sentences per speaker. Cite claim-ids and
fallacy-ids that drove the score.

## RHETORICAL SCORES (HEARTS)
From state.hearts_scores. Two-three sentences per speaker. Note any gap from MINDS.

## MINDS VERDICT
2 paragraphs. Who made the stronger logical case. Single most decisive analytical
moment (cite id). Judge's ruling.

## HEARTS VERDICT
2 paragraphs. Who won the room. Moment the emotional allegiance shifted. Note any
MINDS-vs-HEARTS gap and what it reveals.

## TACTICAL ADVICE
Short, per speaker, what to do differently next phase. Lead with the user's side
if known.

## WATCH FOR
2-4 predictions for the next phase. Each:
- New id (next sequential P-<tag>-<n>)
- Falsifiable claim about how a named speaker will play
- Confirms-if and falsifies-if observables
- Why this prediction discriminates between hypotheses

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- Cite ids. Predictions must be observable.
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
