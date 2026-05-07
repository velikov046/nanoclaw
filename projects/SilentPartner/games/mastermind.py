"""Mastermind — real-time companion for unfamiliar broadcasts/topics.

Posture: NOT an analyst, NOT a predictor, NOT a hype commentator.
A primer that drops just-in-time context so the user can follow what's
happening and have something to say in conversation about it.

Modes (set via --mode): sport / gallery / lecture / panel / concert / broadcast.
Voice for the chime-in lines is mode-keyed via _VOICE_GUIDES below — pithy
sports-fan voice for a hockey watch party is wrong at an art opening, etc.

Two cadences (same machinery as games/conversation.py):
  - FAST (Haiku, ~20s + silence-gap): context micro-drops — WHO/TERM/REF/MOMENT/SAY
  - SLOW (Sonnet, ~3min): maintain primer state and emit TALKING POINTS in
    the mode's voice register.
"""

# Mode-keyed voice guides for the chime-in lines (SAY in fast pass, TALKING
# POINTS in slow pass).
# Company / audience guides. Different from mode (activity) and vibe (cultural
# register of the activity). Company is "who is in the room with you right now"
# — and it's the dimension that shifts safety bar + register the most.
# Wrong fact at expert dinner is much worse than wrong fact with mates.
_COMPANY_GUIDES = {
    "mates": (
        "Mates / friends. Loose, casual, banter-friendly. Pithy reactions great.\n"
        "  Profanity acceptable if it fits the room. Stat-light, vibe-heavy.\n"
        "  Bar: would your mate say this without thinking? Higher tolerance for\n"
        "  being wrong — they'll laugh, not test you."
    ),
    "experts": (
        "Subject-matter experts. PRECISE AND INCISIVE over pithy.\n"
        "  Use proper terminology. Engage with substance, not vibes. Sharp\n"
        "  questions are gold. ≤25 words is fine here, more substance allowed.\n"
        "  Bar: would an expert NOT roll their eyes? SAFETY BAR IS HIGHER —\n"
        "  experts will challenge wrong claims. Default to questions over\n"
        "  assertions when uncertain. Avoid hot takes you can't defend."
    ),
    "mixed": (
        "Mixed company (some knowledgeable, some not). Middle register.\n"
        "  Avoid in-jokes that exclude. Avoid simplifications that condescend.\n"
        "  Bar: comprehensible to non-experts, not embarrassing to experts."
    ),
    "family": (
        "Family / multigenerational. Warm, accessible, no profanity.\n"
        "  Bar: safe at a Sunday dinner. No edgy takes."
    ),
    "strangers": (
        "Strangers / first meeting. Polite, observational, low-risk.\n"
        "  Don't reveal strong opinions before reading the room.\n"
        "  Bar: would land without offending anyone."
    ),
    "alone": (
        "User watching alone. SAY/chime-ins less critical — focus on context\n"
        "  drops (WHO/TERM/REF/MOMENT) over conversational pith. The user\n"
        "  is processing for themselves, not feigning to anyone."
    ),
    "default": (
        "No company specified — default to neutral observational register.\n"
        "  When uncertain, prefer questions over assertions, vague over specific."
    ),
}


def _company_guide(company: str) -> str:
    """Resolve company string to a guide. Free-form descriptions like
    'expert physicists at a department social' fall back to 'experts' if
    keyword matches; otherwise use 'default'."""
    if not company:
        return _COMPANY_GUIDES["default"]
    key = company.lower().strip()
    if key in _COMPANY_GUIDES:
        return _COMPANY_GUIDES[key]
    # Keyword fallback for free-form descriptions
    if any(w in key for w in ("expert", "academic", "phd", "researcher", "specialist")):
        return _COMPANY_GUIDES["experts"]
    if any(w in key for w in ("mate", "friend", "pub", "casual")):
        return _COMPANY_GUIDES["mates"]
    if any(w in key for w in ("family", "parent", "in-law", "kids")):
        return _COMPANY_GUIDES["family"]
    if any(w in key for w in ("stranger", "first meet", "new")):
        return _COMPANY_GUIDES["strangers"]
    if any(w in key for w in ("alone", "solo", "by myself")):
        return _COMPANY_GUIDES["alone"]
    # Free-form description we can't classify — pass it through verbatim
    return f"Company description (free-form, treat as the register guide): {company}"


_VOICE_GUIDES = {
    "broadcast": (
        "Default: pithy enthusiast. ≤8 words. Single clause. Casual fan voice.\n"
        '  Reactions: "Big save." "Vintage Reinhart." "Classic stuff."\n'
        '  Recognition: "Of course it\'s Marchand." "He\'s been money."\n'
        '  Casual questions: "Power play coming?" "What\'s the score?"\n'
        "  Avoid: stat dumps, essay sentences, anything read-from-notes."
    ),
    "sport": (
        "Pithy sports-fan enthusiast. ≤6 words ideal, ≤8 max. Punchy.\n"
        '  "Big save." "Vintage [player]." "He\'s been money."\n'
        '  "Of course it\'s [player]." "Power play?" "Classic stuff."\n'
        "  Casual one-clause delivery, no stats, no full sentences."
    ),
    "gallery": (
        "Observational, curious, slightly arts-literate but never pretentious.\n"
        "  ≤14 words. Lean toward question or quiet observation.\n"
        '  Observations: "Interesting how the scale shifts here."\n'
        '  Questions: "Is this her recent series?" "What\'s the medium?"\n'
        '  Recognition: "Reminds me of late Hockney." (only if grounded)\n'
        "  Avoid: gushing, jargon-stacking, naming theorists you\'d have to defend."
    ),
    "lecture": (
        "Thoughtful, academic-curious. ≤16 words. Bias toward questions.\n"
        '  Questions: "Does this generalize beyond the n=20 case?"\n'
        '  Soft observations: "That assumption seems load-bearing."\n'
        '  Recognition: "Reminds me of [adjacent work] — different framing."\n'
        "  Avoid: claiming expertise, taking sides on contested claims."
    ),
    "panel": (
        "Engaged-listener, slightly insider voice. ≤14 words.\n"
        '  Recognition: "He\'s pushed that line for years."\n'
        '  Questions: "Has anyone replied to her recent piece on X?"\n'
        "  Avoid: false familiarity, dropping names you can\'t back up."
    ),
    "concert": (
        "Vibe-led appreciation. ≤8 words. Sensory over analytical.\n"
        '  "Mix sounds great tonight." "She\'s really inside it."\n'
        '  "That bassline is gorgeous." "Crowd\'s into it."\n'
        "  Avoid: theory talk, comparing to studio versions."
    ),
}


def _voice_guide(mode: str) -> str:
    """Return the voice guide for the given mode, falling back to broadcast."""
    return _VOICE_GUIDES.get((mode or "broadcast").lower(), _VOICE_GUIDES["broadcast"])


CONTEXT = """
LIVE COMPANION — primer, not analyst

You help someone follow a broadcast or live conversation about a topic they
may not fully know. Your job is just-in-time context: who is this person,
what does this term mean, why does this reference matter, what just happened
and why is it notable.

You are NOT:
- a tactical analyst (no "they should switch to a 3-5-2")
- a hype commentator (no "what a moment!")
- a predictor (no "watch for a substitution")

You ARE:
- a friend with deep domain knowledge whispering context in their ear
- terse — one short line at a time
- adaptive — sport, art, music, lecture, anything; infer the domain from the audio
"""


def get_system_prompt(participants: list, mode: str = "broadcast", role: str = "",
                      topic: str = "") -> str:
    """`participants` is optional and refers to the people *in* the broadcast
    (commentators, players, panelists), not the user. `role` describes the
    user — typically empty or 'viewer'/'listener'. `topic` is whatever the
    user told us they're watching/listening to."""
    roster = (
        f"PEOPLE TO LISTEN FOR: {', '.join(participants)}"
        if participants else "PEOPLE: infer from commentary as you go"
    )
    role_line = f"USER POSTURE: {role}" if role else "USER POSTURE: viewer/listener"
    topic_line = f"TOPIC (as user described it): {topic}" if topic else \
                 "TOPIC: infer from the broadcast"
    return f"""{CONTEXT}

MODE: {mode}
{roster}
{role_line}
{topic_line}

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- Never invent. If the broadcast hasn't said it, don't claim it.
- Brevity over completeness.
"""


# ────────────────────────────────────────────────────────────────────────────
# FAST PASS (Haiku) — context micro-drops, stateless except for bridge
# ────────────────────────────────────────────────────────────────────────────

def get_fast_prompt(window_text: str, bridge_state: dict, role: str,
                    mode: str = "broadcast") -> str:
    """Decide if the last window introduced something the user needs context
    on RIGHT NOW. Push at most one micro-drop. SKIP is the right answer most
    of the time — the bar is "would a casual viewer be lost?".
    """
    import json as _json

    bridge_block = (
        "ALREADY EXPLAINED (do NOT redefine these):\n"
        + _json.dumps(bridge_state, indent=2)
        if bridge_state else "ALREADY EXPLAINED: nothing yet — first window"
    )
    voice = _voice_guide(mode)
    company_str = (bridge_state or {}).get("company", "")
    company_guide_text = _company_guide(company_str) if company_str else ""
    company_block = (
        f"\nCOMPANY (who the user is with right now — overrides voice register):\n"
        f"  {company_str}\n"
        f"GUIDE:\n  {company_guide_text}\n"
        if company_guide_text else ""
    )
    corrections_block = ""
    if bridge_state and bridge_state.get("user_corrections"):
        lines = "\n".join(f"- {c}" for c in bridge_state["user_corrections"])
        corrections_block = (
            "\nUSER CORRECTIONS (AUTHORITATIVE — override anything in ALREADY "
            "EXPLAINED that conflicts. The user knows the domain better than "
            "you can infer from a noisy transcript):\n" + lines + "\n"
        )

    return f"""{bridge_block}
{company_block}{corrections_block}
LAST WINDOW (recent broadcast):
{window_text}

TASK:
Did something just appear that a {role or 'casual viewer'} would need context
on to follow along?

- WHO: a name they probably don't know (player, coach, artist, panelist)
- TERM: jargon, a rule, an abbreviation, a technical word
- REF: a reference to a past event, rivalry, tradition, work
- MOMENT: something happened whose significance isn't obvious

If nothing qualifies, output exactly the single token: SKIP

Otherwise output ONE micro-drop in one of these forms (one line):
WHO: <name> = <one-line ID> // say: "<chime-in line in mode voice>"
TERM: <term> = <one-line definition in this context>
REF: <reference> = <one-line context>
MOMENT: <what just happened> // say: "<chime-in line in mode voice>"
HISTORY: <high-confidence entity, era/rivalry/style note> // say: "<chime-in>"
SAY: "<chime-in line in mode voice>"   ← use when nothing needs explaining but a chime-in opportunity is hot

HISTORY rules (additional, on top of the safety rules below):
- ONLY for entities currently in ALREADY EXPLAINED.entities (high confidence).
- NEVER specific scores, years, dates, counts, or single-match claims.
- Qualitative shape only: rivalry-spans-decades, stylistic-contrast,
  multiple-titles-between-them, career-arc, era-of-the-game.
- Fire HISTORY rarely — once every several windows at most. Most windows still SKIP.

VOICE GUIDE for the "say:" / SAY line (mode = {mode}):
{voice}

VIBE override: if ALREADY EXPLAINED contains a non-empty `vibe`, match THAT
register over the generic mode voice. Vibe captures the cultural texture of
the specific event. "Lovely shot" fits Crucible snooker; "Big save" fits
hockey; both are mode=sport but different vibes.

SAFETY RULES (override voice if conflict):
- NEVER include specific numbers in any "say:" or SAY line. No goal counts,
  percentages, years, jersey numbers, dates. Vague is safer than precise.
- ABSOLUTE NAME RULE: any proper name in your output MUST appear verbatim
  in either ALREADY EXPLAINED.entities OR in the LAST WINDOW transcript.
  If a name is not present in either source, you may NOT use it. Period.
  This includes famous players, athletes, artists, composers — even if the
  topic suggests they'd be relevant. If you cannot point to the exact place
  the name was said in the transcript, do not say it. Use role labels
  ("the player at the table", "the goalie", "the host") or just SKIP.
- Anything listed in do_not_say (in ALREADY EXPLAINED) is OFF LIMITS. Never
  emit it. If the only thing notable in the window involves a do_not_say
  item, output SKIP.
- When unsure, SKIP. The user repeats these to real people; getting one
  wrong is worse than missing one. SKIP rate should be high in early windows
  before high-confidence entities exist.

Bar: would someone in this kind of room actually say this naturally, AND
could the user repeat it without risk of being wrong?

Hard rules:
- Default to SKIP. Bar is high.
- Do NOT redefine anything in ALREADY EXPLAINED.
- Pick the SINGLE most useful micro-drop. Never multiple.
- No headers, no markdown, no preamble, no closing remarks.
- No predictions. No tactical advice. No hype.
- NEVER ask the user a question. NEVER comment on the prompt or your own role.
  NEVER say "I understand" or "ready to help" — just emit SKIP or one micro-drop.
- If the LAST WINDOW is empty or too short to identify anything, output SKIP.
"""


# ────────────────────────────────────────────────────────────────────────────
# SLOW PASS (Sonnet) — primer state extraction + rendering
# ────────────────────────────────────────────────────────────────────────────

EMPTY_STATE = {
    "topic":      "",
    "domain":     "",   # "soccer" / "F1" / "art opening" / "lecture: <field>" / inferred
    "vibe":       "",   # 1-2 line cultural register of THIS specific event/sport/setting
    "company":    "",   # "mates" / "experts" / "family" / "alone" / free-form ("expert physicists at dept social")
    "scene":      "",   # 2-3 sentence stage-setter: format, stakes, who's playing, broader thread
    "entities":   [],   # [{name, role, one_line, first_seen, source_quote, confidence}]
    "glossary":   [],   # [{term, definition, first_seen, source_quote, confidence}]
    "narratives": [],   # [{title, summary, status, confidence}]
    "key_moments": [],  # [{position, what, why_it_matters}]
    "historic_context": [],  # [{about, fact, confidence}] — qualitative background, model knowledge
    "do_not_say": [],   # [{item, reason}] — flagged for user safety, never repeat in chime-ins
    "analysis_count": 0,
}


def bridge_from_state(state: dict) -> dict:
    """Slim subset for fast-pass context. Filters to HIGH-CONFIDENCE entries
    only — chime-ins must be safe to repeat in the room. Low/inferred entries
    are forwarded as do_not_say warnings so Haiku knows what to avoid."""
    if not state:
        return {}
    entities = state.get("entities", [])
    glossary = state.get("glossary", [])
    return {
        "domain":     state.get("domain", ""),
        "vibe":       state.get("vibe", ""),
        "company":    state.get("company", ""),
        "scene":      state.get("scene", ""),
        # Only entries the broadcast clearly established — safe to mention.
        "entities":   [
            {"name": e.get("name"), "one_line": e.get("one_line")}
            for e in entities
            if (e.get("confidence") or "high") == "high"
        ],
        "glossary":   [
            {"term": g.get("term"), "definition": g.get("definition")}
            for g in glossary
            if (g.get("confidence") or "high") == "high"
        ],
        "narratives": [n.get("title") for n in state.get("narratives", [])
                       if (n.get("confidence") or "high") in ("high", "medium")],
        # Names/terms the model has already flagged as suspect — never repeat.
        "do_not_say": [
            {"item": e.get("name"), "reason": "low-confidence entity"}
            for e in entities
            if (e.get("confidence") or "high") in ("low", "inferred")
        ] + [
            {"item": g.get("term"), "reason": "low-confidence term"}
            for g in glossary
            if (g.get("confidence") or "high") in ("low", "inferred")
        ] + state.get("do_not_say", []),
    }


def get_extraction_prompt(prior_state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS (treat as authoritative):\n" + _json.dumps(priors, indent=2)
        if priors else "PRIORS: none"
    )
    prior_state_block = (
        "PRIOR STATE (carry forward, update, do not duplicate):\n"
        + _json.dumps(prior_state, indent=2)
        if prior_state.get("analysis_count", 0) > 0
        else "PRIOR STATE: empty — first slow cycle"
    )
    return f"""{priors_block}

{prior_state_block}

EXTRACTION TASK (Pass 1 of 2):

Update the primer state from the transcript above. Output STRICT JSON ONLY
between fenced ```json ... ``` markers. No prose before or after.

Schema:

{{
  "topic":      "<as user described or inferred>",
  "domain":     "<sport / field / format — e.g. 'Premier League soccer', 'contemporary art opening', 'systems neuroscience lecture'>",
  "company":    "<who the user is with — from priors.user_company if given, else carry forward. 'mates' / 'experts' / 'mixed' / 'family' / 'strangers' / 'alone', OR a free-form description like 'expert physicists at department social'. THIS DRIVES TALKING-POINTS REGISTER MORE THAN ANY OTHER FIELD: experts demand precise/incisive, mates want pithy banter, family needs warm-accessible. If unspecified, leave empty.>",
  "vibe":       "<1-2 line CULTURAL REGISTER of this specific event/setting. What does it FEEL like? What kind of language do fans/attendees use? Examples: 'Crucible snooker — hushed, polite, methodical, deeply British, technical-yet-emotional reverence' OR 'Premier League away end — loud, partisan, working-class, profane affection' OR 'gallery opening — oblique, slightly precious, observational not declarative' OR 'F1 paddock — technical, cosmopolitan, driver-storyline-soaked'. The vibe drives chime-in voice: pithy hockey != pithy snooker, even though both are mode=sport.>",
  "scene":      "<2-3 sentences setting the stage: competition format if any (regular season / playoffs / Game N of best-of-7 / opening night / keynote), who's playing or presenting, the stakes, the broader thread or storyline. This is what a friend would tell you in 10 seconds when you walk into the room and ask 'what is this?' Update each cycle as more is revealed.>",
  "entities": [
    {{ "name": "<name as broadcast said it>", "role": "<short role>",
       "one_line": "<who they are, why they matter here>",
       "first_seen": "[<seconds>s]",
       "source_quote": "<5-15 words from transcript where this name appeared — REQUIRED>",
       "confidence": "high" | "medium" | "low" | "inferred" }}
  ],
  "glossary": [
    {{ "term": "<term or abbreviation>",
       "definition": "<one-line, in-context>",
       "first_seen": "[<seconds>s]",
       "source_quote": "<exact transcript phrase where it appeared — REQUIRED>",
       "confidence": "high" | "medium" | "low" | "inferred" }}
  ],
  "narratives": [
    {{ "title": "<short — e.g. 'rookie debut', 'manager on hot seat'>",
       "summary": "<2 sentences>",
       "status": "active" | "resolved" | "background",
       "confidence": "high" | "medium" | "low" }}
  ],
  "key_moments": [
    {{ "position": "[<seconds>s]",
       "what": "<short>",
       "why_it_matters": "<one line — context the user may have missed>" }}
  ],
  "historic_context": [
    {{ "about": "<who/what this concerns — must be a high-confidence entity from this session>",
       "fact": "<qualitative, well-known background — rivalry, era, stylistic note, career-arc shape; NEVER specific scores, years, or stats>",
       "confidence": "high" | "medium" | "low" }}
  ],
  "do_not_say": [
    {{ "item": "<name or term that's risky to repeat>",
       "reason": "<short — 'whisper-mangled', 'heard once unclearly', 'inferred not stated', 'name does not match likely roster'>" }}
  ],
  "analysis_count": <prior + 1>
}}

CONFIDENCE RUBRIC — be paranoid. The user repeats high-confidence entries to
real people in the room; getting one wrong is embarrassing.
- "high"     : broadcast named/used this 2+ times distinctly, role unambiguous,
               name is plausible for the inferred domain.
- "medium"   : named clearly once, role inferred from context, name plausible.
- "low"      : name distorted (likely Whisper artifact — odd spelling, single
               syllable, unusual phonetics for the domain), or attribution unclear,
               or only heard once unclearly. ALWAYS add to do_not_say.
- "inferred" : reconstructed from context — broadcast did NOT actually say this.
               ALWAYS add to do_not_say. Never appears in chime-ins.

When in doubt, mark "low". Better to flag DON'T SAY than be confidently wrong.

source_quote is REQUIRED. If you cannot quote 5-15 words from the transcript
that establish this entity/term, you cannot include the entry. No quote, no entry.

do_not_say should also include any specific NUMBERS or STATS the broadcast
mentioned that the user might be tempted to repeat — these are landmines.
("16 postseason goals last year" → flag with reason "stat — verify before repeating".)

historic_context is for qualitative model-knowledge background ONLY — only
include for high-confidence entities established in this session. Hard rules:
- NO specific numbers (years, scores, dates, counts). Ever.
- NO single-event claims you can't be sure of ("won the 2017 final" — banned).
- YES era/rivalry shape ("they've been doing this since the 90s"), stylistic
  contrast ("his break-building vs the other's pot-and-run flair"), broad
  career arc ("multiple world titles between them"). Falsifiability bar is
  "would a casual fan accept this without challenging it?".
- If you can't write a fact that meets these rules, leave the section empty.

Rules:
- CARRY FORWARD prior entries; update fields when the broadcast adds info,
  but do NOT duplicate. If "Pep" appears in prior entities, do not add a new
  entry; refine the existing one if needed.
- entities + glossary should reflect what's been MENTIONED so far, not what
  the model knows about the world. If the broadcast hasn't named someone,
  don't include them.
- Keep `key_moments` capped at the 12 most recent.
- No predictions field. Mastermind is not a predictor.
- Output JSON ONLY, fenced.
"""


def get_reasoning_prompt(state: dict, priors: dict | None) -> str:
    import json as _json
    priors_block = (
        "PRIORS:\n" + _json.dumps(priors, indent=2) if priors else "PRIORS: none"
    )
    voice = _voice_guide(state.get("mode") or state.get("domain") or "broadcast")
    company_str = state.get("company", "")
    company_guide = _company_guide(company_str) if company_str else ""
    return f"""{priors_block}

CURRENT STATE:
```json
{_json.dumps(state, indent=2)}
```

REASONING TASK (Pass 2 of 2):

Render a primer brief from the state above (do not re-derive from transcript).
The reader is a viewer/listener catching up on an unfamiliar broadcast.

Sections, in order:

## VIBE
One short line: state.vibe verbatim if non-empty. Skip section if empty.
This is the cultural register guide for everything below — chime-ins must
match this voice, even within the same `mode`.

## SCENE
Lead with state.scene verbatim if non-empty; otherwise infer 2-3 sentences
that set the stage. Format: "It's a [competition] between [X] and [Y].
[Stakes / where in the series / what's been happening]. [Broader thread,
storyline, or what's at stake tonight]." Plain English. No bullets.

## DOMAIN
One line: what kind of thing is this and at what level (e.g. "Premier League
soccer, top-flight English football").

## WHO'S INVOLVED
Markdown table from state.entities, FILTERED to confidence == "high" only.
Columns: Name | Role | One-line. Skip the section if no entries qualify.

## DON'T SAY
List every state.do_not_say entry, plus every entity with confidence in
{{"low", "inferred"}} and every glossary entry with confidence in {{"low",
"inferred"}}. Format: bullet "**<item>** — <reason>". This is the user's
safety net: anything in this section is a name they'll see in WHO'S INVOLVED
context but should NOT confidently repeat.

If the section would be empty, write: "Nothing flagged — current entities all
look solid."

## GLOSSARY
Markdown table from state.glossary. Columns: Term | Meaning here.
Skip if empty.

## CURRENT NARRATIVES
For each entry in state.narratives (active first, then background, skip
resolved unless very recent): title in bold + 2-line summary.

## KEY MOMENTS
Bullet list from state.key_moments, most recent first. One line each:
"[Ns] — what happened (why it matters)".

## HISTORIC CONTEXT
Bullet list from state.historic_context, high-confidence entries first.
Format: "**<about>** — <fact>". Skip the section entirely if state.historic_context
is empty. Hard rule: never invent a stat or year here. Qualitative only.

## TALKING POINTS
5-8 chime-in lines in the voice register for this mode. The user wants to
feign comprehension and enthusiasm — these go STRAIGHT INTO THE ROOM.

VOICE for this mode:
{voice}

VIBE — match the cultural register of THIS specific event (from state.vibe).
Pithy at a hockey game ≠ pithy at the Crucible — match the room. If
state.vibe is empty, fall back to the mode's voice guide above.

COMPANY (who the user is with — overrides voice and vibe if conflict):
state.company = {company_str!r}
{company_guide}

The COMPANY guide is the strongest register signal. An expert audience
demands precise/incisive over pithy. Mates want banter. Family wants warm.
The same match watched alone vs at an expert's house produces different
TALKING POINTS shapes — adapt accordingly.

SAFETY RULES (these matter more than voice):
- NEVER include specific numbers in chime-ins. No goal counts, percentages,
  years, jersey numbers, dates, durations. "He's been money" is safe;
  "he's got 16 goals" is a landmine. Numbers can be checked.
- ONLY reference entities with confidence == "high". Do not name anyone
  whose name is in DON'T SAY. The bridge filters this for the fast pass;
  you must enforce it here.
- Vague is safer than precise. The user is feigning comprehension, not
  passing a quiz.
- If you can't write 5 lines that meet these rules, write fewer.

OTHER RULES:
- Write AS the line itself, never "you could say" framing.
- No stat dumps. No essay sentences. No primer voice.
- Mix shapes: reactions / recognition / one-line opinions / casual questions.
- If a line wouldn't pass naturally in this kind of room, cut it.

Constraints:
- No em-dashes. Use commas, en-dashes, colons.
- No predictions. No tactical advice.
- Anything not in state stays out.
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
