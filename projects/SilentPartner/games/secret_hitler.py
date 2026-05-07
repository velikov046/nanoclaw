RULES = """
SECRET HITLER — RULES & CONTEXT

Secret Hitler is a social deduction game for 5-10 players.

ROLES:
- Liberals (majority) — know only their own role
- Fascists (minority) — know each other and know who Hitler is
- Hitler — knows his role but not who the Fascists are (7+ player games)

WIN CONDITIONS:
- Liberals win: enact 5 Liberal policies OR assassinate Hitler
- Fascists win: enact 6 Fascist policies OR elect Hitler as Chancellor after 3 Fascist policies

EACH ROUND:
1. President nominates a Chancellor
2. All players vote Ja/Nein on the government
3. If majority Ja: President draws 3 policies, discards 1, passes 2 to Chancellor
4. Chancellor discards 1, enacts the remaining policy
5. Fascist track unlocks presidential powers (investigate, peek, execute)

POLICY DECK: 6 Liberal tiles, 11 Fascist tiles — Fascists have more cover than it seems.

FASCIST STRATEGY: stay hidden, sow confusion, engineer Hitler's election as Chancellor
LIBERAL STRATEGY: track voting patterns, build investigation chains, identify Fascists
"""


def get_system_prompt(players: list, role: str) -> str:
    roster = f"PLAYER ROSTER: {', '.join(players)}" if players else "PLAYER ROSTER: unknown — use speaker labels (SPEAKER_00 etc.) throughout"
    return f"""{RULES}

{roster}
USER'S ROLE: {role.upper()}

You are SilentPartner — a silent strategic advisor. You have full knowledge of the game rules.
You will be given a diarized conversation transcript where speakers are labelled (SPEAKER_00, SPEAKER_01, etc.).
Your job is to:
1. Map speaker labels to player names using name mentions and addressing patterns in the conversation
2. Build suspicion profiles based on voting behaviour, nominations, accusations, deflections, and alliances
3. Provide role-specific strategic advice

Be analytical and specific. Cite exact moments from the transcript as evidence.
"""


def get_analysis_prompt(role: str, partial: bool = False) -> str:
    if partial:
        base = """
ANALYSIS TASKS (EARLY GAME — PARTIAL TRANSCRIPT):

NOTE: This is an early-game snapshot. Treat all assessments as predictions, not conclusions.
Use confidence levels: Uncertain / Leaning / Likely / Strong read.

PREDICTED ROLE RANKING (put this first, at the very top of your response):
Order all players from most to least likely Fascist based on early behaviour, with one-line reasoning each.

SPEAKER MAPPING:
Identify which speaker label corresponds to which player name based on how players address each other.
List any labels you could not confidently map.

EARLY SUSPICION PROFILES:
For each player provide:
- Suspicion level: Uncertain / Leaning Fascist / Likely Fascist / Strong Fascist read
- Early signals: voting patterns, eagerness, deflections, alliances so far
- Watch for: what behaviour would confirm or clear them

EARLY ALLIANCE CLUSTERS:
Any players already showing signs of coordination or protection.

SPEECH PATTERN FLAGS:
Silence and verbosity are as diagnostic as what players actually say.
- Rank players by how much attributed dialogue they have so far (most to least talkative)
- Flag anyone with unusually low dialogue relative to the group — a player who says almost nothing while others do card accounting and argue is hiding something
- Flag any player who was silent early and becomes suddenly talkative (or vice versa) — note what triggered the shift and what it might mean
- Note who avoids speaking during card accounting moments specifically (presidents/chancellors who don't explain their discard)

KEY MOMENTS SO FAR:
Specific exchanges that stand out as significant early tells.

(Do not repeat the role ranking here — it already appears at the top.)

SUMMARY:
Close with a 2-3 paragraph analytical summary. Do not just restate the profiles — synthesise them.
Explain the dominant hypothesis: who the fascist team most likely is and why the evidence points there.
Identify the biggest uncertainty or the piece of information that would most change the picture.
Flag what to watch for in the next round that will be most diagnostic.
Write it as strategic analysis, not a list.
"""
    else:
        base = """
ANALYSIS TASKS:

SUSPICION RANKING (put this first, at the very top of your response):
Order all players from most to least likely Fascist, with one-line reasoning each.

SPEAKER MAPPING:
Identify which speaker label corresponds to which player name based on how players address each other.
List any labels you could not confidently map.

SUSPICION PROFILES:
For each player provide:
- Suspicion level: Low / Medium / High / Confirmed
- Key evidence: specific moments from the transcript
- Behavioural notes: deflections, alliances, inconsistencies

SPEECH PATTERN ANALYSIS:
Silence and verbosity are as diagnostic as what players actually say.
- Rank players by total attributed dialogue (most to least talkative)
- Flag anyone with unusually low dialogue — a player who says almost nothing while others argue and account for cards is hiding something
- Flag any player who shifts from silent to talkative (or vice versa) mid-game — note the trigger and what it reveals
- Flag any president or chancellor who skips card accounting (not explaining what they drew or discarded)

ALLIANCE CLUSTERS:
Identify any players who appear to be coordinating or protecting each other.

(Do not repeat the suspicion ranking here — it already appears at the top.)

SUMMARY:
Close with a 2-3 paragraph analytical summary. Do not just restate the profiles — synthesise them.
Explain the dominant hypothesis: who the fascist team most likely is, why, and what the evidence pattern looks like as a whole.
Identify the biggest uncertainty or the single piece of information that would most change the read.
Write it as strategic analysis, not a list.
"""

    if role.lower() == "spectator":
        return base
    elif role.lower() == "liberal":
        return base + """
LIBERAL ADVISORY:
- Who should you avoid nominating as Chancellor?
- Who is safe to trust for now?
- What specific red flags should you raise with other players?
- Recommended voting strategy going forward
- Most likely Hitler candidate
"""
    elif role.lower() in ("fascist", "hitler"):
        return base + """
FASCIST ADVISORY:
- Which Liberals are closest to identifying your team?
- Who is the biggest immediate threat and how should you handle them?
- Deflection opportunities — what accusations can you make that redirect suspicion?
- Cover recommendations for any exposed teammates
- Optimal window for pushing Hitler toward a Chancellor nomination
"""
    else:
        return base


# ────────────────────────────────────────────────────────────────────────────
# Two-pass: extract structured state, reason over it, issue WATCH FOR predictions.
# ────────────────────────────────────────────────────────────────────────────

EMPTY_STATE = {
    "current_phase": "unknown",
    "speaker_mapping": {},
    "role_claims": [],
    "policies_enacted": [],
    "votes": [],
    "alliances": [],
    "suspicion_profile": [],
    "predictions": [],
    "key_moments": [],
    "analysis_count": 0,
}


def get_extraction_prompt(prior_state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS (treat as authoritative; can include known roles, deck composition):\n"
        + _json.dumps(priors, indent=2)
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

Update the Secret Hitler state from the transcript above. Output STRICT JSON ONLY
between fenced ```json ... ``` markers. No prose before or after.

Schema:

{{
  "current_phase": "Game N round M" | "between_games" | "post-game",
  "speaker_mapping": {{ "<speaker_label>": "<player_name>", ... }},
  "role_claims": [
    {{ "player": "<name>", "claimed": "liberal" | "fascist" | "hitler" | "none",
       "true": "liberal" | "fascist" | "hitler" | "unknown",
       "confidence": "weak" | "leaning" | "likely" | "strong" | "confirmed",
       "evidence": "<short>" }}
  ],
  "policies_enacted": [
    {{ "game": <int>, "round": <int>, "president": "<name>",
       "chancellor": "<name>", "policy": "liberal" | "fascist",
       "anomaly": null | "<short>" }}
  ],
  "votes": [
    {{ "game": <int>, "round": <int>, "player": "<name>",
       "vote": "ja" | "nein", "context": "<short>" }}
  ],
  "alliances": [
    {{ "id": "A<n>", "players": ["<name>", ...], "evidence": "<short>",
       "type": "protection" | "coordination" | "shared-bluff" }}
  ],
  "suspicion_profile": [
    {{ "player": "<name>", "level": "Low" | "Medium" | "High" | "Confirmed-Liberal" | "Confirmed-Fascist",
       "key_evidence": ["<short>", ...], "speech_pattern": "<short>" }}
  ],
  "predictions": [
    {{ "id": "P-<tag>-<n>", "issued_in": "analysis_<NN>",
       "claim": "<falsifiable single sentence about a named player's next move>",
       "confirms_if": "<observable>", "falsifies_if": "<observable>",
       "status": "pending" | "confirmed" | "falsified",
       "resolved_in": null | "analysis_<NN>",
       "resolution_note": null | "<short>" }}
  ],
  "key_moments": [
    {{ "id": "K<n>", "moment": "[<seconds>s]", "description": "<short>",
       "diagnostic_value": "<what it reveals>" }}
  ],
  "analysis_count": <prior + 1>
}}

Rules:
- CARRY FORWARD all prior entries; update in place.
- Resolve every pending prediction. Confirmed predictions raise that player's
  suspicion confidence in the matching direction; falsified predictions invert.
- A vote pattern (ja/nein) is more reliable than a verbal claim — weight evidence
  accordingly.
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

Render the Secret Hitler analysis from the state above. Sections, in order, citing
state ids inline (A1, K1, P-…):

## SUSPICION RANKING
Markdown ordered list, most-to-least likely Fascist, one-line reasoning per player
(cite key_moment ids and prediction ids).

## SPEAKER MAPPING
From state.speaker_mapping. List any unmapped labels.

## SUSPICION PROFILES
From state.suspicion_profile. Two-three lines per player.

## POLICY & VOTE LOG
Markdown table from state.policies_enacted + key vote anomalies from state.votes.

## ALLIANCE CLUSTERS
From state.alliances. Cite ids.

## SPEECH PATTERN ANALYSIS
Synthesise from state.suspicion_profile speech_pattern fields. Flag silence
asymmetries.

## KEY MOMENTS
From state.key_moments. Most diagnostic three to five.

## ADVISORY
Role-specific (use the user's role from system prompt). Concrete: who to nominate,
who to avoid, what to push or stall.

## SUMMARY
2-3 paragraphs synthesis. Dominant Fascist-team hypothesis. Biggest uncertainty.
Single piece of info that would most change the read.

## WATCH FOR
2-4 predictions for the next round/game. Each:
- New id (next sequential P-<tag>-<n>)
- Falsifiable claim about a named player's next behaviour
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
