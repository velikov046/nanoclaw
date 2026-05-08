# velikov — music library

Background music + stings for velikov's YouTube pipeline.

## Naming convention (pipeline auto-picks)

- `intro.mp3`   — opener sting (gen_opener.py picks this first; falls back to first mp3)
- `outro.mp3`   — closer swell (gen_closer.py picks this first)
- `bed_<name>.mp3` — full-length background bed for compose.py --music
- Any other `*.mp3` becomes a candidate. `script.json: "music": true` triggers auto-pick.

## How compose mixes it

Music is sidechain-ducked under narration via ffmpeg `sidechaincompress` —
voice stays clean, music breathes between sentences. `amix normalize=0` so
narration doesn't get halved (default `normalize=1` silently does that).

## Tracking

mp3 files are gitignored by default (large + often licensed). Only this
README is tracked. Drop tracks in here locally; back up separately.
