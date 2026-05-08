# Video Production Pipeline

Shared pipeline for Velikov, Lydia, and Stella. Read this when producing a video — not at every session start.

Scripts live in `/workspace/extra/youtube/pipeline/`. Jobs live in `/workspace/extra/youtube/jobs/<job_id>/`.

---

## Job structure

```
jobs/<job_id>/
  script.json       ← you write this in Step 1
  audio/            ← narrate.py fills this (Step 2)
  anchor.jpg        ← character anchor (Step 2.5, optional but expected for character videos)
  images/           ← gen_images.py fills this (Step 3)
  thumbnail.jpg     ← gen_thumbnail.py creates this (Step 4)
  opener.mp4        ← gen_opener.py creates this (Step 4a, hook + visual + music)
  closer.mp4        ← gen_closer.py creates this (Step 4c, closing line + swell + fade)
  broll/            ← gen_broll.py fills this (Step 4b, optional)
  output.mp4        ← compose.py creates this (Step 5)
```

---

## Step 1 — Write script.json

```json
{
  "title": "Video title",
  "description": "Description for YouTube (in your voice)",
  "thumbnail_prompt": "Visual concept — specific, no text",
  "tags": ["tag1", "tag2"],
  "music": false,

  "hook_script": "Why did they stop testing for it after 2020?",
  "hook_mode": "question",
  "opener_visual_prompt": "Modern expedition cruise ship adrift in dense fog, somber, photoreal",
  "title_overlay": "THE CRUISE THEY DIDN'T WANT YOU TO SEE",

  "closer_script": "And that's the question the testing data quietly stopped asking.",
  "closer_visual_prompt": "Empty cruise corridor at night, single overhead light, slow zoom",
  "end_card": "Subscribe — the next one's about the testing protocol they buried.",

  "punchlines": [
    "It doesn't just open an inquiry — it opens a market.",
    "Same outbreak, same protocol — different ending."
  ],

  "segments": [
    {
      "id": 1,
      "text": "Narration for this segment. Written for the ear.",
      "image_prompt": "Visual prompt for this segment's image"
    }
  ]
}
```

- 6–10 segments, ~25–35 seconds each, ~300 words total
- `music: true` → pick a track from `/workspace/extra/youtube/music/` and pass it to compose
- **Hook fields** (`hook_script`, `hook_mode`, `opener_visual_prompt`, `title_overlay`) drive `gen_opener.py`. The hook MUST be either an audience question or a shocking-truth statement — declarative narration alone won't hold viewers past the 5-second cliff. Aim for ≤4s of speech so the opener lands under 5s total.
- **Closer fields** (`closer_script`, `closer_visual_prompt`, `end_card`) drive `gen_closer.py`. One sentence, callback to the thesis or hook for the next video.
- **Punchlines** (`punchlines`): top-level array of substrings that MUST land dramatically. `narrate.py` passes each that appears in the current segment to `tag_cli.py`, which wraps it with `<break time="1.0s"/>` + a `[dramatic]` / `[emphatic]` tag for ElevenLabs v3. Use for the load-bearing rhetorical payoffs — contrast reveals, fragment punches, "not just X — Y" structures. Don't over-flag: 2-4 per video is right; tagging everything as a punchline destroys the pattern.
- **Segment text must SEGUE** — adjacent segments shouldn't read like cold openings to chapters. If segment 1 ends "...protocol they buried." then segment 2 starts "The time was January 2020." — that's whiplash. Open each non-first segment with a transitional clause that picks up the thread:
  - Pivot question: "Now what about the timing?", "And the dates? That's where it gets stranger."
  - Causal carry: "Which brings us to —", "And then —", "Because of that —"
  - Counter-point: "But here's the thing.", "Except the records say otherwise."
  - Pronoun callback: "That protocol — the one they buried — has a sibling."
  The 300ms compose breath gives audio space; only good prose makes the listener want to stay through it.
- Image prompts: specific and visual — match your character's aesthetic

---

## Step 2 — Narrate

```bash
python3 /workspace/extra/youtube/pipeline/narrate.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --voice <your_voice_name> \
  --char-profile <velikov|lydia|stella>
```

Generates per-segment MP3s and `audio/final.mp3`. Each segment is passed through `tag_cli.py` first to insert ElevenLabs v3 emotion tags. Uses `eleven_v3` model. Updates `script.json` with audio paths and durations.

---

## Step 2.5 — Anchor image (optional, for single-subject videos)

**Skip this step for essay-style videos** that cut between varied imagery, different figures, archival shots, locations — Aurora's natural variety is the right look there.

**Use an anchor when the video centres on one recurring subject** — a single character followed across the video, a creature, a specific location revisited. Without an anchor, Aurora drifts: ask for "the same detective" across 30 beats and you get 30 different detectives — different face, different coat, different age. Aurora has no memory between calls. The anchor is what gives it one.

The anchor is a single image of the central subject, generated once, then attached as a build-from reference on every subsequent gen call (beats + thumbnail). Aurora preserves face, clothing, and palette strongly when a reference is attached.

**1. Generate the anchor.** Pick the most distinctive, identifying frame possible — a clear portrait of the character in their canonical look. Write a prompt with concrete identifying details (build, age, hair, clothing, palette, lighting medium):

```bash
python3 /workspace/extra/youtube/pipeline/_aurora_via_grok.py \
  "vintage 1970s film-noir detective: tall, weathered face, three-day stubble, charcoal trench coat, dark felt fedora, kodachrome film stock, photoreal" \
  /workspace/extra/youtube/jobs/<job_id>/anchor.jpg
```

Inspect the result. If it isn't right, regenerate before continuing — every downstream image inherits from this one. Keep the anchor in the job dir (or in a longer-lived `/workspace/group/characters/` if reused across videos).

**2. Wire it into the script.** Add `character_reference` at the top level of `script.json`:

```json
{
  "title": "...",
  "character_reference": "/workspace/extra/youtube/jobs/<job_id>/anchor.jpg",
  "segments": [...]
}
```

`gen_images.py` attaches the anchor on every beat. `gen_thumbnail.py` picks it up automatically. Per-beat override: set `beat.reference_image` for a specific beat that needs a different anchor (e.g. a secondary character entering for one segment). Per-thumbnail override: set `thumbnail_reference` if you want a different framing on the thumbnail.

**Decide upfront** whether your video has a single recurring subject worth anchoring to — most essay-style videos don't, and varied imagery between cuts is the right look. The anchor is the deliberate choice for character-driven pieces, not a default.

---

## Step 3 — Generate images

```bash
python3 /workspace/extra/youtube/pipeline/gen_images.py \
  --job /workspace/extra/youtube/jobs/<job_id>
```

Generates one image per segment via Aurora — routed through `tools/grok_imagine.py` (browser-drive of grok.com using SuperGrok cookies at `/workspace/global/grok.com_cookies.json`). Updates `script.json` with image paths. If `character_reference` is set (Step 2.5), it's attached on every beat. No API key needed.

---

## Step 4 — Generate thumbnail

```bash
python3 /workspace/extra/youtube/pipeline/gen_thumbnail.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --char-profile <velikov|lydia|stella>
```

Generates `thumbnail.jpg` from `thumbnail_prompt` in script.json. Each char-profile applies a different aesthetic style. Routed through `grok_imagine.py` like step 3 — no API key needed.

---

## Step 4a — Opener (mandatory if you care about retention)

```bash
python3 /workspace/extra/youtube/pipeline/gen_opener.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --voice <Malakai|James Oak|...> \
  --char-profile <velikov|lydia|stella>
```

Builds a ≤5s `opener.mp4` with: hook line voiced (ElevenLabs + tag_cli emote injection) + Aurora video visual + music sting (sidechain-ducked under the voice) + optional bold title overlay.

The hook MUST come from `hook_script` in script.json — phrased as an **audience question** or a **shocking-truth statement**. Declarative narration alone gets scrolled past in the first 5 seconds. Falls back to a generic question derived from `title` if `hook_script` is missing, but that fallback is for legacy scripts only — write the hook deliberately for real production.

Compose picks up the result via `opener_file` in script.json (auto-set by gen_opener). The legacy `kling_file` from gen_broll still works as a silent fallback.

---

## Step 4b — B-roll and Kling opener (optional)

```bash
python3 /workspace/extra/youtube/pipeline/gen_broll.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --kling \
  --broll \
  --broll-count 3
```

- `--kling`: generates a 5s AI video opener via Kling from `thumbnail_prompt`. Requires `KLING_API_KEY`. Polls up to 3 minutes.
- `--broll`: fetches up to `--broll-count` Pixabay clips matched to `tags`. Requires `PIXABAY_API_KEY`.

Both skip silently if their key is absent. Paths written to `script.json`; compose picks them up automatically and prepends them as a muted visual opener before the narrated segments. **Use `--music` in compose when running this** — the opener is silent without it.

Prefer Step 4a over Step 4b's `--kling` for the opener — gen_opener gives a real voiced hook with music, not a silent visual.

---

## Step 4c — Closer (lands the message, earns the next click)

```bash
python3 /workspace/extra/youtube/pipeline/gen_closer.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --voice <Malakai|James Oak|...> \
  --char-profile <velikov|lydia|stella>
```

Builds a `closer.mp4` (~5-7s) with: closing line voiced + Aurora video visual + music swell-then-fade + optional `end_card` text overlay (channel handle, subscribe prompt, hook for next video). Last 1.5s fades to black + silence.

The closing line should be a callback to the thesis or a hook for the next video — not a "thanks for watching." Compose picks it up via `closer_file` in script.json.

---

## Step 5 — Compose

```bash
# Without music:
python3 /workspace/extra/youtube/pipeline/compose.py \
  --job /workspace/extra/youtube/jobs/<job_id>

# With music:
python3 /workspace/extra/youtube/pipeline/compose.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --music /workspace/extra/youtube/music/track.mp3 \
  --music-volume 0.15
```

Assembles `output.mp4`. Ken Burns pan/zoom on each image synced to narration. If `kling_file` or `broll_files` are in `script.json`, they are normalized to 1920x1080 and prepended as a muted intro. Music (if provided) runs over the full duration.

---

## Step 6 — Upload

```bash
python3 /workspace/tools/youtube_upload.py \
  /workspace/extra/youtube/jobs/<job_id>/output.mp4 \
  --title "Title" \
  --description "Description" \
  --thumbnail /workspace/extra/youtube/jobs/<job_id>/thumbnail.jpg \
  --privacy unlisted \
  --channel <channel_number> \
  --tags "tag1,tag2,tag3"
```

Channel mapping: `/workspace/extra/youtube/channels.json`. Default privacy: `unlisted`.
