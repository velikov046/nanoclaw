# Video Production Pipeline

Shared pipeline for Velikov, Lydia, and Stella. Read this when producing a video — not at every session start.

Scripts live in `/workspace/extra/youtube/pipeline/`. Jobs live in `/workspace/extra/youtube/jobs/<job_id>/`.

---

## Job structure

```
jobs/<job_id>/
  script.json       ← you write this in Step 1
  audio/            ← narrate.py fills this (Step 2)
  images/           ← gen_images.py fills this (Step 3)
  thumbnail.jpg     ← gen_thumbnail.py creates this (Step 4)
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

## Step 3 — Generate images

```bash
python3 /workspace/extra/youtube/pipeline/gen_images.py \
  --job /workspace/extra/youtube/jobs/<job_id>
```

Generates one image per segment via xAI Aurora. Updates `script.json` with image paths. Requires `XAI_API_KEY`.

---

## Step 4 — Generate thumbnail

```bash
python3 /workspace/extra/youtube/pipeline/gen_thumbnail.py \
  --job /workspace/extra/youtube/jobs/<job_id> \
  --char-profile <velikov|lydia|stella>
```

Generates `thumbnail.jpg` from `thumbnail_prompt` in script.json. Each char-profile applies a different aesthetic style. Requires `XAI_API_KEY`.

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
