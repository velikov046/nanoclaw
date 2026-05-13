"""
gen_infographic.py — Render infographics from a manually-written spec JSON.

Aurora provides the atmospheric background. Pillow composites all layout and
real data on top. Agent or human writes the spec; this tool renders it.

For KB-auto-extracted knowledge cards, use gen_card.py instead.

Templates:
  stat_grid   — 2×N grid of stat panels (default)
  comparison  — two-column A vs B
  timeline    — vertical alternating event sequence
  flow        — top-to-bottom causal/process chain

Spec format (stat_grid example):
  {
    "template": "stat_grid",
    "title": "HANTAVIRUS",
    "subtitle": "The epidemic that never came",
    "sections": [
      {"label": "CASE FATALITY RATE", "value": "36%", "detail": "Among confirmed HPS cases."},
      ...
    ]
  }

Usage:
  python3 gen_infographic.py --spec layout.json
  python3 gen_infographic.py --spec layout.json --out result.jpg
  python3 gen_infographic.py --spec layout.json --skip-aurora   # fast test
  python3 gen_infographic.py --spec layout.json --brand         # Velikov palette
  python3 gen_infographic.py --spec layout.json --aurora-bg bg.png  # reuse bg
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
    from PIL import Image, ImageDraw, ImageFont
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

GROK_IMAGINE  = _TOOLS_DIR / "grok_imagine.py"
PLAYWRIGHT_PY = _TOOLS_DIR / ".playwright-venv/bin/python3"
OUT_DIR       = _TOOLS_DIR.parent / "groups/velikov/researcher/infographics"

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


TEMPLATES = ("stat_grid", "comparison", "timeline", "flow")

# ── Aurora ────────────────────────────────────────────────────────────────────

_COOKIE_CANDIDATES = [
    _TOOLS_DIR.parent / "groups/global/grok.com_cookies.json",
    _TOOLS_DIR.parent / "data/sessions/velikov/grok.com_cookies.json",
    Path("/workspace/global/grok.com_cookies.json"),
]


def _resolve_cookies() -> Path | None:
    env = os.environ.get("GROK_COOKIES_FILE")
    if env:
        return Path(env)
    for p in _COOKIE_CANDIDATES:
        if p.exists():
            return p
    return None


def generate_background(prompt: str, out_path: Path, timeout_s: int = 180) -> bool:
    if not PLAYWRIGHT_PY.exists():
        print(f"[aurora] playwright venv missing", file=sys.stderr)
        return False
    cookies = _resolve_cookies()
    if not cookies:
        print("[aurora] no grok.com cookies found", file=sys.stderr)
        return False
    profile_dir = Path(tempfile.mkdtemp(prefix="grok-infographic-", dir="/tmp"))
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
    print("[aurora] generating background …")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 60)
    if result.returncode != 0:
        print(f"[aurora] failed:\n{result.stderr[-600:]}", file=sys.stderr)
        return False
    print(f"[aurora] → {out_path}")
    return True


def build_aurora_prompt(title: str) -> str:
    return (
        f"Dark cinematic background texture for a '{title}' editorial infographic. "
        "Deep near-black with subtle teal-blue atmospheric depth, very faint geometric "
        "light rays or bokeh, slight vignette toward corners, premium editorial feel. "
        "NO grid lines, NO panels, NO boxes, NO text, NO numbers, NO labels. "
        "Pure atmospheric texture only. High resolution, 4K."
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
               max_w: int, fill, line_gap: int = 4) -> int:
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


def _header(draw: ImageDraw.ImageDraw, W: int, H: int, title: str, subtitle: str = "") -> int:
    header_h = int(H * 0.13)
    draw.rectangle([0, 0, W, header_h], fill=DARK_BG)
    draw.rectangle([0, header_h, W, header_h + 3], fill=ACCENT)
    size = int(H * 0.052)
    tf = _font(size)
    tw = draw.textlength(title, font=tf)
    while tw > W * 0.92 and size > 28:
        size -= 2
        tf = _font(size)
        tw = draw.textlength(title, font=tf)
    draw.text(((W - tw) // 2, int(H * 0.022)), title, font=tf, fill=ACCENT,
              stroke_width=2, stroke_fill=(0, 0, 0))
    if subtitle:
        sf = _font(int(H * 0.021), bold=False)
        sw = draw.textlength(subtitle, font=sf)
        draw.text(((W - sw) // 2, int(H * 0.082)), subtitle, font=sf, fill=DIM_TEXT)
    return header_h


def _footer(draw: ImageDraw.ImageDraw, W: int, H: int, brand: bool = False) -> None:
    footer_y = H - int(H * 0.044)
    draw.rectangle([0, footer_y, W, H], fill=(11, 11, 16))
    draw.rectangle([0, footer_y, W, footer_y + 2], fill=BORDER)
    if brand:
        ff = _font(int(H * 0.018), bold=False)
        draw.text((int(W * 0.025), footer_y + int(H * 0.012)),
                  "VELIKOV RESEARCH", font=ff, fill=DIM_TEXT)


def _fallback_bg(W: int, H: int) -> Image.Image:
    img = Image.new("RGB", (W, H), DARK_BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 4], fill=ACCENT)
    draw.rectangle([0, H - 4, W, H], fill=BORDER)
    return img


# ── Compositors ───────────────────────────────────────────────────────────────

def composite_stat_grid(img: Image.Image, spec: dict, brand: bool = False) -> Image.Image:
    W, H = img.size
    draw = ImageDraw.Draw(img)
    sections = spec.get("sections", [])
    header_h = _header(draw, W, H, spec.get("title", ""), spec.get("subtitle", ""))

    cols = 2
    n = len(sections)
    rows = max(1, (n + cols - 1) // cols)
    pad = int(W * 0.022)
    cell_w = (W - pad * (cols + 1)) // cols
    footer_reserve = int(H * 0.055)
    cell_h = (H - header_h - footer_reserve - pad * (rows + 1)) // rows
    df = _font(min(int(cell_h * 0.11), 18), bold=False)

    for i, sec in enumerate(sections):
        col = i % cols
        row = i // cols
        x0 = pad + col * (cell_w + pad)
        y0 = header_h + pad + row * (cell_h + pad)
        draw.rectangle([x0, y0, x0 + cell_w, y0 + cell_h], fill=PANEL_BG)
        draw.rectangle([x0, y0, x0 + cell_w, y0 + 3], fill=ACCENT)

        ix = x0 + int(cell_w * 0.055)
        iw = int(cell_w * 0.89)
        cy = y0 + int(cell_h * 0.10)

        label = sec.get("label", "")
        lf_sz = min(int(cell_h * 0.14), 22)
        lf_s = _font(lf_sz)
        while draw.textlength(label, font=lf_s) > iw and lf_sz > 10:
            lf_sz -= 1
            lf_s = _font(lf_sz)
        draw.text((ix, cy), label, font=lf_s, fill=ACCENT)
        cy += int(cell_h * 0.21)

        value = sec.get("value", "")
        vf_sz = min(int(cell_h * 0.22), 36)
        vf_s = _font(vf_sz)
        while draw.textlength(value, font=vf_s) > iw and vf_sz > 12:
            vf_sz -= 1
            vf_s = _font(vf_sz)
        draw.text((ix, cy), value, font=vf_s, fill=WHITE,
                  stroke_width=1, stroke_fill=(0, 0, 0))
        cy += int(cell_h * 0.30)
        _wrap_draw(draw, sec.get("detail", ""), df, ix, cy, iw, DIM_TEXT)

    _footer(draw, W, H, brand=brand)
    return img


def composite_comparison(img: Image.Image, spec: dict, brand: bool = False) -> Image.Image:
    W, H = img.size
    draw = ImageDraw.Draw(img)
    header_h = _header(draw, W, H, spec.get("title", ""))

    mid = W // 2
    draw.rectangle([mid - 2, header_h + 4, mid + 2, H - int(H * 0.05)], fill=ACCENT)

    pad = int(W * 0.028)
    col_y = header_h + int(H * 0.018)
    cf = _font(int(H * 0.036))
    draw.text((pad, col_y), spec.get("col_a", "A"), font=cf, fill=WHITE)
    draw.text((mid + pad, col_y), spec.get("col_b", "B"), font=cf, fill=WHITE)

    divider_y = col_y + int(H * 0.055)
    draw.rectangle([0, divider_y, W, divider_y + 2], fill=(45, 45, 58))

    item_font = _font(int(H * 0.024))
    detail_font = _font(int(H * 0.019), bold=False)
    item_spacing = int(H * 0.092)
    max_w = mid - pad * 2 - int(W * 0.01)

    for x_base, key in [(pad, "items_a"), (mid + pad, "items_b")]:
        y = divider_y + int(H * 0.018)
        for item in spec.get(key, []):
            draw.text((x_base, y), "▸ " + item.get("label", ""), font=item_font, fill=ACCENT)
            y += int(H * 0.032)
            h = _wrap_draw(draw, item.get("detail", ""), detail_font,
                           x_base + int(W * 0.008), y, max_w, DIM_TEXT)
            y += max(h, item_spacing - int(H * 0.032)) + int(H * 0.008)

    _footer(draw, W, H, brand=brand)
    return img


def composite_timeline(img: Image.Image, spec: dict, brand: bool = False) -> Image.Image:
    W, H = img.size
    draw = ImageDraw.Draw(img)
    header_h = _header(draw, W, H, spec.get("title", ""))

    events = spec.get("events", [])
    n = len(events)
    if not n:
        _footer(draw, W, H, brand=brand)
        return img

    spine_x = W // 2
    spine_y0 = header_h + int(H * 0.035)
    spine_y1 = H - int(H * 0.058)
    draw.rectangle([spine_x - 2, spine_y0, spine_x + 2, spine_y1], fill=ACCENT)

    event_h = (spine_y1 - spine_y0) // n
    lf_size = int(H * 0.026)
    df_size = int(H * 0.019)
    pad = int(W * 0.038)
    inner_pad_frac = 0.055

    for i, ev in enumerate(events):
        cy = spine_y0 + i * event_h + event_h // 2
        draw.ellipse([spine_x - 8, cy - 8, spine_x + 8, cy + 8], fill=ACCENT)

        if i % 2 == 0:
            conn_x1 = spine_x - int(W * 0.055)
            draw.rectangle([conn_x1, cy - 2, spine_x - 8, cy + 2], fill=ACCENT)
            px0, px1 = pad, conn_x1 - int(W * 0.008)
        else:
            conn_x0 = spine_x + int(W * 0.055)
            draw.rectangle([spine_x + 8, cy - 2, conn_x0, cy + 2], fill=ACCENT)
            px0, px1 = conn_x0 + int(W * 0.008), W - pad

        ph = int(event_h * 0.72)
        py0, py1 = cy - ph // 2, cy + ph // 2
        draw.rectangle([px0, py0, px1, py1], fill=PANEL_BG)
        draw.rectangle([px0, py0, px0 + 3, py1], fill=ACCENT)

        inner_off = int((px1 - px0) * inner_pad_frac)
        tx = px0 + inner_off
        tw_max = px1 - tx - inner_off

        lf = _font(lf_size)
        sz = lf_size
        label = ev.get("label", "")
        while draw.textlength(label, font=lf) > tw_max and sz > 10:
            sz -= 1
            lf = _font(sz)
        _wrap_draw(draw, label, lf, tx, py0 + int(ph * 0.09), tw_max, WHITE)
        _wrap_draw(draw, ev.get("detail", ""), _font(df_size, bold=False),
                   tx, py0 + int(ph * 0.50), tw_max, DIM_TEXT)

    _footer(draw, W, H, brand=brand)
    return img


def composite_flow(img: Image.Image, spec: dict, brand: bool = False) -> Image.Image:
    W, H = img.size
    draw = ImageDraw.Draw(img)
    header_h = _header(draw, W, H, spec.get("title", ""))

    steps = spec.get("steps", [])
    n = len(steps)
    if not n:
        _footer(draw, W, H, brand=brand)
        return img

    pad_x = int(W * 0.11)
    node_w = W - pad_x * 2
    y_start = header_h + int(H * 0.022)
    available_h = H - y_start - int(H * 0.058)
    arrow_frac = 0.28
    node_h = int(available_h / (n + (n - 1) * arrow_frac))
    arrow_h = int(node_h * arrow_frac)

    lf = _font(int(node_h * 0.24))
    df = _font(int(node_h * 0.16), bold=False)

    y = y_start
    for i, step in enumerate(steps):
        x0, y0, x1, y1 = pad_x, y, pad_x + node_w, y + node_h
        draw.rectangle([x0, y0, x1, y1], fill=PANEL_BG)
        draw.rectangle([x0, y0, x0 + 4, y1], fill=ACCENT)
        draw.rectangle([x1 - 4, y0, x1, y1], fill=ACCENT)
        draw.rectangle([x0, y0, x1, y0 + 3], fill=BORDER)
        ix = x0 + int(node_w * 0.04)
        iw = node_w - int(node_w * 0.08)
        draw.text((ix, y0 + int(node_h * 0.10)), step.get("label", ""), font=lf, fill=ACCENT)
        _wrap_draw(draw, step.get("detail", ""), df, ix, y0 + int(node_h * 0.44), iw, DIM_TEXT)
        y += node_h
        if i < n - 1:
            ax = W // 2
            ay0, ay1 = y + 5, y + arrow_h - 8
            draw.rectangle([ax - 2, ay0, ax + 2, ay1], fill=ACCENT)
            aw = 11
            draw.polygon([(ax, ay1 + aw), (ax - aw, ay1), (ax + aw, ay1)], fill=ACCENT)
            y += arrow_h

    _footer(draw, W, H, brand=brand)
    return img


COMPOSITORS = {
    "stat_grid": composite_stat_grid,
    "comparison": composite_comparison,
    "timeline": composite_timeline,
    "flow": composite_flow,
}


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run(
    spec: dict,
    out_path: Path | None = None,
    aurora_bg: Path | None = None,
    size: tuple[int, int] = (1920, 1080),
    skip_aurora: bool = False,
    brand: bool = False,
) -> Path:
    _setup_palette(brand)
    W, H = size
    title = spec.get("title", "infographic")
    template = spec.get("template", "stat_grid")
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower())[:40]
    if out_path is None:
        out_path = OUT_DIR / f"{slug}_{template}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Aurora background
    resolved_bg: Path | None = None
    if aurora_bg and aurora_bg.exists():
        resolved_bg = aurora_bg
    elif not skip_aurora:
        prompt = build_aurora_prompt(title)
        print(f"[aurora] {prompt[:100]}…")
        bg_path = out_path.with_suffix(".bg.png")
        if generate_background(prompt, bg_path):
            resolved_bg = bg_path

    if resolved_bg:
        img = Image.open(resolved_bg).convert("RGB").resize((W, H), _RESAMPLE)
        if brand and _HAS_BRAND:
            img = apply_color_lut(img)   # type: ignore[possibly-undefined]
            img = apply_vignette(img)    # type: ignore[possibly-undefined]
            img = apply_grain(img, amount=4.0)  # type: ignore[possibly-undefined]
    else:
        print("[bg] using fallback dark background", file=sys.stderr)
        img = _fallback_bg(W, H)

    compositor = COMPOSITORS.get(template, composite_stat_grid)
    img = compositor(img, spec, brand=brand)
    img.save(out_path, "JPEG", quality=92)
    print(f"[done] → {out_path}")
    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Render an infographic from a spec JSON file")
    ap.add_argument("--spec", required=True, metavar="JSON", help="Layout spec JSON file")
    ap.add_argument("--out", help="Output JPEG path")
    ap.add_argument("--aurora-bg", help="Use existing Aurora background, skip generation")
    ap.add_argument("--size", default="1920x1080", metavar="WxH")
    ap.add_argument("--skip-aurora", action="store_true")
    ap.add_argument("--brand", action="store_true",
                    help="Velikov amber palette + footer")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    try:
        W, H = map(int, args.size.lower().split("x"))
    except ValueError:
        ap.error(f"--size must be WxH, got: {args.size}")

    run(
        spec=spec,
        out_path=Path(args.out) if args.out else None,
        aurora_bg=Path(args.aurora_bg) if args.aurora_bg else None,
        size=(W, H),
        skip_aurora=args.skip_aurora,
        brand=args.brand,
    )


if __name__ == "__main__":
    main()
