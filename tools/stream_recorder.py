#!/usr/bin/env python3
"""
stream_recorder.py — Video/audio stream recorder
Supports: YouTube Live, Twitch, HLS (.m3u8), RTMP, and most streamlink-compatible URLs
Requirements: pip install streamlink
              + ffmpeg on your system (https://ffmpeg.org)
"""

import argparse
import subprocess
import sys
import os
import re
import shutil
import threading
from datetime import datetime

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

KNOWN_PATHS = {
    "ffmpeg":     r"C:\Users\Sol\ffmpeg\bin\ffmpeg.exe",
    "streamlink": r"C:\Users\Sol\AppData\Local\Python\pythoncore-3.14-64\Scripts\streamlink.exe",
    "yt-dlp":     r"C:\Users\Sol\AppData\Local\Python\pythoncore-3.14-64\Scripts\yt-dlp.exe",
}

def resolve_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    fallback = KNOWN_PATHS.get(name)
    if fallback and os.path.isfile(fallback):
        return fallback
    return None


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def build_output_path(output_dir: str, stream_url: str, fmt: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = sanitize_filename(stream_url.split("//")[-1][:40])
    filename = f"stream_{slug}_{ts}.{fmt}"
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, filename)


# ──────────────────────────────────────────────
# Recorders
# ──────────────────────────────────────────────

DIRECT_PATTERNS  = [r"\.m3u8", r"rtmp://", r"rtmps://", r"rtsp://", r"udp://"]
YOUTUBE_PATTERNS = [r"youtube\.com", r"youtu\.be"]

def is_direct_stream(url: str) -> bool:
    return any(re.search(p, url) for p in DIRECT_PATTERNS)

def is_youtube(url: str) -> bool:
    return any(re.search(p, url) for p in YOUTUBE_PATTERNS)


def record_with_streamlink(url: str, quality: str, output_path: str,
                            cookies_browser: str | None = None,
                            log=print, proc_holder: list = None) -> int:
    sl = resolve_tool("streamlink")
    if not sl:
        log("ERROR: streamlink not found. Install with: pip install streamlink")
        return 1

    cmd = [sl, "--output", output_path, "--force"]
    if cookies_browser and cookies_browser != "none":
        cmd += ["--cookies-from-browser", cookies_browser]
    cmd += [url, quality]

    log(f"streamlink -> {output_path}")
    log(f"cmd: {' '.join(cmd)}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    if proc_holder is not None:
        proc_holder.append(proc)

    for line in proc.stdout:
        log(line.rstrip())
    proc.wait()
    return proc.returncode


def record_with_ytdlp(url: str, quality: str, output_path: str,
                       cookies_browser: str | None = None,
                       log=print, proc_holder: list = None) -> int:
    yt = resolve_tool("yt-dlp")
    if not yt:
        log("ERROR: yt-dlp not found. Install with: pip install yt-dlp")
        return 1

    fmt_map = {"1080p": "bestvideo[height<=1080]+bestaudio/best",
               "720p":  "bestvideo[height<=720]+bestaudio/best",
               "480p":  "bestvideo[height<=480]+bestaudio/best",
               "360p":  "bestvideo[height<=360]+bestaudio/best",
               "worst": "worstvideo+worstaudio/worst",
               "audio_only": "bestaudio"}
    fmt = fmt_map.get(quality, "bestvideo+bestaudio/best")

    cmd = [yt,
           "--ffmpeg-location", str(os.path.dirname(resolve_tool("ffmpeg") or "ffmpeg")),
           "-f", fmt,
           "--merge-output-format", os.path.splitext(output_path)[1].lstrip(".") or "mkv",
           "-o", output_path]
    if cookies_browser and cookies_browser != "none":
        cmd += ["--cookies-from-browser", cookies_browser]
    cmd.append(url)

    log(f"yt-dlp -> {output_path}")
    log(f"cmd: {' '.join(cmd)}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    if proc_holder is not None:
        proc_holder.append(proc)

    for line in proc.stdout:
        log(line.rstrip())
    proc.wait()
    return proc.returncode


def record_with_ffmpeg(url: str, output_path: str, duration: int | None,
                        log=print, proc_holder: list = None) -> int:
    ff = resolve_tool("ffmpeg")
    if not ff:
        log("ERROR: ffmpeg not found. Download from https://ffmpeg.org/download.html")
        return 1

    cmd = [ff, "-hide_banner", "-loglevel", "warning", "-i", url, "-c", "copy"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd.append(output_path)

    log(f"ffmpeg -> {output_path}")
    log(f"cmd: {' '.join(cmd)}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    if proc_holder is not None:
        proc_holder.append(proc)

    for line in proc.stdout:
        log(line.rstrip())
    proc.wait()
    return proc.returncode


def run_record(url: str, quality: str, output_dir: str, fmt: str,
               duration: int | None, engine: str, output_override: str | None,
               cookies_browser: str | None = None,
               log=print, proc_holder: list = None) -> int:
    url = url.strip()
    if fmt == "auto":
        fmt = "mp4" if is_direct_stream(url) else "mkv"

    output_path = output_override or build_output_path(output_dir, url, fmt)

    log(f"URL     : {url}")
    log(f"Quality : {quality}")
    log(f"Output  : {output_path}")
    if duration:
        log(f"Duration: {duration}s")
    if cookies_browser and cookies_browser != "none":
        log(f"Cookies : {cookies_browser}")
    log("")

    # resolve engine
    if engine == "auto":
        if is_direct_stream(url):
            engine = "ffmpeg"
        elif is_youtube(url):
            engine = "yt-dlp"
        else:
            engine = "streamlink"
        log(f"Engine  : {engine} (auto-selected)")

    if engine == "ffmpeg":
        return record_with_ffmpeg(url, output_path, duration, log, proc_holder)
    elif engine == "yt-dlp":
        return record_with_ytdlp(url, quality, output_path, cookies_browser, log, proc_holder)
    else:  # streamlink
        rc = record_with_streamlink(url, quality, output_path, cookies_browser, log, proc_holder)
        if rc != 0:
            log("streamlink failed — falling back to ffmpeg...")
            return record_with_ffmpeg(url, output_path, duration, log, proc_holder)
        return rc


# ──────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, scrolledtext

    root = tk.Tk()
    root.title("Stream Recorder")
    root.resizable(False, False)

    proc_holder = []
    recording = False

    QUALITIES = ["best", "1080p", "720p", "480p", "360p", "worst", "audio_only"]
    FORMATS   = ["auto", "mp4", "mkv", "ts", "flv", "mp3", "aac"]
    BROWSERS  = ["none", "chrome", "firefox", "edge", "brave", "opera", "chromium"]
    ENGINES   = ["auto", "yt-dlp", "streamlink", "ffmpeg"]

    pad = {"padx": 8, "pady": 4}

    # ── URL ──
    tk.Label(root, text="Stream URL:").grid(row=0, column=0, sticky="w", **pad)
    url_var = tk.StringVar()
    tk.Entry(root, textvariable=url_var, width=60).grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

    # ── Output dir ──
    tk.Label(root, text="Output folder:").grid(row=1, column=0, sticky="w", **pad)
    dir_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Videos", "recordings"))
    tk.Entry(root, textvariable=dir_var, width=52).grid(row=1, column=1, sticky="ew", **pad)

    def browse():
        d = filedialog.askdirectory(initialdir=dir_var.get())
        if d:
            dir_var.set(d)

    tk.Button(root, text="Browse…", command=browse).grid(row=1, column=2, **pad)

    # ── Quality / Format ──
    tk.Label(root, text="Quality:").grid(row=2, column=0, sticky="w", **pad)
    quality_var = tk.StringVar(value="best")
    ttk.Combobox(root, textvariable=quality_var, values=QUALITIES, width=14, state="readonly")\
        .grid(row=2, column=1, sticky="w", **pad)

    tk.Label(root, text="Format:").grid(row=3, column=0, sticky="w", **pad)
    fmt_var = tk.StringVar(value="auto")
    ttk.Combobox(root, textvariable=fmt_var, values=FORMATS, width=14, state="readonly")\
        .grid(row=3, column=1, sticky="w", **pad)

    # ── Cookies ──
    tk.Label(root, text="Cookies from browser:").grid(row=4, column=0, sticky="w", **pad)
    cookies_var = tk.StringVar(value="none")
    ttk.Combobox(root, textvariable=cookies_var, values=BROWSERS, width=14, state="readonly")\
        .grid(row=4, column=1, sticky="w", **pad)
    tk.Label(root, text="(fixes 403 on YouTube/Twitch)", fg="gray").grid(row=4, column=2, sticky="w", **pad)

    # ── Engine ──
    tk.Label(root, text="Engine:").grid(row=5, column=0, sticky="w", **pad)
    engine_var = tk.StringVar(value="auto")
    ttk.Combobox(root, textvariable=engine_var, values=ENGINES, width=14, state="readonly")\
        .grid(row=5, column=1, sticky="w", **pad)
    tk.Label(root, text="(auto: YouTube→yt-dlp, HLS/RTMP→ffmpeg, else→streamlink)", fg="gray")\
        .grid(row=5, column=2, sticky="w", **pad)

    # ── Duration ──
    tk.Label(root, text="Duration (sec, optional):").grid(row=6, column=0, sticky="w", **pad)
    dur_var = tk.StringVar()
    tk.Entry(root, textvariable=dur_var, width=10).grid(row=6, column=1, sticky="w", **pad)

    # ── Log ──
    log_box = scrolledtext.ScrolledText(root, width=80, height=16, state="disabled",
                                         font=("Consolas", 9))
    log_box.grid(row=8, column=0, columnspan=3, padx=8, pady=(4, 0))

    status_var = tk.StringVar(value="Ready")
    tk.Label(root, textvariable=status_var, anchor="w").grid(
        row=9, column=0, columnspan=3, sticky="ew", padx=8, pady=2)

    def log(msg: str):
        log_box.configure(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    # ── Buttons ──
    btn_frame = tk.Frame(root)
    btn_frame.grid(row=10, column=0, columnspan=3, pady=8)

    def start_recording():
        nonlocal recording
        url = url_var.get().strip()
        if not url:
            status_var.set("Enter a stream URL first.")
            return

        dur_str = dur_var.get().strip()
        duration = int(dur_str) if dur_str.isdigit() else None

        proc_holder.clear()
        recording = True
        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        status_var.set("Recording…")
        log("─" * 60)

        def worker():
            rc = run_record(
                url=url,
                quality=quality_var.get(),
                output_dir=dir_var.get(),
                fmt=fmt_var.get(),
                duration=duration,
                engine=engine_var.get(),
                output_override=None,
                cookies_browser=cookies_var.get(),
                log=lambda m: root.after(0, log, m),
                proc_holder=proc_holder,
            )
            root.after(0, on_done, rc)

        threading.Thread(target=worker, daemon=True).start()

    def stop_recording():
        for p in proc_holder:
            try:
                p.terminate()
            except Exception:
                pass
        status_var.set("Stopping…")
        stop_btn.config(state="disabled")

    def on_done(rc: int):
        nonlocal recording
        recording = False
        start_btn.config(state="normal")
        stop_btn.config(state="disabled")
        status_var.set(f"Done (exit code {rc})." if rc == 0 else f"Finished with errors (code {rc}).")
        log(f"─ exit {rc} " + "─" * 54)

    start_btn = tk.Button(btn_frame, text="  Start Recording  ", command=start_recording,
                           bg="#2e7d32", fg="white", font=("Segoe UI", 10, "bold"), width=18)
    start_btn.pack(side="left", padx=6)

    stop_btn = tk.Button(btn_frame, text="  Stop  ", command=stop_recording,
                          bg="#c62828", fg="white", font=("Segoe UI", 10), width=10,
                          state="disabled")
    stop_btn.pack(side="left", padx=6)

    root.mainloop()


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    if len(sys.argv) == 1:
        launch_gui()
        return

    parser = argparse.ArgumentParser(
        description="Record video/audio streams (HLS, RTMP, YouTube Live, Twitch, …)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python stream_recorder.py https://www.youtube.com/watch?v=LIVE_ID
  python stream_recorder.py https://www.twitch.tv/channel -q 720p
  python stream_recorder.py https://example.com/live/stream.m3u8 -d 60
  python stream_recorder.py rtmp://live.example.com/app/key -f flv
  python stream_recorder.py https://www.youtube.com/watch?v=XYZ --cookies-browser chrome
        """
    )
    parser.add_argument("url")
    parser.add_argument("-q", "--quality", default="best")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--output-dir", default="recordings")
    parser.add_argument("-f", "--format", default="auto",
                        choices=["auto", "mp4", "mkv", "ts", "flv", "mp3", "aac"])
    parser.add_argument("-d", "--duration", type=int, default=None)
    parser.add_argument("--engine", default="auto",
                        choices=["auto", "yt-dlp", "streamlink", "ffmpeg"],
                        help="Recording engine (default: auto)")
    parser.add_argument("--cookies-browser", default=None,
                        choices=["chrome", "firefox", "edge", "brave", "opera", "chromium"],
                        help="Pull cookies from browser to bypass 403 errors")

    args = parser.parse_args()
    rc = run_record(
        url=args.url,
        quality=args.quality,
        output_dir=args.output_dir,
        fmt=args.format,
        duration=args.duration,
        engine=args.engine,
        output_override=args.output,
        cookies_browser=args.cookies_browser,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
