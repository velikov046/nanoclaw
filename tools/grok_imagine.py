#!/usr/bin/env python3
"""Drive grok.com / x.com Grok Imagine via Playwright with a persistent profile.

Pattern: persistent Chromium profile dir keeps the X login session, so we only
log in manually once. Subsequent runs reuse cookies and run quickly.

First run: open headed, prompt user to log in (if not already), then submit a
test prompt. Captures screenshots at each step into the profile dir for
debugging when selectors drift.

Args:
  --prompt <text>     the imagine prompt
  --out <path>        output PNG path (defaults to /tmp/grok-<timestamp>.png)
  --profile-dir <path>  override default profile (default: data/sessions/velikov/grok-browser-profile)
  --headless          run without UI (only works after first manual login)
  --debug-shots       save step-by-step screenshots into the profile dir
  --timeout <sec>     overall timeout (default 240s)

Run via the venv:
  /home/aurellian/nanoclaw/tools/.playwright-venv/bin/python3 \\
    /home/aurellian/nanoclaw/tools/grok_imagine.py \\
    --prompt "dark cinematic conspiratorial..." --debug-shots
"""
import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page, BrowserContext

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "data" / "sessions" / "velikov" / "grok-browser-profile"

GROK_URL = "https://grok.com/"

# Heuristic selectors. UI changes regularly so order from most-specific to most-general.
PROMPT_INPUT_SELECTORS = [
    'textarea[placeholder*="What" i]',
    'textarea[placeholder*="Ask" i]',
    'textarea[placeholder*="message" i]',
    'div[contenteditable="true"][role="textbox"]',
    'textarea',
]

# An "Imagine" toggle / mode switcher. May not exist if image gen is the default
# behaviour for an explicit "imagine X" prompt.
IMAGINE_TOGGLE_SELECTORS = [
    'button:has-text("Imagine")',
    'button:has-text("Image")',
    '[aria-label*="Imagine" i]',
    '[role="tab"]:has-text("Imagine")',
]

# Login state markers — if any of these appear, user isn't authenticated.
LOGIN_GATE_SELECTORS = [
    'a[href*="/login"]',
    'a[href*="i/flow/login"]',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    ':text-matches("(?i)\\bsign in\\b")',
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


def wait_for_image(page: Page, profile_dir: Path, debug: bool, timeout_s: int = 180) -> Optional[str]:
    """Poll the page for a generated image element. Return its src URL when stable.

    Grok renders generated images as <img> with ai-generated content URLs. We
    look for an <img> whose src is large (indicating actual image bytes), not a
    UI sprite. Polling rather than waiting on a single selector because the DOM
    structure isn't predictable.
    """
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
            now = time.time()
            if src not in last_seen:
                last_seen[src] = now
                print(f"  [img] candidate detected w={o['w']} h={o['h']} url={src[:120]}", file=sys.stderr)
            elif now - last_seen[src] > 2.5:
                # Stable for >2.5s — likely fully loaded.
                _shot(page, profile_dir, "image-found", debug)
                return src
        time.sleep(1)
    _shot(page, profile_dir, "image-timeout", debug)
    return None


def submit_prompt(page: Page, prompt: str, profile_dir: Path, debug: bool):
    sel = _try_selectors(page, PROMPT_INPUT_SELECTORS)
    if not sel:
        _shot(page, profile_dir, "no-input", debug)
        raise RuntimeError(
            "Could not locate the prompt input. Layout may have changed. "
            "Inspect _debug-shots/no-input.png for the current DOM."
        )
    print(f"  [input] using selector: {sel}", file=sys.stderr)
    el = page.locator(sel).first
    el.click()
    el.fill(prompt)
    _shot(page, profile_dir, "prompt-filled", debug)

    # Optional: switch into Imagine mode if a toggle is visible.
    toggle = _try_selectors(page, IMAGINE_TOGGLE_SELECTORS)
    if toggle:
        try:
            page.locator(toggle).first.click(timeout=2000)
            print(f"  [toggle] clicked Imagine: {toggle}", file=sys.stderr)
            _shot(page, profile_dir, "imagine-toggled", debug)
        except Exception as e:
            print(f"  [toggle-skip] {e}", file=sys.stderr)

    # Submit. Try Enter first; some UIs need Ctrl+Enter or a Send button.
    try:
        el.press("Enter")
    except Exception:
        pass
    _shot(page, profile_dir, "submitted", debug)


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
    args = ap.parse_args()

    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else Path(f"/tmp/grok-{int(time.time())}.png")

    print(f"profile: {profile_dir}", file=sys.stderr)
    print(f"output:  {out_path}", file=sys.stderr)

    with sync_playwright() as pw:
        # launch_persistent_context bundles browser+context so cookies persist
        # naturally between runs without manual storage_state plumbing.
        context = pw.chromium.launch_persistent_context(
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
                # WSL2: default sandbox/shm-tmpfs combo causes silent renderer
                # crashes on heavyweight SPAs (grok.com qualifies).
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            print(f"[1/4] navigating to {GROK_URL}", file=sys.stderr)
            page.goto(GROK_URL, wait_until="domcontentloaded", timeout=60_000)
            _shot(page, profile_dir, "01-loaded", args.debug_shots)

            # Login gate detection.
            time.sleep(2)  # let the SPA render
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
                    "\n  → A browser window is open. Please complete the X login "
                    "and arrive at the Grok chat interface, then press Enter here:",
                    file=sys.stderr,
                )
                input()
                _shot(page, profile_dir, "02-after-manual-login", args.debug_shots)
                if args.login_only:
                    print("login-only mode — exiting after login.", file=sys.stderr)
                    return

            print(f"[3/4] submitting prompt: {args.prompt[:80]}{'...' if len(args.prompt)>80 else ''}",
                  file=sys.stderr)
            submit_prompt(page, args.prompt, profile_dir, args.debug_shots)

            print(f"[4/4] waiting up to {args.timeout}s for the generated image...", file=sys.stderr)
            url = wait_for_image(page, profile_dir, args.debug_shots, timeout_s=args.timeout)
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
