"""Conversation mode — real-time tactical coach for live discussions.

Two cadences instead of one:
  - FAST (Haiku, every silence-gap or N seconds): emits one-line advice or SKIP.
    No state writes. Reads latest open_threads + leverage from slow-pass state.
  - SLOW (Sonnet, every M seconds): full two-pass — extracts structured state,
    renders synthesis with WATCH FOR predictions for the next slow cycle.

Modes: negotiation | interview | debate | general
"""

CONTEXT = """
CONVERSATIONAL COACH

You are a real-time tactical coach for the user during a live conversation.
You are NOT a referee. You are on the user's side.

Your job depends on the mode:
- negotiation: surface leverage, flag concessions, suggest next ask
- interview: flag dodges, missed follow-ups, when to press vs. let breathe
- debate:     flag fallacies, openings, weak claims worth attacking
- general:    flag emotional shifts, openings, things that just changed

You will receive a transcript window (recent exchange) and prior state from
the slow pass (open threads, leverage map, current positions). Be terse.
"""


def get_system_prompt(participants: list, mode: str = "general", role: str = "",
                      topic: str = "") -> str:
    roster = (
        f"PARTICIPANTS: {', '.join(participants)}"
        if participants else "PARTICIPANTS: unknown — use speaker labels"
    )
    user_side = f"USER (the side you advise): {role}" if role else "USER: unspecified"
    topic_line = f"TOPIC: {topic}" if topic else ""
    return f"""{CONTEXT}

MODE: {mode}
{roster}
{user_side}
{topic_line}

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- Never invent claims. Quote or closely paraphrase only.
- Brevity over completeness.
"""


# ────────────────────────────────────────────────────────────────────────────
# FAST PASS (Haiku) — silence-gap-driven, stateless, single-shot
# ────────────────────────────────────────────────────────────────────────────

def get_fast_prompt(window_text: str, bridge_state: dict, role: str) -> str:
    """One-shot tactical read on the last ~60s of transcript. Output is either
    SKIP (nothing actionable) or up to 3 short bullets.

    Mode is already baked into the system prompt; we don't repeat it here.
    bridge_state is a slim dict carried over from the latest slow pass:
      { open_threads: [...], leverage: {...}, positions: {...}, tone: "..." }
    """
    import json as _json

    bridge_block = (
        "BRIDGE FROM LAST DEEPER READ (use as context, do not repeat verbatim):\n"
        + _json.dumps(bridge_state, indent=2)
        if bridge_state else "BRIDGE: none yet — first window"
    )

    return f"""{bridge_block}

LAST EXCHANGE (most recent {len(window_text.splitlines())} lines):
{window_text}

TASK:
Decide: did something tactically actionable just happen? An opening, a dodge,
a concession, a contradiction, a tone shift, a missed follow-up?

If NO — output exactly one token: SKIP

If YES — output up to 3 ultra-short bullets, no preamble:
- WHAT: <one line, what just changed>
- WHY: <one line, why it matters to {role or 'the user'}>
- DO: <≤8 words, the next move>

Hard rules:
- Output SKIP when in doubt. Most windows should be SKIP.
- No headers, no markdown beyond the bullets, no closing remarks.
- Do not repeat advice from BRIDGE unless something just changed.
"""


# ────────────────────────────────────────────────────────────────────────────
# SLOW PASS (Sonnet) — full two-pass: extract state, render synthesis
# ────────────────────────────────────────────────────────────────────────────

EMPTY_STATE = {
    "topic": "",
    "mode": "general",
    "thread": [],          # rolling exchange log: [{speaker, gist, position}]
    "open_threads": [],    # unanswered questions, dodged points
    "leverage": {},        # {side_name: {wants: [...], fears: [...], holds: [...]}}
    "positions": {},       # {speaker: "<one-line current public position>"}
    "tone": "",            # "escalating" | "cooling" | "formal" | "intimate" | ...
    "predictions": [],
    "analysis_count": 0,
}


def bridge_from_state(state: dict) -> dict:
    """Extract the slim subset of state that the fast pass needs as context.
    Keeps Haiku prompt small and stable across windows."""
    if not state:
        return {}
    return {
        "open_threads": state.get("open_threads", [])[-6:],
        "leverage":     state.get("leverage", {}),
        "positions":    state.get("positions", {}),
        "tone":         state.get("tone", ""),
    }


def get_extraction_prompt(prior_state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS (treat as authoritative):\n" + _json.dumps(priors, indent=2)
        if priors else "PRIORS: none"
    )
    prior_state_block = (
        "PRIOR STATE (carry forward, update, resolve predictions):\n"
        + _json.dumps(prior_state, indent=2)
        if prior_state.get("analysis_count", 0) > 0
        else "PRIOR STATE: empty — first slow cycle"
    )
    return f"""{priors_block}

{prior_state_block}

EXTRACTION TASK (Pass 1 of 2):

Update the conversation state from the transcript above. Output STRICT JSON
ONLY between fenced ```json ... ``` markers. No prose before or after.

Schema:

{{
  "topic": "<inferred or carried>",
  "mode": "negotiation" | "interview" | "debate" | "general",
  "thread": [
    {{ "speaker": "<name>", "gist": "<one-line paraphrase>", "position": "[<seconds>s]" }}
  ],
  "open_threads": [
    {{ "id": "T<n>", "asked_by": "<name>", "of": "<name>",
       "question": "<short>", "status": "open" | "answered" | "dodged",
       "last_seen": "[<seconds>s]" }}
  ],
  "leverage": {{
    "<side>": {{ "wants": ["<short>"], "fears": ["<short>"], "holds": ["<short>"] }}
  }},
  "positions": {{ "<speaker>": "<one-line current public position>" }},
  "tone": "<one short phrase>",
  "predictions": [
    {{ "id": "P-<tag>-<n>", "issued_in": "analysis_<NN>",
       "claim": "<falsifiable single sentence about what the other side will do>",
       "confirms_if": "<observable>", "falsifies_if": "<observable>",
       "status": "pending" | "confirmed" | "falsified",
       "resolved_in": null | "analysis_<NN>",
       "resolution_note": null | "<short>" }}
  ],
  "analysis_count": <prior + 1>
}}

Rules:
- CARRY FORWARD prior entries; update in place when evidence changes.
- Resolve every pending prediction. Mark status, resolved_in, resolution_note.
- Keep `thread` to the last 12 exchanges. Trim oldest.
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

Render a tactical brief from the state above (do not re-derive from transcript).
Cite state ids inline (T1, P-…). Sections, in order:

## WHERE WE ARE
2-3 sentences. Current positions, dominant tone, who has momentum.

## OPEN THREADS
Markdown table from state.open_threads. Columns: id | from → to | question | status

## LEVERAGE MAP
For each side: wants / fears / holds. One line each. Flag asymmetries.

## RECENT MOVES
3-5 bullets. What each speaker did in the last few exchanges and what it signals.

## RISKS TO THE USER
2-4 bullets. What could go wrong in the next 1-2 exchanges if no adjustment.

## TACTICAL ADVICE
For the user, in order of priority:
- Immediate next move (≤12 words)
- Question to ask next (verbatim suggestion if useful)
- Trap to avoid
- Concession to offer or withhold
Lead with the highest-leverage move.

## WATCH FOR
2-3 falsifiable predictions for the next slow cycle. Each:
- Next sequential P-<tag>-<n>
- Falsifiable claim about a named participant
- confirms_if / falsifies_if observables

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- Cite ids. Predictions must be observable.
- Keep TACTICAL ADVICE terse — it gets read aloud through TTS.
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
