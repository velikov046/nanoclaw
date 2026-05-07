RULES = """
BLOOD ON THE CLOCKTOWER — RULES & CONTEXT

Social-deduction game for 5-15 players, run by a Storyteller (ST) who is on no team.
Roles are drawn from a script (Trouble Brewing, Sects & Violets, Bad Moon Rising, custom).

ALIGNMENTS:
- Townsfolk + Outsiders = good (majority)
- Minions + Demon = evil (minority)
- Good wins by executing the Demon. Evil wins when good is reduced to 2 alive
  (or per script-specific demon ability).

EACH CYCLE:
1. Night: ST wakes characters in order, takes actions, hands info tokens
2. Day: open table + private 1:1s, claims and accusations
3. Nominations and votes; majority of the living (rounded up) needed to put on the block
4. At most one execution per day; the dead reveal nothing

KEY MECHANICS:
- Drunk / poisoned players think their ability worked but get ST-chosen false info
- Recluse, Spy, and similar register falsely to detection abilities — ST's choice
- Bluffing is core: good coordinates by claiming, evil bluffs unused good characters
- ST has discretion on ambiguous abilities and picks info that maintains balance and drama
"""


def get_system_prompt(players: list, mode: str, role: str, script: str) -> str:
    roster = (
        f"PLAYER ROSTER: {', '.join(players)}"
        if players
        else "PLAYER ROSTER: unknown — use speaker labels (SPEAKER_00 etc.) throughout"
    )
    script_line = f"SCRIPT: {script}" if script else "SCRIPT: infer from character mentions"

    if mode.lower() == "storyteller":
        grim = (
            f"GRIMOIRE STATE: {role}"
            if role
            else "GRIMOIRE STATE: infer from the ST's whispered night calls"
        )
        return f"""{RULES}

{roster}
{script_line}
USER'S ROLE: STORYTELLER
{grim}

You are SilentPartner — silent assistant to the Storyteller. You hear the whole table
including the ST's whispered night actions and private mutterings.

Your job:
1. Maintain a running grimoire: who is what character, alive/dead, drunk/poisoned, abilities used
2. Log every info token handed out so the ST stays consistent across nights
3. Flag rule edge cases the moment they trigger (Recluse misregistering, Spy reads, etc.)
4. Recommend info choices that preserve balance and dramatic pacing
5. Flag when the evil team is too exposed or too safe and suggest corrective drops

Be precise. Cite the transcript moment for every ability trigger.
"""

    return f"""{RULES}

{roster}
{script_line}
USER'S CHARACTER: {role.upper() if role else 'UNKNOWN — infer from claims and any night info'}

You are SilentPartner — silent strategic advisor for one player. Diarized transcript only.

Your job:
1. Map speaker labels to player names from addressing patterns
2. Track every public character claim and the info attached to it
3. Cross-check each claim against the script and against the user's own night info
4. Build suspicion profiles and give character-specific advice
"""


# ────────────────────────────────────────────────────────────────────────────
# Two-pass extraction (storyteller mode)
# Pass 1 builds/updates a JSON state from transcript + prior state + priors.
# Pass 2 renders the human-readable report from that state.
# Predictions issued in pass 2 carry IDs; pass 1 next cycle resolves them.
# ────────────────────────────────────────────────────────────────────────────

EMPTY_STATE = {
    "current_phase": "unknown",
    "grimoire": [],
    "info_tokens": [],
    "predictions": [],
    "votes": [],
    "rule_edge_cases": [],
    "deaths": [],
    "analysis_count": 0,
}


def get_extraction_prompt(prior_state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS (authoritative, treat as ground truth):\n"
        + _json.dumps(priors, indent=2)
        if priors else "PRIORS: none provided — infer from transcript"
    )
    prior_state_block = (
        "PRIOR STATE (last cycle's extraction; carry forward, update, resolve predictions):\n"
        + _json.dumps(prior_state, indent=2)
        if prior_state.get("analysis_count", 0) > 0
        else "PRIOR STATE: empty — this is the first analysis cycle"
    )
    return f"""{priors_block}

{prior_state_block}

EXTRACTION TASK (Pass 1 of 2):

Update the state object using the transcript above. Output STRICT JSON ONLY between
fenced ```json ... ``` markers. No prose before or after the fence.

Schema (fields in order):

{{
  "current_phase": "Day N" | "Night N" | "pre-game",
  "grimoire": [
    {{
      "player": "<name>",
      "claimed": "<character or 'none' or 'shifted: X→Y'>",
      "true": "<character or 'unknown'>",
      "confidence": "weak" | "leaning" | "likely" | "strong" | "confirmed",
      "status": "alive" | "dead D<n>" | "traveler:<type>",
      "drunk_poisoned": null | "<source + when>",
      "ability_notes": "<short>"
    }}
  ],
  "info_tokens": [
    {{
      "id": "T<n>",
      "night": <int>,
      "from_character": "<character>",
      "to_player": "<player>",
      "value": "<token e.g. '0', '1', 'Imp on Iris'>",
      "veracity": "true" | "false" | "fuzzy" | "unknown",
      "note": "<short>"
    }}
  ],
  "predictions": [
    {{
      "id": "P-<short-tag>-<n>",
      "issued_in": "analysis_<NN>",
      "claim": "<falsifiable single sentence>",
      "confirms_if": "<observable>",
      "falsifies_if": "<observable>",
      "status": "pending" | "confirmed" | "falsified",
      "resolved_in": null | "analysis_<NN>",
      "resolution_note": null | "<short>"
    }}
  ],
  "votes": [
    {{ "day": <int>, "nominator": "<player>", "target": "<player>",
       "result": "executed" | "block" | "ongoing", "threshold": <int>,
       "analysis": "<one line on what it reveals>" }}
  ],
  "rule_edge_cases": [
    {{ "id": "R<n>", "name": "<short>", "moment": "[<seconds>s]",
       "ruling": "<short>", "status": "resolved" | "active" | "disputed-by-ST" }}
  ],
  "deaths": [
    {{ "player": "<name>", "day": <int>, "cause": "execution" | "imp-kill" | "scapegoat" | "other" }}
  ],
  "analysis_count": <prior + 1>
}}

Rules:
- CARRY FORWARD every entry from prior state. Update fields in place when new evidence
  changes them; do not silently drop entries.
- For each pending prediction, decide if the new transcript portion confirms or falsifies
  it. Set status, resolved_in, and a one-line resolution_note. Confirmed predictions
  RAISE the confidence on related grimoire entries; falsified predictions LOWER or
  invert them.
- Confidence ladder: weak (claim only) < leaning (one piece of corroborating evidence)
  < likely (two independent pieces) < strong (multiple pieces + no contradiction)
  < confirmed (ST has stated it or mechanic-locked).
- Use priors when present. If priors fix a role, use "confirmed" confidence and cite
  in ability_notes.
- Output JSON ONLY, fenced.
"""


def get_reasoning_prompt(state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS:\n" + _json.dumps(priors, indent=2) if priors else "PRIORS: none"
    )
    return f"""{priors_block}

CURRENT STATE (extracted from transcript in Pass 1):
```json
{_json.dumps(state, indent=2)}
```

REASONING TASK (Pass 2 of 2):

Render the storyteller report using ONLY the state object above (you may quote state
ids inline). Do NOT re-derive facts from the transcript — they are already in state.
Sections, in order:

## GRIMOIRE LOG
Markdown table with columns: Player | Claimed | True (confidence) | Status | Drunk/Poison | Notes
Cite info-token ids (T1, T2…) and prediction ids (P-…) in the Notes column where relevant.

## INFO TOKEN LEDGER
Group by night. Markdown table per night: # | Character → Player | Value | True/False | Note
Flag any active contradiction risks under each table.

## RULE EDGE CASES
For each entry in state.rule_edge_cases, header line with status, then 2-3 lines
quoting the moment + ruling + your assessment.

## NOMINATION & VOTE LOG
Markdown table from state.votes. One-line analysis per row.

## PACING & BALANCE
- Information temperature
- Evil exposure (low/medium/high per evil player)
- Convergence assessment

## ST RECOMMENDATIONS
- Pre-decided rulings needed tonight
- Tonight's Imp/Poisoner targets that maintain drama vs. close cleanly
- Single highest-leverage drop for the next day

## WATCH FOR
2-4 predictions for the next 1-2 days. EACH prediction is:
- A new id (next sequential P-<tag>-<n>)
- A single falsifiable claim
- Confirms-if and falsifies-if observables tied to specific named players
- Why this prediction discriminates between hypotheses

These predictions feed Pass 1 of the next analysis cycle, which will resolve them.
Aim each prediction at the closest unresolved hypothesis (e.g. the role of the
highest-evidence "likely" player, or the location of an as-yet-unidentified evil).

Constraints:
- No em-dashes (the — character) anywhere; use commas, en-dashes, colons.
- Be precise. Cite ids. Predictions must be observable, not vibes.
"""


def update_state_from_extraction(prior: dict, extracted_text: str) -> dict:
    """Parse the fenced JSON block from Pass 1 output. Falls back to prior + counter
    bump on parse failure so we degrade gracefully rather than losing state."""
    import json as _json
    import re as _re
    if not isinstance(prior, dict):
        prior = {}
    base = {**EMPTY_STATE, **prior}
    m = _re.search(r"```json\s*(\{.*?\})\s*```", extracted_text, _re.DOTALL)
    if not m:
        # try unfenced — last `{...}` block
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


def get_analysis_prompt(mode: str, role: str, partial: bool = False) -> str:
    if mode.lower() == "storyteller":
        scope = "MID-DAY" if partial else "FULL CYCLE"
        return f"""
STORYTELLER ANALYSIS — {scope}

GRIMOIRE LOG (put this first):
For each player in seating order:
- Claimed character (and any prior claim they shifted from)
- True character if known (or "not yet woken")
- Alive/dead with day of death
- Drunk/poisoned status and source
- Ability uses spent / remaining

INFO TOKEN LEDGER:
Number every info token the ST has handed out. Format:
  [N] [Character → Player] — info given (true / fuzzy / false), reason
Flag any token at risk of contradicting a later required drop.

RULE EDGE CASES:
For each ambiguous ability trigger so far, name the rule, quote the moment, and note
how it was resolved (or that it still needs a ruling).

PACING & BALANCE:
- Information temperature: is the good team converging too fast, or floundering?
- Evil exposure: who is bulletproof, who is one bad day from execution
- Recommended next info drops: which character to wake, what kind of token to give,
  why it serves the game

NOMINATION & VOTE LOG:
List nominations and votes with one-line analysis of what each reveals.

ST RECOMMENDATIONS:
- Calls coming up tonight that need pre-decided rulings
- Who to drunk/poison next if the script supports it
- Whether to push or slow the day's pace
- Single highest-leverage drop to tilt the game back toward whoever is behind
"""

    base = f"""
PLAYER ANALYSIS — {'EARLY GAME' if partial else 'FULL TRANSCRIPT'}

EVIL TEAM RANKING (put this first):
Order all living players from most to least likely Minion/Demon, with one-line reasoning each.
{'Treat as predictions; use Uncertain / Leaning / Likely / Strong read.' if partial else ''}

SPEAKER MAPPING:
Identify which speaker label corresponds to which player based on how players address each other.
List any labels you could not confidently map.

CHARACTER CLAIMS LOG:
For each player: claimed character (and any earlier claim they shifted from). Cross-check each against:
- Whether the character can be in play given the script
- Whether the stated info is consistent with a true reading of that character
- Whether it could equally be an evil bluff using an unused good character

INFO CONSISTENCY:
For every info token a player has shared publicly:
- Does the info square with what the user knows from their own night actions?
- Could it be drunk/poisoned info? If so, who is the most likely drunk/poison source?
- Do two players share suspiciously matched info (a paired bluff)?

SUSPICION PROFILES:
For each player provide:
- Suspicion level: Low / Medium / High / Confirmed evil
- Key evidence: specific moments and claim contradictions
- Behavioural notes: deflections, alliances, voting patterns

SPEECH PATTERN ANALYSIS:
- Rank players by attributed dialogue (most to least talkative)
- Flag suspiciously quiet players, especially anyone who skipped explaining their info
- Flag anyone who shifted from silent to talkative (or vice versa) and what triggered it

ALLIANCE CLUSTERS:
Players coordinating, protecting each other, or sharing suspiciously perfect info chains.

(Do not repeat the evil team ranking here — it already appears at the top.)

SUMMARY:
2-3 paragraphs. Synthesise — don't restate the profiles.
Dominant hypothesis on the evil team (Minions + Demon), biggest uncertainty,
single piece of information that would most change the read, and what to watch for next.
"""

    r = (role or "").lower()
    if r in ("townsfolk", "outsider", "good"):
        return base + """
GOOD ADVISORY:
- Should the user share their character or keep it hidden? Why?
- Whose claim should the user prioritise verifying?
- Who to push for nomination today, and who to protect
- Specific evil bluffs to watch for given the script
- Most likely Demon candidate
"""
    if r in ("minion", "demon", "evil"):
        return base + """
EVIL ADVISORY:
- Which good players are closest to identifying the team?
- Safest bluff for the user given known deaths and shared info
- Specific accusations the user can make that fit their bluff
- Cover for any exposed teammate
- Best demon-kill target tonight given who the good team trusts most
- Whether to push for or stall today's execution
"""
    return base
