"""
_aurora_via_grok.py — Shared Aurora image-gen via Playwright cookie-drive.

The xAI dev API key (Aurora endpoint) is dead since 2026-05-05; this helper
routes Aurora-style image gen through tools/grok_imagine.py instead, reusing
Leo's SuperGrok subscription via cookie import. Replaces the dead
`requests.post('https://api.x.ai/v1/images/generations')` path.

Public API:
    generate(prompt, out_path) -> Path: writes JPG, returns the path.

Cookies path resolution: explicit arg → $GROK_COOKIES_FILE → default at
data/sessions/velikov/grok.com_cookies.json.

Underscore prefix marks this as pipeline-internal — not for direct CLI use.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Resolve nanoclaw root from this file's location.
#  - On host: projects/youtube/pipeline/.../parents[2] = nanoclaw repo root.
#  - In agent container: /workspace/extra/youtube/pipeline/.../parents[2] = /workspace.
_PIPELINE_DIR = Path(__file__).resolve().parent
_NANOCLAW_ROOT = _PIPELINE_DIR.parents[2]

# Containers ship a /.dockerenv marker. We use it to choose the right Python
# (host venv vs container's pip-installed playwright) and the right cookie
# location (groups/global/ on host = /workspace/global/ in container).
_IN_CONTAINER = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()

DRIVER = _NANOCLAW_ROOT / "tools" / "grok_imagine.py"
_HOST_VENV_PY = _NANOCLAW_ROOT / "tools" / ".playwright-venv" / "bin" / "python3"


def _resolve_python() -> str:
    """Pick the Python interpreter that has playwright installed.

    Host: tools/.playwright-venv/bin/python3 (Leo's setup venv).
    Container: /usr/bin/python3 (nanoclaw Dockerfile pip-installs playwright system-wide).
    """
    if _IN_CONTAINER:
        return "/usr/bin/python3"
    if _HOST_VENV_PY.exists():
        return str(_HOST_VENV_PY)
    return "/usr/bin/python3"


def _cookie_candidates() -> list[Path]:
    """Ordered list of cookie-file paths to probe. First existing wins."""
    if _IN_CONTAINER:
        return [Path("/workspace/global/grok.com_cookies.json")]
    return [
        _NANOCLAW_ROOT / "groups" / "global" / "grok.com_cookies.json",
        # Legacy path — the cookies were originally exported here.
        _NANOCLAW_ROOT / "data" / "sessions" / "velikov" / "grok.com_cookies.json",
    ]


def _resolve_cookies(override: str | Path | None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("GROK_COOKIES_FILE")
    if env:
        return Path(env)
    candidates = _cookie_candidates()
    for p in candidates:
        if p.exists():
            return p
    # Return the first candidate so the caller's missing-file error names a
    # plausible path rather than a legacy one.
    return candidates[0]


def generate(
    prompt: str,
    out_path: str | Path,
    *,
    cookies_file: str | Path | None = None,
    timeout_s: int = 240,
    headless: bool = True,
    reference_image: str | Path | None = None,
    mode: str = "image",
    quality: bool = False,
) -> Path:
    """Generate one image or short video via grok.com browser-drive.

    Args:
        prompt: Aurora prompt text. In image mode the driver auto-prefixes
            "imagine " if missing; in video mode the prompt is sent as-is.
        out_path: where to write the result. Image mode = JPG/PNG, video mode = mp4.
        cookies_file: override path to grok.com cookie JSON.
        timeout_s: per-call ceiling. Image gen ~15-30s, video gen ~30-90s; the
            default 240s covers both with headroom.
        headless: pass --headless to the driver (True for batch).
        reference_image: optional path to a local image to attach as a
            build-from reference (Aurora img2img). Image mode only — video
            mode does not support reference images.
        mode: 'image' (default, chat-root flow) or 'video' (uses /imagine page,
            Aurora video gen, ~6s mp4 output).
        quality: video mode only — pick the Quality radio (slower, higher
            fidelity). Default Speed.

    Returns:
        Resolved path to the written file.

    Raises:
        FileNotFoundError: cookies file or reference image missing.
        ValueError: invalid mode, or reference_image passed in video mode.
        RuntimeError: driver failed or wrote no output.
    """
    if mode not in ("image", "video"):
        raise ValueError(f"mode must be 'image' or 'video', got {mode!r}")
    if mode == "video" and reference_image:
        raise ValueError("reference_image is not supported in video mode")
    cookies = _resolve_cookies(cookies_file)
    if not cookies.exists():
        raise FileNotFoundError(
            f"Grok cookies file not found at {cookies}. "
            "Re-export from a real Chrome via the Cookie-Editor extension; "
            "see project_grok_imagine_browser_drive memory note."
        )

    ref_path: Path | None = None
    if reference_image:
        ref_path = Path(reference_image).expanduser().resolve()
        if not ref_path.exists():
            raise FileNotFoundError(f"Reference image not found: {ref_path}")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Per-call profile dir: cookies are injected fresh from JSON every run, so
    # the persistent profile gives us nothing — and a SHARED `/tmp/grok-imagine-profile`
    # was the failure mode in long-running agent containers (Stella, 2026-05-08):
    # a prior crashed Chromium leaves SingletonLock + half-written state that
    # breaks the next launch with `chrome_crashpad_handler: --database is required`
    # / `recvmsg: Connection reset by peer`.
    profile_dir = Path(tempfile.mkdtemp(prefix="grok-imagine-", dir="/tmp"))
    cmd = [
        _resolve_python(), str(DRIVER),
        "--cookies-file", str(cookies),
        "--prompt", prompt,
        "--out", str(out),
        "--timeout", str(timeout_s),
        "--profile-dir", str(profile_dir),
        "--mode", mode,
    ]
    if ref_path is not None:
        cmd.extend(["--reference-image", str(ref_path)])
    if quality:
        cmd.append("--quality")
    if headless:
        cmd.append("--headless")

    try:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s + 60,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"grok_imagine.py wall-clock timeout after {timeout_s + 60}s"
            ) from e

        if result.returncode != 0:
            # Driver writes diagnostics to stderr — surface so callers can see why.
            sys.stderr.write(result.stderr)
            raise RuntimeError(
                f"grok_imagine.py exited {result.returncode}; see stderr above."
            )
        if not out.exists():
            raise RuntimeError(
                f"grok_imagine.py reported success but {out} is missing."
            )
        return out
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


# CLI entry point for one-shot image gen outside the YouTube pipeline shape.
# Any agent with /workspace/extra/youtube mounted can invoke:
#   python3 /workspace/extra/youtube/pipeline/_aurora_via_grok.py "<prompt>" <out_path>
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Aurora image gen via grok.com browser-drive (one-shot CLI).",
    )
    ap.add_argument("prompt", help="Aurora prompt text")
    ap.add_argument("out_path", help="Where to write the JPG")
    ap.add_argument("--cookies-file", default=None,
                    help="Override grok.com cookies path")
    ap.add_argument("--reference-image", default=None,
                    help="Optional local image to attach as a build-from "
                         "reference (Aurora img2img). Same ref across many "
                         "calls = character consistency across beats. "
                         "Image mode only.")
    ap.add_argument("--mode", default="image", choices=["image", "video"],
                    help="'image' (default) = JPG via chat-root composer. "
                         "'video' = ~6s mp4 via grok.com/imagine Video toggle.")
    ap.add_argument("--quality", action="store_true",
                    help="Video mode only: Quality radio (slower, higher fidelity). "
                         "Default Speed.")
    ap.add_argument("--timeout", type=int, default=240)
    ns = ap.parse_args()

    try:
        path = generate(ns.prompt, ns.out_path,
                        cookies_file=ns.cookies_file, timeout_s=ns.timeout,
                        reference_image=ns.reference_image,
                        mode=ns.mode, quality=ns.quality)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK wrote {path} ({path.stat().st_size} bytes)")
