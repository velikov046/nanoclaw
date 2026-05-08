#!/usr/bin/env python3
"""Drive grok.com / x.com Grok Imagine via Playwright with a persistent profile.

Pattern: persistent Chromium profile dir keeps the X login session, so we only
log in manually once. Subsequent runs reuse cookies and run quickly.

First run: open headed, prompt user to log in (if not already), then submit a
test prompt. Captures screenshots at each step into the profile dir for
debugging when selectors drift.

Args:
  --prompt <text>           the imagine prompt
  --out <path>              output PNG path (defaults to /tmp/grok-<timestamp>.png)
  --profile-dir <path>      override default profile (default: data/sessions/velikov/grok-browser-profile)
  --headless                run without UI (only works after first manual login)
  --debug-shots             save step-by-step screenshots into the profile dir
  --timeout <sec>           overall timeout (default 240s)
  --reference-image <path>  attach an image as a build-from reference (Aurora img2img).
                            Pass the same reference across many calls for character consistency
                            across YouTube beat images.
  --mode {image,video}      output type. 'image' = chat-root composer (fast, default).
                            'video' = /imagine page Video toggle (Aurora video, ~6s mp4).
  --resolution {480p,720p}  video mode only. Default 720p.
  --duration {6s,10s}       video mode only. Default 6s.

Run via the venv:
  /home/aurellian/nanoclaw/tools/.playwright-venv/bin/python3 \\
    /home/aurellian/nanoclaw/tools/grok_imagine.py \\
    --prompt "dark cinematic conspiratorial..." --debug-shots
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, BrowserContext

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "data" / "sessions" / "velikov" / "grok-browser-profile"

GROK_URL = "https://grok.com/"
GROK_IMAGINE_URL = "https://grok.com/imagine"

# OneTrust cookie consent banner — fresh profile dirs hit this even with our
# imported cookies (the banner state lives in localStorage, not the cookie jar).
# Dismiss it once at startup so it doesn't intercept Video-radio / Submit clicks.
ONETRUST_REJECT_BTN = "#onetrust-reject-all-handler"

# /imagine page selectors (probed live 2026-05-08).
# Mode radiogroup is always present:
#   <button role="radio">Image</button> / <button role="radio">Video</button>
#     (parent has aria-label="Generation mode")
# IMPORTANT: the Speed/Quality radios that *also* appear in the DOM live in
# `aria-label="Image generation speed"` and apply ONLY in Image mode. Don't
# confuse them with the video controls — they vanish when Video is selected.
# After Video is selected, two new radiogroups mount:
#   - "Video resolution": <button role="radio">480p</button> / <button role="radio">720p</button>
#   - "Video duration":   <button role="radio">6s</button>   / <button role="radio">10s</button>
#
# Prompt input: <div contenteditable="true" class="tiptap ProseMirror ..."> (TipTap;
#               fill() doesn't work — use click + keyboard.type).
# Submit: <button aria-label="Submit" type="submit"> (disabled until prompt non-empty).
IMAGINE_VIDEO_RADIO = 'button[role="radio"]:has-text("Video")'
IMAGINE_IMAGE_RADIO = 'button[role="radio"]:has-text("Image")'
IMAGINE_RESOLUTION_720 = '[aria-label="Video resolution"] button[role="radio"]:has-text("720p")'
IMAGINE_RESOLUTION_480 = '[aria-label="Video resolution"] button[role="radio"]:has-text("480p")'
IMAGINE_DURATION_6 = '[aria-label="Video duration"] button[role="radio"]:has-text("6s")'
IMAGINE_DURATION_10 = '[aria-label="Video duration"] button[role="radio"]:has-text("10s")'
IMAGINE_PROMPT_INPUT = 'div.tiptap[contenteditable="true"], div.ProseMirror[contenteditable="true"]'
IMAGINE_SUBMIT_BTN = 'button[aria-label="Submit"][type="submit"]'

# Heuristic selectors. UI changes regularly so order from most-specific to most-general.
PROMPT_INPUT_SELECTORS = [
    'textarea[placeholder*="What" i]',
    'textarea[placeholder*="Ask" i]',
    'textarea[placeholder*="message" i]',
    '[placeholder*="What" i]',          # any tag, e.g. contenteditable div
    'div[contenteditable="true"][role="textbox"]',
    '[role="textbox"]',
    'textarea',
    '[contenteditable="true"]',
]
# Combined selector used to wait for input mount — avoids racing the SPA's hydration.
PROMPT_INPUT_WAIT_SELECTOR = (
    'textarea, [contenteditable="true"], [role="textbox"]'
)

# An "Imagine" toggle / mode switcher. May not exist if image gen is the default
# behaviour for an explicit "imagine X" prompt.
IMAGINE_TOGGLE_SELECTORS = [
    'button:has-text("Imagine")',
    'button:has-text("Image")',
    '[aria-label*="Imagine" i]',
    '[role="tab"]:has-text("Imagine")',
]

# File-upload affordance. Probed live 2026-05-08 on grok.com:
#   <input class="hidden" multiple type="file" name="files">
# is in the DOM as a hidden input, with an `aria-label="Attach"` button next to it.
# Playwright's `set_input_files` works on hidden inputs without needing a click,
# so we target the input directly and ignore the button.
FILE_INPUT_SELECTORS = [
    'input[type="file"][name="files"]',
    'input[type="file"][accept*="image" i]',
    'input[type="file"]',
]


# Login state markers — if any of these are visible, user isn't authenticated.
# Use tag-agnostic text matches because Grok renders these as styled <a> not <button>.
LOGIN_GATE_SELECTORS = [
    'a[href*="/login"]',
    'a[href*="i/flow/login"]',
    ':text-is("Sign in")',
    ':text-is("Sign up")',
    ':text-is("Log in")',
]


def _shot(page: Page, profile_dir: Path, label: str, enabled: bool):
    if not enabled:
        return
    shots = profile_dir / "_debug-shots"
    shots.mkdir(parents=True, exist_ok=True)
    out = shots / f"{int(time.time())}-{label}.png"
    try:
        page.screenshot(path=str(out), full_page=True)
        print(f"  [shot] {out}", file=sys.stderr)
    except Exception as e:
        print(f"  [shot-failed] {label}: {e}", file=sys.stderr)


def _try_selectors(page: Page, selectors: list) -> Optional[str]:
    """Return the first selector that matches a visible element, or None."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                return sel
        except (PWTimeout, Exception):
            continue
    return None


def detect_login_state(page: Page) -> bool:
    """True = logged in (no login-gate found), False = login required."""
    for sel in LOGIN_GATE_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=1500):
                return False
        except (PWTimeout, Exception):
            continue
    return True


# Hostnames / path fragments that are never the generated image (cookie banners,
# CDN logos, static UI). Block-listed so a slow-loading UI sprite can't win the
# "first stable" race against the actual generation.
DENY_URL_FRAGMENTS = (
    "cookielaw.org",
    "imagine-public.x.ai",   # public showcase gallery — never the user's own generation
    "/_thumbnail.jpg",       # gallery-video thumbnails on the showcase grid
    "_thumbnail.jpg",
    "favicon",
    ".svg",
)


def load_and_apply_cookies(context: BrowserContext, cookies_path: Path) -> int:
    """Read a JSON cookie export (Cookie-Editor / EditThisCookie / similar) and
    inject into the persistent context. Returns count loaded.

    Browser extensions export cookies in slightly different shapes; this normalises
    the common variants to Playwright's expected schema. Field details that matter:
    - `expirationDate` (float seconds) → `expires` (int seconds).
    - `sameSite` values like `no_restriction` / `lax` / `unspecified` →
      `None` / `Lax` / dropped.
    - Drop fields Playwright rejects (`hostOnly`, `session`, `storeId`, etc).
    """
    raw = json.loads(cookies_path.read_text())
    if isinstance(raw, dict) and "cookies" in raw:
        raw = raw["cookies"]
    if not isinstance(raw, list):
        raise ValueError(f"{cookies_path}: expected a JSON array or {{cookies: [...]}} envelope")

    accepted_fields = {"name", "value", "domain", "path", "expires",
                       "httpOnly", "secure", "sameSite", "url"}
    normalized = []
    for c in raw:
        c = dict(c)
        if "expirationDate" in c and "expires" not in c:
            try:
                c["expires"] = int(c["expirationDate"])
            except (TypeError, ValueError):
                pass
        ss = c.get("sameSite")
        if isinstance(ss, str):
            sl = ss.lower()
            if sl in ("no_restriction", "none"):
                c["sameSite"] = "None"
            elif sl == "lax":
                c["sameSite"] = "Lax"
            elif sl == "strict":
                c["sameSite"] = "Strict"
            else:
                c.pop("sameSite", None)
        clean = {k: v for k, v in c.items() if k in accepted_fields}
        if "name" in clean and "value" in clean and ("domain" in clean or "url" in clean):
            normalized.append(clean)
    context.add_cookies(normalized)
    return len(normalized)


def snapshot_image_srcs(page: Page) -> set[str]:
    """Capture every currently-loaded <img> src so we can diff against post-submit state."""
    try:
        return set(page.evaluate(
            """() => Array.from(document.querySelectorAll('img'))
                .map(i => i.currentSrc || i.src)
                .filter(s => s && (s.startsWith('http') || s.startsWith('blob:')))
            """
        ))
    except Exception:
        return set()


def wait_for_image(
    page: Page,
    profile_dir: Path,
    debug: bool,
    timeout_s: int = 180,
    exclude_srcs: Optional[set] = None,
    min_wait_s: float = 10.0,
) -> Optional[str]:
    """Poll for a NEW generated image (not present before submission). Return its src.

    Grok renders generated images as <img> with ai-generated content URLs. We
    diff against the pre-submit set of img srcs so the homepage showcase grid,
    Imagine landing-page user-gallery, and cookie banner logos can't win the
    "first stable" race against the real Aurora result.

    `min_wait_s` enforces a floor on how soon a candidate can be returned —
    Aurora generations take 15-30s, so anything that "appears" within seconds
    of submission is almost certainly a stale render from the prior view.
    """
    exclude_srcs = exclude_srcs or set()
    start = time.time()
    last_seen: dict[str, float] = {}
    while time.time() - start < timeout_s:
        try:
            urls = page.evaluate(
                """() => Array.from(document.querySelectorAll('img'))
                    .map(i => ({src: i.currentSrc || i.src, w: i.naturalWidth, h: i.naturalHeight}))
                    .filter(o => o.src && (o.src.startsWith('http') || o.src.startsWith('blob:'))
                                && o.w >= 256 && o.h >= 256)
                """
            )
        except Exception:
            urls = []
        for o in urls:
            src = o["src"]
            if src in exclude_srcs:
                continue
            if any(frag in src for frag in DENY_URL_FRAGMENTS):
                continue
            now = time.time()
            elapsed_total = now - start
            if src not in last_seen:
                last_seen[src] = now
                print(f"  [img] candidate detected w={o['w']} h={o['h']} url={src[:120]}", file=sys.stderr)
            elif now - last_seen[src] > 2.5 and elapsed_total > min_wait_s:
                # Stable for >2.5s AND past the min-wait floor — likely a real
                # generation, not a stale render carried over from prior view.
                _shot(page, profile_dir, "image-found", debug)
                return src
        time.sleep(1)
    _shot(page, profile_dir, "image-timeout", debug)
    return None


def dismiss_onetrust_banner(page: Page) -> bool:
    """Dismiss the OneTrust cookie consent banner if it's visible.

    The banner intercepts pointer events on /imagine, which blocks the Video
    radio and Submit clicks. Banner state lives in localStorage, so a fresh
    profile dir always sees it even with our imported cookies. Reject-all is
    sufficient — no consent is required for image/video gen.
    """
    try:
        btn = page.locator(ONETRUST_REJECT_BTN).first
        if btn.is_visible(timeout=2_000):
            btn.click()
            print("  [banner] dismissed OneTrust consent", file=sys.stderr)
            time.sleep(0.5)
            return True
    except (PWTimeout, Exception):
        pass
    return False


def _select_radio(page: Page, selector: str, label: str,
                   profile_dir: Path, debug: bool, settle_s: float = 0.4) -> bool:
    """Click a radio button and verify aria-checked flipped to true.

    Returns True on success. Non-fatal on failure — Aurora will fall back to
    whatever default was already selected. Used for video resolution + duration
    toggles which mount only after Video is selected.
    """
    try:
        radio = page.locator(selector).first
        radio.wait_for(state="visible", timeout=15_000)
        radio.click(timeout=5_000)
        time.sleep(settle_s)
        if radio.get_attribute("aria-checked") == "true":
            print(f"  [video] {label} selected", file=sys.stderr)
            return True
        print(
            f"  [video] {label} click didn't flip aria-checked; "
            f"staying on whatever was default",
            file=sys.stderr,
        )
        return False
    except (PWTimeout, Exception) as e:
        slug = label.lower().replace(" ", "-")
        _shot(page, profile_dir, f"video-{slug}-click-failed", debug)
        print(
            f"  [video] couldn't select {label} ({e}); falling back to default",
            file=sys.stderr,
        )
        return False


def run_video_flow(
    context: BrowserContext,
    page: Page,
    prompt: str,
    out_path: Path,
    profile_dir: Path,
    debug: bool,
    timeout_s: int,
    resolution: str = "720p",
    duration: str = "6s",
) -> None:
    """Drive grok.com/imagine to generate a video and download the mp4.

    Different surface from the chat-root image flow:
    - Lives at /imagine (not /).
    - Image/Video radio toggle (default Image; we click Video).
    - Speed/Quality radio toggle (default Speed; pass quality=True to switch).
    - Prompt input is TipTap ProseMirror — needs keyboard.type, not fill().
    - Submit button stays disabled until the prompt is non-empty.
    - Output is a <video> element with mp4 src; download via context.request.
    """
    print(f"[video] navigating to {GROK_IMAGINE_URL}", file=sys.stderr)
    page.goto(GROK_IMAGINE_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass
    _shot(page, profile_dir, "video-01-loaded", debug)

    dismiss_onetrust_banner(page)

    # Toggle Video mode. Verify aria-checked flipped to confirm the click landed.
    video_radio = page.locator(IMAGINE_VIDEO_RADIO).first
    video_radio.click(timeout=10_000)
    time.sleep(0.5)
    if video_radio.get_attribute("aria-checked") != "true":
        _shot(page, profile_dir, "video-toggle-failed", debug)
        raise RuntimeError("Video radio click didn't flip aria-checked — UI may have shifted.")
    print("  [video] Video mode selected", file=sys.stderr)

    # The Video-mode resolution + duration radiogroups only mount AFTER the
    # Video radio is selected. Toggle them only when the desired option isn't
    # already the default. Failure is non-fatal — we still get a video, just
    # at the default 480p / 6s.
    if resolution == "720p":
        _select_radio(page, IMAGINE_RESOLUTION_720, "720p resolution",
                       profile_dir, debug)
    if duration == "10s":
        _select_radio(page, IMAGINE_DURATION_10, "10s duration",
                       profile_dir, debug)

    _shot(page, profile_dir, "video-02-mode-set", debug)

    # ProseMirror needs typed input, not fill(). Click to focus, then type.
    prompt_el = page.locator(IMAGINE_PROMPT_INPUT).first
    prompt_el.click()
    time.sleep(0.2)
    page.keyboard.type(prompt, delay=10)
    _shot(page, profile_dir, "video-03-prompt-typed", debug)

    # Snapshot pre-submit <video> srcs so the diff excludes the gallery thumbnails.
    pre_existing = snapshot_video_srcs(page)
    print(f"  [video] {len(pre_existing)} existing video src(s) before submit", file=sys.stderr)

    # Submit becomes enabled once the prompt is non-empty. Wait for it, then click.
    submit = page.locator(IMAGINE_SUBMIT_BTN).first
    for _ in range(30):
        if not submit.is_disabled():
            break
        time.sleep(0.2)
    else:
        _shot(page, profile_dir, "video-submit-stuck-disabled", debug)
        raise RuntimeError("Submit button never became enabled after typing prompt.")
    submit.click()
    _shot(page, profile_dir, "video-04-submitted", debug)
    print(f"[video] submitted; waiting up to {timeout_s}s for generation...", file=sys.stderr)

    src = wait_for_video(page, profile_dir, debug, timeout_s, exclude_srcs=pre_existing)
    if not src:
        raise RuntimeError("Timed out waiting for generated video.")

    print(f"  [video] src: {src[:120]}", file=sys.stderr)
    if not download_image(context, src, out_path):
        raise RuntimeError(f"Failed to download generated video from {src}")


def snapshot_video_srcs(page: Page) -> set[str]:
    """Capture every currently-loaded <video> src for pre/post-submit diffing."""
    try:
        return set(page.evaluate(
            """() => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.src || v.querySelector('source')?.src)
                .filter(s => s && (s.startsWith('http') || s.startsWith('blob:')))
            """
        ))
    except Exception:
        return set()


def wait_for_video(
    page: Page,
    profile_dir: Path,
    debug: bool,
    timeout_s: int,
    exclude_srcs: Optional[set] = None,
    min_wait_s: float = 15.0,
) -> Optional[str]:
    """Poll for a NEW <video> src not present before submission. Returns the src.

    Aurora video gen typically 30-90s; min_wait_s floor avoids returning a
    just-mounted gallery thumbnail. Stable-for-2.5s window is sufficient since
    once the mp4 src lands it doesn't churn.
    """
    exclude_srcs = exclude_srcs or set()
    start = time.time()
    last_seen: dict[str, float] = {}
    while time.time() - start < timeout_s:
        try:
            srcs = page.evaluate(
                """() => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || v.querySelector('source')?.src)
                    .filter(s => s && (s.startsWith('http') || s.startsWith('blob:')))
                """
            )
        except Exception:
            srcs = []
        for src in srcs:
            if src in exclude_srcs:
                continue
            if any(frag in src for frag in DENY_URL_FRAGMENTS):
                continue
            now = time.time()
            elapsed_total = now - start
            if src not in last_seen:
                last_seen[src] = now
                print(f"  [vid] candidate detected url={src[:120]}", file=sys.stderr)
            elif now - last_seen[src] > 2.5 and elapsed_total > min_wait_s:
                _shot(page, profile_dir, "video-found", debug)
                return src
        time.sleep(1)
    _shot(page, profile_dir, "video-timeout", debug)
    return None


def attach_reference_image(page: Page, image_path: Path, profile_dir: Path, debug: bool) -> None:
    """Attach a local image to the composer for img2img / reference-conditioned gen.

    Sets files directly on the hidden `<input type="file">` (probed 2026-05-08).
    The composer renders a preview thumbnail asynchronously after this fires —
    we don't need to wait for it explicitly because `submit_prompt` snapshots
    pre-submit imgs so the thumbnail is already in `exclude_srcs`.

    Raises:
        RuntimeError if no file input is found in the DOM.
    """
    if not image_path.exists():
        raise RuntimeError(f"Reference image not found: {image_path}")
    sel = _try_selectors(page, FILE_INPUT_SELECTORS) or FILE_INPUT_SELECTORS[-1]
    # Hidden inputs aren't "visible" so _try_selectors may return None — the
    # fallback locator below works regardless of visibility.
    try:
        page.locator(sel).first.set_input_files(str(image_path))
    except Exception as e:
        _shot(page, profile_dir, "attach-failed", debug)
        raise RuntimeError(f"set_input_files failed on {sel}: {e}") from e
    print(f"  [attach] uploaded {image_path.name} via {sel}", file=sys.stderr)
    # Brief settle so the preview thumbnail mounts before we snapshot pre-submit imgs.
    time.sleep(2)
    _shot(page, profile_dir, "reference-attached", debug)


def submit_prompt(page: Page, prompt: str, profile_dir: Path, debug: bool) -> set[str]:
    """Type the prompt, click Imagine toggle, snapshot the gallery, press Enter.

    Returns the set of img srcs visible on the page IMMEDIATELY BEFORE Enter is
    pressed — so the caller can diff against post-submit state. Snapshotting
    here (not in main) catches images that loaded as a side-effect of the
    Imagine-toggle navigation (the user's prior creations rendered on the
    /imagine landing page).
    """
    # Wait for ANY editable input to mount before probing — the SPA can take a
    # moment to hydrate, especially when post-login toasts (e.g. "Connectors
    # are now available") are mutating the DOM.
    try:
        page.wait_for_selector(PROMPT_INPUT_WAIT_SELECTOR, timeout=10_000, state="visible")
    except PWTimeout:
        pass
    sel = _try_selectors(page, PROMPT_INPUT_SELECTORS)
    if not sel:
        _shot(page, profile_dir, "no-input", debug)
        raise RuntimeError(
            "Could not locate the prompt input. Layout may have changed. "
            "Inspect _debug-shots/no-input.png for the current DOM."
        )
    # Auto-prefix "imagine " so Grok's natural-language router invokes Aurora,
    # rather than us clicking a nav link (which navigates to /imagine and
    # destroys our typed prompt). The trigger phrases Grok recognises include
    # "imagine ", "draw ", "create an image of " — "imagine" is canonical here.
    routed_prompt = prompt
    if not any(prompt.lower().lstrip().startswith(t) for t in ("imagine ", "draw ", "create an image", "generate an image")):
        routed_prompt = f"imagine {prompt}"
        print(f"  [prefix] auto-prefixed prompt with 'imagine '", file=sys.stderr)

    print(f"  [input] using selector: {sel}", file=sys.stderr)
    el = page.locator(sel).first
    el.click()
    el.fill(routed_prompt)
    _shot(page, profile_dir, "prompt-filled", debug)

    # Snapshot RIGHT before submission — captures whatever's currently on screen
    # (homepage cards, sidebar history thumbnails, etc.) so wait_for_image only
    # considers genuinely new images that appear after Enter.
    pre_existing = snapshot_image_srcs(page)
    print(f"  [pre-snapshot] {len(pre_existing)} image(s) before submit; will diff", file=sys.stderr)

    try:
        el.press("Enter")
    except Exception as e:
        print(f"  [submit-fallback] {e}; trying keyboard.press", file=sys.stderr)
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    _shot(page, profile_dir, "submitted", debug)
    return pre_existing


def download_image(context: BrowserContext, url: str, out_path: Path) -> bool:
    """Download via the same browser context so cookies + same-origin work."""
    try:
        resp = context.request.get(url, timeout=60_000)
        if resp.ok:
            out_path.write_bytes(resp.body())
            return True
        print(f"  [download] HTTP {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"  [download-failed] {e}", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE))
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--debug-shots", action="store_true")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument(
        "--login-only", action="store_true",
        help="Open headed and pause for manual login; don't submit a prompt.",
    )
    ap.add_argument(
        "--cookies-file", default="",
        help="Path to a JSON cookie export (e.g. from Cookie-Editor extension). "
             "Loaded into the context before navigating — bypasses Cloudflare's "
             "login-flow challenge by reusing an already-authenticated session.",
    )
    ap.add_argument(
        "--reference-image", default="",
        help="Path to a local image to attach as a build-from reference (Aurora "
             "img2img). Same reference across many calls = character consistency "
             "across e.g. YouTube beat images.",
    )
    ap.add_argument(
        "--mode", default="image", choices=["image", "video"],
        help="Output type. 'image' uses the chat-root composer (default, fast). "
             "'video' uses the /imagine page Video toggle (Aurora video gen, "
             "30-90s, mp4 output). Reference image not supported in video mode.",
    )
    # Video mode toggles. /imagine exposes "Video resolution" (480p / 720p)
    # and "Video duration" (6s / 10s) radiogroups after Video is selected.
    # Defaults match the higher-quality / shorter / cheaper combo: 720p, 6s.
    ap.add_argument(
        "--resolution", choices=["480p", "720p"], default="720p",
        help="Video mode: output resolution (default 720p).",
    )
    ap.add_argument(
        "--duration", choices=["6s", "10s"], default="6s",
        help="Video mode: clip duration (default 6s; 10s costs more quota).",
    )
    args = ap.parse_args()

    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    default_ext = "mp4" if args.mode == "video" else "png"
    out_path = Path(args.out) if args.out else Path(f"/tmp/grok-{int(time.time())}.{default_ext}")

    if args.mode == "video" and args.reference_image:
        print("ERROR: --reference-image is not supported in --mode video.", file=sys.stderr)
        sys.exit(2)

    print(f"profile: {profile_dir}", file=sys.stderr)
    print(f"output:  {out_path}", file=sys.stderr)

    with sync_playwright() as pw:
        # launch_persistent_context bundles browser+context so cookies persist
        # naturally between runs without manual storage_state plumbing.
        launch_kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
            args=[
                # Cuts the most obvious headless-Chromium fingerprints. Won't beat
                # serious anti-bot, but helps against basic checks.
                "--disable-blink-features=AutomationControlled",
                # WSL2 + nanoclaw containers: default sandbox/shm-tmpfs combo
                # causes silent renderer crashes on heavyweight SPAs.
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Stella's long-running agent container hit
                # `chrome_crashpad_handler: --database is required` +
                # `recvmsg: Connection reset by peer (104)` on launch — the
                # crashpad handler subprocess fails to initialise inside a
                # nanoclaw container. We don't need crash reports anyway.
                "--disable-crash-reporter",
                "--disable-breakpad",
            ],
        )
        # Honor an explicit chromium binary path — required inside nanoclaw
        # containers, where Playwright's bundled chromium isn't present but
        # /usr/bin/chromium is installed via apt. The Dockerfile sets this env
        # var; on host it's unset and Playwright finds its own bundled binary.
        exec_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        if exec_path:
            launch_kwargs["executable_path"] = exec_path
        context = pw.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            if args.cookies_file:
                cookies_path = Path(args.cookies_file).expanduser().resolve()
                if not cookies_path.exists():
                    print(f"ERROR: --cookies-file not found: {cookies_path}", file=sys.stderr)
                    sys.exit(2)
                count = load_and_apply_cookies(context, cookies_path)
                print(f"  [cookies] loaded {count} cookie(s) from {cookies_path}", file=sys.stderr)

            if args.mode == "video":
                # Video mode has its own self-contained flow. It still relies on
                # the cookie-injected SuperGrok session above, but uses a
                # different page (/imagine) with different selectors.
                run_video_flow(context, page, args.prompt, out_path, profile_dir,
                               args.debug_shots, args.timeout,
                               resolution=args.resolution, duration=args.duration)
                print(f"OK wrote {out_path} ({out_path.stat().st_size} bytes)")
                return

            print(f"[1/4] navigating to {GROK_URL}", file=sys.stderr)
            page.goto(GROK_URL, wait_until="domcontentloaded", timeout=60_000)
            _shot(page, profile_dir, "01-loaded", args.debug_shots)

            # Login gate detection. Wait for the SPA to settle — the Sign in/up
            # buttons render after a JS hydration step, missing them is a known
            # foot-gun.
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass
            time.sleep(1)
            logged_in = detect_login_state(page)
            print(f"[2/4] login state: {'OK' if logged_in else 'NEEDS MANUAL LOGIN'}", file=sys.stderr)
            if not logged_in or args.login_only:
                if args.headless:
                    print(
                        "ERROR: not logged in and running headless. Re-run without "
                        "--headless once to authenticate, then headless thereafter.",
                        file=sys.stderr,
                    )
                    sys.exit(2)
                print(
                    "\n  → A browser window is open on your desktop. Please sign in via "
                    "X in that window. The script is polling for sign-in (10 min timeout).",
                    file=sys.stderr,
                )
                # Poll instead of blocking on stdin — works whether or not we have a
                # tty. Re-checks login state every 2s for up to 10 minutes.
                login_deadline = time.time() + 600
                while time.time() < login_deadline:
                    time.sleep(2)
                    if detect_login_state(page):
                        print("  → sign-in detected.", file=sys.stderr)
                        break
                else:
                    print("ERROR: sign-in not completed within 10 min — exiting.", file=sys.stderr)
                    sys.exit(2)
                _shot(page, profile_dir, "02-after-manual-login", args.debug_shots)
                if args.login_only:
                    print("login-only mode — exiting after login.", file=sys.stderr)
                    return

            if args.reference_image:
                ref_path = Path(args.reference_image).expanduser().resolve()
                print(f"[3a/4] attaching reference image: {ref_path}", file=sys.stderr)
                attach_reference_image(page, ref_path, profile_dir, args.debug_shots)

            print(f"[3/4] submitting prompt: {args.prompt[:80]}{'...' if len(args.prompt)>80 else ''}",
                  file=sys.stderr)
            pre_existing = submit_prompt(page, args.prompt, profile_dir, args.debug_shots)

            print(f"[4/4] waiting up to {args.timeout}s for the generated image...", file=sys.stderr)
            url = wait_for_image(page, profile_dir, args.debug_shots,
                                 timeout_s=args.timeout, exclude_srcs=pre_existing)
            if not url:
                print("ERROR: timed out waiting for generated image.", file=sys.stderr)
                sys.exit(3)

            print(f"  [image-url] {url}", file=sys.stderr)

            # Download via the browser context (preserves auth cookies for same-origin).
            ok = download_image(context, url, out_path)
            if not ok:
                # Last-resort: screenshot the IMG element directly.
                try:
                    img_el = page.locator(f'img[src="{url}"]').first
                    img_el.screenshot(path=str(out_path))
                    ok = True
                    print("  [download] fell back to element screenshot", file=sys.stderr)
                except Exception as e:
                    print(f"  [download] element-screenshot also failed: {e}", file=sys.stderr)
                    sys.exit(4)

            print(f"OK wrote {out_path} ({out_path.stat().st_size} bytes)")
        finally:
            context.close()


if __name__ == "__main__":
    main()
