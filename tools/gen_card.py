"""
gen_card.py — Generate knowledge cards from KB concepts.

Three layout modes:
  gradient  — Aurora fills frame, left-to-right dark gradient for text (default)
  split     — Dark panel left, Aurora image visible right
  inset     — Aurora bg + second Aurora circular inset, text full-width

Usage:
  python3 gen_card.py --query "hantavirus"
  python3 gen_card.py --query "mRNA" --layout split
  python3 gen_card.py --query "AIDS" --layout inset
  python3 gen_card.py --query "test" --skip-aurora
  python3 gen_card.py --query "test" --brand
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError as e:
    sys.exit(f"missing dep: {e}  →  pip install Pillow numpy")

_TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(_TOOLS_DIR))
try:
    from brand_thumbnail import apply_color_lut, apply_vignette, apply_grain, FONT_CANDIDATES
    _HAS_BRAND = True
except ImportError:
    _HAS_BRAND = False
    FONT_CANDIDATES = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

# ── Paths ─────────────────────────────────────────────────────────────────────

KB_PATH        = _TOOLS_DIR.parent / "groups/velikov/researcher/knowledge/base.json"
KB_IMAGES_DIR  = _TOOLS_DIR.parent / "groups/velikov/researcher/knowledge/images"
GROK_IMAGINE   = _TOOLS_DIR / "grok_imagine.py"
PLAYWRIGHT_PY  = _TOOLS_DIR / ".playwright-venv/bin/python3"
OUT_DIR        = _TOOLS_DIR.parent / "groups/velikov/researcher/cards"

LAYOUTS = ("gradient", "split", "inset")

# ── Palettes ──────────────────────────────────────────────────────────────────

WHITE = (255, 255, 255)

_PALETTE_CLEAN = {
    "accent":   (70, 190, 255),
    "panel_bg": (20, 26, 38),
    "dark_bg":  (13, 17, 26),
    "dim_text": (120, 148, 178),
    "border":   (35, 50, 72),
}
_PALETTE_VELIKOV = {
    "accent":   (255, 200, 60),
    "panel_bg": (28, 28, 36),
    "dark_bg":  (18, 18, 22),
    "dim_text": (155, 155, 170),
    "border":   (40, 40, 52),
}

ACCENT   = _PALETTE_CLEAN["accent"]
PANEL_BG = _PALETTE_CLEAN["panel_bg"]
DARK_BG  = _PALETTE_CLEAN["dark_bg"]
DIM_TEXT = _PALETTE_CLEAN["dim_text"]
BORDER   = _PALETTE_CLEAN["border"]


def _setup_palette(brand: bool) -> None:
    global ACCENT, PANEL_BG, DARK_BG, DIM_TEXT, BORDER
    p = _PALETTE_VELIKOV if brand else _PALETTE_CLEAN
    ACCENT, PANEL_BG, DARK_BG, DIM_TEXT, BORDER = (
        p["accent"], p["panel_bg"], p["dark_bg"], p["dim_text"], p["border"],
    )


# ── KB query ──────────────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def query_kb(query: str) -> dict | None:
    if not KB_PATH.exists():
        print(f"[kb] base.json not found at {KB_PATH}", file=sys.stderr)
        return None
    with open(KB_PATH) as f:
        kb = json.load(f)

    def _score(qtoks: set[str]) -> list[tuple[int, dict]]:
        scored: list[tuple[int, dict]] = []
        for c in kb.get("concepts", []):
            text = f"{c.get('name','')} {c.get('summary','')} {c.get('field','')}"
            overlap = len(qtoks & _tokens(text))
            if overlap:
                scored.append((overlap, c))
        scored.sort(key=lambda x: -x[0])
        return scored

    results = _score(_tokens(query))
    if not results:
        for word in query.lower().split():
            if len(word) >= 4:
                results = _score({word})
                if results:
                    break
    return results[0][1] if results else None


def find_kb_images(concept: dict, max_images: int = 2) -> list[Path]:
    """Find extracted KB images whose source directory best matches the concept's sources."""
    if not KB_IMAGES_DIR.exists():
        return []

    # Gather candidate source stems from concept
    stems: list[str] = []
    if sf := concept.get("source_file"):
        stems.append(sf)
    for s in concept.get("sources") or []:
        stems.append(Path(s).stem)

    if not stems:
        return []

    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

    slug_stems = [_slug(s) for s in stems]

    # Score each image directory by token overlap with any stem
    best_dir: Path | None = None
    best_score = 0
    for d in KB_IMAGES_DIR.iterdir():
        if not d.is_dir():
            continue
        dtoks = _tokens(d.name)
        score = max(len(_tokens(slug) & dtoks) for slug in slug_stems)
        if score > best_score:
            best_score, best_dir = score, d

    if not best_dir or best_score == 0:
        return []

    imgs = sorted(best_dir.glob("*.png")) + sorted(best_dir.glob("*.jpg"))
    # Tier 1: extracted figures with captions  (page-XXXX-img-NN_description.ext)
    figures = [p for p in imgs if re.match(r"page-\d{4}-img-\d+_.+", p.name)]
    # Tier 2: named page screenshots (page-XXXX_description.ext) — text-heavy but usable as ref
    screenshots = [p for p in imgs if re.match(r"page-\d{4}_", p.name)]
    pool = figures if figures else screenshots if screenshots else imgs
    print(f"[kb-img] {best_dir.name} — {len(figures)} figures, {len(screenshots)} screenshots")
    return pool[:max_images]


# ── Aurora ────────────────────────────────────────────────────────────────────

_COOKIE_CANDIDATES = [
    _TOOLS_DIR.parent / "groups/global/grok.com_cookies.json",
    _TOOLS_DIR.parent / "data/sessions/velikov/grok.com_cookies.json",
    Path("/workspace/global/grok.com_cookies.json"),
]

_FIELD_HINTS: dict[str, str] = {
    "immunology":          "microscopic immune cells, antibodies, T-cell visualization",
    "virology":            "electron microscope view of virus particles, crystalline viral structure",
    "public health":       "epidemiological mapping, medical surveillance, clinical environment",
    "philosophy":          "abstract geometric patterns, scientific instruments, symbolic imagery",
    "genetics":            "DNA helix, gene sequencing, molecular biology visualization",
    "medicine":            "clinical laboratory, medical imaging, microscopy",
    "chemistry":           "molecular structures, chemical reactions, laboratory glassware",
    "physics":             "particle physics visualization, quantum field patterns, wave interference",
    "neuroscience":        "neural network, brain scan imagery, synapse visualization",
    "epidemiology":        "disease spread mapping, statistical heatmaps, population data",
    "political":           "institutional architecture, government documents, formal proceedings",
    "history":             "archival photography, aged documents, period-specific imagery",
    "economics":           "financial data visualization, market charts, institutional buildings",
    "psychology":          "cognitive pattern visualization, abstract mind imagery",
}


def _field_hint(concept: dict) -> str:
    field = (concept.get("field") or "").lower()
    domain = (concept.get("domain") or "").lower()
    combined = f"{field} {domain}"
    for key, hint in _FIELD_HINTS.items():
        if key in combined:
            return hint
    return "abstract dark editorial photography, scientific visualization, cinematic depth"


def _resolve_cookies() -> Path | None:
    env = os.environ.get("GROK_COOKIES_FILE")
    if env:
        return Path(env)
    for p in _COOKIE_CANDIDATES:
        if p.exists():
            return p
    return None


def _aurora(prompt: str, out_path: Path, timeout_s: int = 180,
            reference_images: list[Path] | None = None) -> bool:
    if not PLAYWRIGHT_PY.exists():
        return False
    cookies = _resolve_cookies()
    if not cookies:
        print("[aurora] no grok.com cookies found", file=sys.stderr)
        return False
    profile_dir = Path(tempfile.mkdtemp(prefix="grok-card-", dir="/tmp"))
    cmd = [
        str(PLAYWRIGHT_PY), str(GROK_IMAGINE),
        "--cookies-file", str(cookies),
        "--prompt", prompt,
        "--mode", "image",
        "--out", str(out_path),
        "--profile-dir", str(profile_dir),
        "--timeout", str(timeout_s),
        "--headless",
    ]
    for ref in (reference_images or []):
        cmd += ["--reference-image", str(ref)]
    if reference_images:
        print(f"[aurora] using {len(reference_images)} KB reference image(s)")
    print(f"[aurora] {prompt[:90]}…")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 60)
    if result.returncode != 0:
        print(f"[aurora] failed:\n{result.stderr[-400:]}", file=sys.stderr)
        return False
    print(f"[aurora] → {out_path}")
    return True


def bg_prompt(concept: dict, layout: str) -> str:
    name = concept.get("name", "")
    hint = _field_hint(concept)
    if layout == "gradient":
        return (
            f"Dark cinematic editorial photograph for '{name}'. {hint}. "
            "Left side of frame compositionally darker, right side shows the visual subject. "
            "Moody, high contrast, professional documentary photography style. "
            "NO text, NO labels, NO words. 4K."
        )
    elif layout == "split":
        return (
            f"Cinematic editorial photograph: {hint}, related to '{name}'. "
            "Strong visual subject centered or on the right side, dark teal tones. "
            "High contrast, dramatic lighting, no text. 4K."
        )
    else:  # inset
        return (
            f"Dark atmospheric editorial background, deep navy and teal textures, "
            f"subtle depth for '{name}' knowledge card. "
            "Very dark, slight bokeh, premium feel. NO text, NO structure. 4K."
        )


def inset_prompt(concept: dict) -> str:
    name = concept.get("name", "")
    hint = _field_hint(concept)
    return (
        f"Thematic close-up visualization: {hint}, representing '{name}'. "
        "Macro detail, abstract, artistic. Teal and deep blue tones, dark background. "
        "Square composition, high detail. NO text. 4K."
    )


# ── Pillow helpers ────────────────────────────────────────────────────────────

_RESAMPLE = getattr(getattr(Image, "Resampling", None), "LANCZOS", Image.LANCZOS)  # type: ignore[attr-defined]


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = FONT_CANDIDATES if bold else [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    ] + FONT_CANDIDATES
    for f in candidates:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def _wrap_draw(draw: ImageDraw.ImageDraw, text: str, font, x: int, y: int,  # type: ignore[type-arg]
               max_w: int, fill, line_gap: int = 8) -> int:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_w:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    h = 0
    for ln in lines:
        draw.text((x, y + h), ln, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), ln, font=font)
        h += int(bbox[3] - bbox[1]) + line_gap
    return h


def _gradient_overlay_lr(img: Image.Image,
                         left_alpha: int = 220, right_alpha: int = 40) -> Image.Image:
    """Left-to-right dark gradient overlay, using numpy for speed."""
    W, H = img.size
    alpha = np.linspace(left_alpha, right_alpha, W, dtype=np.float32)
    alpha_2d = np.tile(alpha[np.newaxis, :], (H, 1)).astype(np.uint8)
    r = np.full((H, W), DARK_BG[0], dtype=np.uint8)
    g = np.full((H, W), DARK_BG[1], dtype=np.uint8)
    b = np.full((H, W), DARK_BG[2], dtype=np.uint8)
    overlay_arr = np.stack([r, g, b, alpha_2d], axis=2)
    overlay = Image.fromarray(overlay_arr, "RGBA")
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _split_overlay(img: Image.Image, split_frac: float = 0.55,
                   fade_frac: float = 0.12) -> Image.Image:
    """Solid dark panel on left, gradient fade zone, image visible on right."""
    W, H = img.size
    split_x = int(W * split_frac)
    fade_w = int(W * fade_frac)
    total_w = split_x + fade_w

    alpha = np.zeros(W, dtype=np.float32)
    alpha[:split_x] = 235
    if fade_w > 0:
        alpha[split_x:total_w] = np.linspace(235, 0, fade_w)

    alpha_2d = np.tile(alpha[np.newaxis, :], (H, 1)).astype(np.uint8)
    r = np.full((H, W), DARK_BG[0], dtype=np.uint8)
    g = np.full((H, W), DARK_BG[1], dtype=np.uint8)
    b = np.full((H, W), DARK_BG[2], dtype=np.uint8)
    overlay_arr = np.stack([r, g, b, alpha_2d], axis=2)
    overlay = Image.fromarray(overlay_arr, "RGBA")
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _light_overlay(img: Image.Image, alpha: int = 130) -> Image.Image:
    """Uniform dark overlay at lower opacity — lets bg breathe for inset layout."""
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (DARK_BG[0], DARK_BG[1], DARK_BG[2], alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _circular_inset(base: Image.Image, inset: Image.Image,
                    cx: int, cy: int, radius: int,
                    border_px: int = 3) -> Image.Image:
    """Composite inset as a feathered circle at (cx, cy)."""
    size = radius * 2
    inset_r = inset.resize((size, size), _RESAMPLE)

    # Feathered circular mask
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius // 10))

    inset_rgba = inset_r.convert("RGBA")
    inset_rgba.putalpha(mask)

    base_rgba = base.convert("RGBA")
    px, py = cx - radius, cy - radius
    base_rgba.paste(inset_rgba, (px, py), inset_rgba)

    # Accent ring
    draw = ImageDraw.Draw(base_rgba)
    draw.ellipse([px, py, px + size, py + size],
                 outline=ACCENT, width=border_px)

    return base_rgba.convert("RGB")


def _fallback_bg(W: int, H: int) -> Image.Image:
    img = Image.new("RGB", (W, H), DARK_BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 4], fill=ACCENT)
    draw.rectangle([0, H - 4, W, H], fill=BORDER)
    return img


# ── Text block (shared by all layouts) ───────────────────────────────────────

def _draw_card_text(draw: ImageDraw.ImageDraw, concept: dict,
                    x: int, y: int, W: int, H: int,
                    text_w: int, brand: bool = False) -> None:
    """Draw field tag, name, divider, summary, source at (x, y) within text_w."""

    # Field tag
    field = (concept.get("field") or "").split("/")[0].strip().upper()
    domain = (concept.get("domain") or "").upper()
    tag = f"{field}  ·  {domain}" if domain and domain != field else field
    tf = _font(int(H * 0.026), bold=False)
    draw.text((x, y), tag, font=tf, fill=ACCENT)
    draw.rectangle([x, y + int(H * 0.038), x + int(W * 0.055), y + int(H * 0.040)],
                   fill=ACCENT)

    # Concept name
    name = (concept.get("name") or "UNKNOWN").upper()
    name_size = int(H * 0.092)
    nf = _font(name_size)
    while draw.textlength(name, font=nf) > text_w and name_size > 28:
        name_size -= 2
        nf = _font(name_size)
    name_y = y + int(H * 0.075)
    name_h = _wrap_draw(draw, name, nf, x, name_y, text_w, WHITE,
                        line_gap=int(name_size * 0.12))

    # Divider
    div_y = name_y + name_h + int(H * 0.025)
    draw.rectangle([x, div_y, x + text_w, div_y + 2], fill=ACCENT)

    # Summary
    summary = concept.get("summary") or ""
    sf = _font(int(H * 0.028), bold=False)
    _wrap_draw(draw, summary, sf, x, div_y + int(H * 0.04), text_w, WHITE, line_gap=9)

    # Source
    sources = concept.get("sources") or []
    if sources:
        src = "From: " + "  ·  ".join(Path(s).stem for s in sources[:2])
        src_font = _font(int(H * 0.020), bold=False)
        src_y = H - int(H * 0.075)
        draw.rectangle([x, src_y - int(H * 0.010), x + text_w, src_y - int(H * 0.008)],
                       fill=BORDER)
        draw.text((x, src_y), src, font=src_font, fill=DIM_TEXT)

    if brand:
        ff = _font(int(H * 0.018), bold=False)
        draw.text((x, H - int(H * 0.038)), "VELIKOV RESEARCH", font=ff, fill=DIM_TEXT)


# ── Compositors ───────────────────────────────────────────────────────────────

def composite_gradient(img: Image.Image, concept: dict, brand: bool = False) -> Image.Image:
    """Full-bleed Aurora image with left-to-right gradient for text legibility."""
    W, H = img.size
    img = _gradient_overlay_lr(img, left_alpha=215, right_alpha=35)
    draw = ImageDraw.Draw(img)
    pad = int(W * 0.06)
    # Text occupies left ~58% to stay readable
    text_w = int(W * 0.52)
    _draw_card_text(draw, concept, pad, int(H * 0.08), W, H, text_w, brand)
    return img


def composite_split(img: Image.Image, concept: dict, brand: bool = False) -> Image.Image:
    """Dark panel left, Aurora visible right, gradient fade between."""
    W, H = img.size
    img = _split_overlay(img, split_frac=0.54, fade_frac=0.13)
    draw = ImageDraw.Draw(img)

    # Thin accent line at split edge
    split_x = int(W * 0.54)
    draw.rectangle([split_x, int(H * 0.06), split_x + 1, H - int(H * 0.06)], fill=ACCENT)

    pad = int(W * 0.055)
    text_w = split_x - pad * 2
    _draw_card_text(draw, concept, pad, int(H * 0.08), W, H, text_w, brand)
    return img


def composite_inset(img: Image.Image, inset_img: Image.Image | None,
                    concept: dict, brand: bool = False) -> Image.Image:
    """Lighter full-bleed overlay, text full-width, circular inset upper-right."""
    W, H = img.size
    img = _light_overlay(img, alpha=145)

    # Composite inset circle
    if inset_img is not None:
        radius = int(W * 0.14)
        cx = W - int(W * 0.10) - radius
        cy = int(H * 0.12) + radius
        img = _circular_inset(img, inset_img, cx, cy, radius)

    draw = ImageDraw.Draw(img)
    pad = int(W * 0.06)
    # Right margin keeps clear of inset
    inset_reserve = int(W * 0.32) if inset_img else 0
    text_w = W - pad * 2 - inset_reserve
    _draw_card_text(draw, concept, pad, int(H * 0.08), W, H, text_w, brand)
    return img


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run(
    query: str,
    layout: str = "gradient",
    out_path: Path | None = None,
    aurora_bg: Path | None = None,
    aurora_inset: Path | None = None,
    size: tuple[int, int] = (1920, 1080),
    skip_aurora: bool = False,
    brand: bool = False,
) -> Path:
    _setup_palette(brand)
    W, H = size
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower())[:40]
    if out_path is None:
        out_path = OUT_DIR / f"{slug}_{layout}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    concept = query_kb(query)
    if not concept:
        sys.exit(f"[kb] no concept found for '{query}' — aborting.")
    print(f"[kb] → {concept['name']}")

    # Find KB-extracted source images
    kb_imgs = find_kb_images(concept)

    # Background — pass KB images as Aurora reference to ground the visual
    resolved_bg: Path | None = None
    if aurora_bg and Path(aurora_bg).exists():
        resolved_bg = Path(aurora_bg)
    elif not skip_aurora:
        bg_path = out_path.with_suffix(".bg.png")
        if _aurora(bg_prompt(concept, layout), bg_path, reference_images=kb_imgs[:1]):
            resolved_bg = bg_path

    if resolved_bg:
        img = Image.open(resolved_bg).convert("RGB").resize((W, H), _RESAMPLE)
        if brand and _HAS_BRAND:
            img = apply_color_lut(img)   # type: ignore[possibly-undefined]
            img = apply_vignette(img)    # type: ignore[possibly-undefined]
            img = apply_grain(img, amount=3.0)  # type: ignore[possibly-undefined]
    else:
        print("[bg] using fallback dark background", file=sys.stderr)
        img = _fallback_bg(W, H)

    # Inset: figures used directly; screenshots only guide Aurora; fallback = pure prompt
    inset_img: Image.Image | None = None
    if layout == "inset":
        if aurora_inset and Path(aurora_inset).exists():
            inset_img = Image.open(aurora_inset).convert("RGB")
            print("[inset] using supplied image")
        else:
            figures = [p for p in kb_imgs if re.match(r"page-\d{4}-img-\d+_.+", p.name)]
            screenshots = [p for p in kb_imgs if p not in figures]
            if figures:
                # Real extracted figure — use directly
                inset_img = Image.open(figures[0]).convert("RGB")
                print(f"[inset] using KB figure: {figures[0].name}")
            elif not skip_aurora:
                # Screenshots are text-heavy — use as Aurora reference, not direct
                refs = screenshots[:1]
                inset_path = out_path.with_suffix(".inset.png")
                if _aurora(inset_prompt(concept), inset_path, reference_images=refs):
                    inset_img = Image.open(inset_path).convert("RGB")

    # Composite
    if layout == "split":
        img = composite_split(img, concept, brand=brand)
    elif layout == "inset":
        img = composite_inset(img, inset_img, concept, brand=brand)
    else:
        img = composite_gradient(img, concept, brand=brand)

    img.save(out_path, "JPEG", quality=92)
    print(f"[done] → {out_path}")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Generate knowledge cards from KB concepts")
    ap.add_argument("--query", required=True)
    ap.add_argument("--layout", choices=LAYOUTS, default="gradient",
                    help="Visual layout: gradient | split | inset (default: gradient)")
    ap.add_argument("--out")
    ap.add_argument("--aurora-bg", help="Reuse existing background, skip generation")
    ap.add_argument("--aurora-inset", help="Reuse existing inset image (inset layout)")
    ap.add_argument("--size", default="1920x1080", metavar="WxH")
    ap.add_argument("--skip-aurora", action="store_true")
    ap.add_argument("--brand", action="store_true")
    args = ap.parse_args()

    try:
        W, H = map(int, args.size.lower().split("x"))
    except ValueError:
        ap.error(f"--size must be WxH, got: {args.size}")

    run(
        query=args.query,
        layout=args.layout,
        out_path=Path(args.out) if args.out else None,
        aurora_bg=Path(args.aurora_bg) if args.aurora_bg else None,
        aurora_inset=Path(args.aurora_inset) if args.aurora_inset else None,
        size=(W, H),
        skip_aurora=args.skip_aurora,
        brand=args.brand,
    )


if __name__ == "__main__":
    main()
