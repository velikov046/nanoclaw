# Aurora / Grok Imagine — Prompting Guide

Quick reference for anyone calling `grok_imagine.py` directly or writing prompts
for `gen_card.py`, `gen_infographic.py`, or any other tool that routes through Aurora.

---

## How Aurora is called

All Aurora calls go through `grok_imagine.py` via Playwright browser automation
using Leo's SuperGrok cookies. Never hit the xAI image API directly — it's dead.

```bash
.playwright-venv/bin/python3 grok_imagine.py \
  --cookies-file groups/global/grok.com_cookies.json \
  --prompt "..." \
  --mode image \
  --out output.png \
  --profile-dir /tmp/grok-xxx \
  --timeout 180 \
  --headless
```

For pipeline use, import `_aurora_via_grok.generate()` instead of shelling out
manually — it handles the circuit-breaker, cookie resolution, and temp profile.

---

## What Aurora does well

- **Cinematic dark atmospherics** — deep blacks, teal shadows, dramatic lighting
- **Scientific/microscopic visuals** — virus particles, cells, molecular structures
- **Editorial photography style** — documentary, high contrast, moody
- **Abstract textures** — bokeh, light rays, depth-of-field backgrounds
- **Reference-guided generation** — `--reference-image` steers visual style

---

## What Aurora gets wrong

| Problem | Fix |
|---|---|
| Draws text, labels, numbers into the image | Always add: `NO text, NO labels, NO numbers, NO words anywhere` |
| Reproduces `--reference-image` too literally | Use abstract figures/diagrams as refs, not book covers or screenshots |
| Generates misaligned grid/panel structure | Never ask Aurora to draw layout — Pillow owns structure, Aurora owns atmosphere |
| Output aspect ratio is uncontrollable | Aurora picks its own ratio; always resize after with Pillow |
| Hallucinated data in charts/graphs | Never ask Aurora to visualize specific numbers — composite real data in Pillow |

---

## Prompt patterns that work

**Atmospheric background (for cards/infographics):**
```
Dark cinematic background texture for a '{topic}' editorial piece.
Deep near-black with subtle teal-blue atmospheric depth, very faint geometric
light rays or bokeh, slight vignette toward corners, premium editorial feel.
NO grid lines, NO panels, NO boxes, NO text, NO numbers, NO labels.
Pure atmospheric texture only. High resolution, 4K.
```

**Thematic subject (for gradient/split card layouts):**
```
Dark cinematic editorial photograph for '{concept}'. {field-specific visual hint}.
Left side of frame compositionally darker, right side shows the visual subject.
Moody, high contrast, professional documentary photography style.
NO text, NO labels, NO words. 4K.
```

**Close-up / inset decoration:**
```
Thematic close-up visualization: {field-specific visual}, representing '{concept}'.
Macro detail, abstract, artistic. Teal and deep blue tones, dark background.
Square composition, high detail. NO text. 4K.
```

**Style suffix to append to any prompt:**
```
cinematic, high contrast, editorial photography, 4K
```

---

## Field-specific visual hints

Used in `gen_card.py` `_FIELD_HINTS` — extend this list as needed:

| Field | Visual hint |
|---|---|
| Immunology | microscopic immune cells, antibodies, T-cell visualization |
| Virology | electron microscope view of virus particles, crystalline viral structure |
| Public Health | epidemiological mapping, medical surveillance, clinical environment |
| Philosophy of Science | abstract geometric patterns, scientific instruments, symbolic imagery |
| Genetics | DNA helix, gene sequencing, molecular biology visualization |
| Neuroscience | neural network, brain scan imagery, synapse visualization |
| Political Science | institutional architecture, government documents, formal proceedings |
| Economics | financial data visualization, market charts, institutional buildings |

---

## Reference images

`--reference-image` steers Aurora's visual style. Rules:

- **Use**: abstract figures, diagrams, scientific illustrations, charts
- **Avoid**: book covers, page screenshots with text, faces of real people
- Aurora will partially reproduce strong reference images — use for inspiration,
  not as templates
- Multiple refs (`--reference-image a --reference-image b`) blend their styles;
  keep to 1-2 max or results get muddled

In `gen_card.py`, KB-extracted figures are automatically passed as references.
KB page screenshots (text-heavy) are used as refs for inset *generation* only,
never displayed directly.

---

## Logging learnings

When a prompt technique works better than expected, log it to the shared learnings
file so it gets folded back into this guide:

```bash
# From inside a container (writable path):
python3 -c "
from datetime import datetime
entry = '''
## {datetime.now().strftime('%Y-%m-%d')} — [one-line description]

**What worked:** [prompt or technique]
**Why it worked:** [your read on it]
**Context:** [what you were generating — card bg, inset, thumbnail, etc.]
'''
with open('/workspace/global/aurora_learnings.md', 'a') as f:
    f.write(entry)
"

# From host:
# Append directly to groups/global/aurora_learnings.md
# Run tools/merge_aurora_learnings.py to fold reviewed entries into this file
```

Learnings live in `groups/global/aurora_learnings.md`. Leo reviews and merges
periodically — don't edit AURORA.md directly from a container (read-only mount).

---

## Palette reference

The Velikov / card pipeline uses these as prompting anchors:

- Background: `deep near-black (#0D1118)`
- Shadows: `teal-blue atmospheric depth`
- Accent: `amber gold` (Velikov brand) or `informational blue` (clean default)
- Style: `editorial data journalism`, `cinematic`, `high contrast`
